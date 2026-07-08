"""Runtime telemetry (ROADMAP 2.3): OTLP parsing, store, merge, flags, receiver.

merge_runtime tests use hand-built graphs and injected `now_ms` so window
math is deterministic. The end-to-end test replays the demo script's batches
through the real HTTP receiver, then scans a demo copy.
"""
import importlib.util
import json
import shutil
import threading
import time
import urllib.request
import urllib.error

import pytest

from wiremap import cli
from wiremap.collector import (RuntimeStore, make_collector, merge_runtime,
                               parse_otlp_traces)
from wiremap.graph import Graph, Node, NodeType
from wiremap.risk import DEFAULT_CONFIG

from conftest import DEMO_DIR, PROJECT_ROOT

NOW_MS = 1_800_000_000_000
NOW_NS = NOW_MS * 1_000_000


def otlp_span(method="GET", route=None, path=None, status=200, dur_ms=100.0,
              kind=2, legacy=False, ts_ns=NOW_NS):
    key_m = "http.method" if legacy else "http.request.method"
    key_s = "http.status_code" if legacy else "http.response.status_code"
    key_p = "http.target" if legacy else "url.path"
    attrs = [{"key": key_m, "value": {"stringValue": method}},
             {"key": key_s, "value": {"intValue": str(status)}}]
    if route:
        attrs.append({"key": "http.route", "value": {"stringValue": route}})
    if path:
        attrs.append({"key": key_p, "value": {"stringValue": path}})
    return {"name": "s", "kind": kind, "startTimeUnixNano": str(ts_ns),
            "endTimeUnixNano": str(int(ts_ns + dur_ms * 1e6)),
            "attributes": attrs}


def otlp_payload(*spans):
    return {"resourceSpans": [{"scopeSpans": [{"spans": list(spans)}]}]}


class TestParseOtlp:
    def test_parses_new_semconv(self):
        recs = parse_otlp_traces(otlp_payload(
            otlp_span(method="post", route="/api/orders", status=201, dur_ms=250)))
        assert recs == [{"method": "POST", "route": "/api/orders", "path": None,
                         "status": 201, "ts_ms": NOW_MS, "dur_ms": 250.0}]

    def test_parses_legacy_semconv(self):
        recs = parse_otlp_traces(otlp_payload(
            otlp_span(route="/x", path="/x", legacy=True, status=404)))
        assert recs[0]["status"] == 404
        assert recs[0]["path"] == "/x"

    def test_non_server_spans_skipped(self):
        assert parse_otlp_traces(otlp_payload(otlp_span(kind=3))) == []

    def test_span_without_method_skipped(self):
        span = otlp_span()
        span["attributes"] = [a for a in span["attributes"]
                              if "method" not in a["key"]]
        assert parse_otlp_traces(otlp_payload(span)) == []


class TestRuntimeStore:
    def test_routed_vs_unrouted_and_roundtrip(self, tmp_path):
        p = str(tmp_path / "runtime.json")
        store = RuntimeStore(p)
        now_ms = int(time.time() * 1000)
        store.ingest([
            {"method": "GET", "route": "/api/users/{id}", "path": None,
             "status": 200, "ts_ms": now_ms, "dur_ms": 50.0},
            {"method": "GET", "route": None, "path": "/api/users/7",
             "status": 200, "ts_ms": now_ms, "dur_ms": 60.0},
        ])
        store.save()
        loaded = RuntimeStore(p)
        assert list(loaded.routed) == ["GET /api/users/:p"]
        assert len(loaded.unrouted) == 1

    def test_prune_drops_old_observations(self, tmp_path):
        store = RuntimeStore(str(tmp_path / "r.json"), window_hours=1)
        store.ingest([
            {"method": "GET", "route": "/a", "path": None, "status": 200,
             "ts_ms": NOW_MS - 2 * 3_600_000, "dur_ms": 10.0},
            {"method": "GET", "route": "/a", "path": None, "status": 200,
             "ts_ms": NOW_MS - 60_000, "dur_ms": 10.0},
        ])
        store.prune(NOW_MS)
        assert len(store.routed["GET /a"]) == 1

    def test_corrupt_store_starts_fresh(self, tmp_path):
        p = tmp_path / "runtime.json"
        p.write_text("{oops")
        store = RuntimeStore(str(p))
        assert store.routed == {} and store.unrouted == []


def _endpoint(g, method, path, flags=()):
    n = g.add_node(Node(
        id=f"ep:{method} {path}", type=NodeType.ENDPOINT,
        label=f"{method} {path}", file="app.py", line=1,
        meta={"raw_path": path, "handler": "h"}))
    for code, severity in flags:
        n.risk_flags.append({"code": code, "severity": severity,
                             "category": "x", "message": "", "evidence": "",
                             "suggestion": ""})
    return n


def _store_file(tmp_path, routed=None, unrouted=None, window=24):
    p = tmp_path / "runtime.json"
    p.write_text(json.dumps({"version": 1, "window_hours": window,
                             "routed": routed or {}, "unrouted": unrouted or []}))
    return str(p)


def obs(dur_ms, status=200, age_min=5):
    return [NOW_MS - age_min * 60_000, dur_ms, status]


class TestMergeRuntime:
    def test_missing_store_returns_none(self, tmp_path):
        g = Graph()
        assert merge_runtime(g, str(tmp_path / "nope.json"),
                             DEFAULT_CONFIG, NOW_MS) is None

    def test_aggregates(self, tmp_path):
        g = Graph()
        ep = _endpoint(g, "GET", "/api/users/{id}")
        rows = [obs(d, status=500 if i == 0 else 200)
                for i, d in enumerate([100, 200, 300, 400, 500,
                                       600, 700, 800, 900, 1000])]
        path = _store_file(tmp_path, routed={"GET /api/users/:p": rows})
        stats = merge_runtime(g, path, DEFAULT_CONFIG, NOW_MS)
        rt = ep.meta["runtime"]
        assert stats == {"endpoints_with_traffic": 1, "runtime_flags": 1}
        assert rt["req_count"] == 10
        assert rt["p50_ms"] == 500.0               # nearest-rank, not interpolated
        assert rt["p95_ms"] == 1000.0
        assert rt["error_rate"] == 0.1
        assert rt["confidence"] == "certain"
        codes = {f["code"] for f in ep.risk_flags}
        assert "high_latency" not in codes         # p95 == threshold, not over
        assert "high_error_rate" in codes          # 10% > 2% default

    def test_error_rate_flag_fires_above_threshold(self, tmp_path):
        g = Graph()
        ep = _endpoint(g, "GET", "/a")
        rows = [obs(100, status=500 if i < 3 else 200) for i in range(100)]
        merge_runtime(g, _store_file(tmp_path, routed={"GET /a": rows}),
                      DEFAULT_CONFIG, NOW_MS)
        flag = [f for f in ep.risk_flags if f["code"] == "high_error_rate"]
        assert flag and flag[0]["severity"] == "high"

    def test_error_rate_at_threshold_no_flag(self, tmp_path):
        g = Graph()
        ep = _endpoint(g, "GET", "/a")
        rows = [obs(100, status=500 if i < 2 else 200) for i in range(100)]
        merge_runtime(g, _store_file(tmp_path, routed={"GET /a": rows}),
                      DEFAULT_CONFIG, NOW_MS)
        assert not any(f["code"] == "high_error_rate" for f in ep.risk_flags)

    def test_high_latency_fires_over_threshold(self, tmp_path):
        g = Graph()
        ep = _endpoint(g, "GET", "/a")
        rows = [obs(1500) for _ in range(10)]
        merge_runtime(g, _store_file(tmp_path, routed={"GET /a": rows}),
                      DEFAULT_CONFIG, NOW_MS)
        flag = [f for f in ep.risk_flags if f["code"] == "high_latency"]
        assert flag and flag[0]["severity"] == "medium"

    def test_latency_threshold_configurable(self, tmp_path):
        g = Graph()
        ep = _endpoint(g, "GET", "/a")
        cfg = {**DEFAULT_CONFIG,
               "runtime": {"p95_ms_threshold": 200, "error_rate_threshold": 0.02}}
        merge_runtime(g, _store_file(tmp_path, routed={"GET /a": [obs(300)]}),
                      cfg, NOW_MS)
        assert any(f["code"] == "high_latency" for f in ep.risk_flags)

    def test_url_path_fallback_is_probable(self, tmp_path):
        g = Graph()
        ep = _endpoint(g, "GET", "/api/users/{user_id}")
        unrouted = [[NOW_MS - 60_000, "GET", "/api/users/abc-123", 80.0, 200],
                    [NOW_MS - 60_000, "GET", "/api/users/9", 90.0, 200],
                    [NOW_MS - 60_000, "POST", "/api/users/9", 90.0, 200],
                    [NOW_MS - 60_000, "GET", "/api/other", 10.0, 200]]
        merge_runtime(g, _store_file(tmp_path, unrouted=unrouted),
                      DEFAULT_CONFIG, NOW_MS)
        rt = ep.meta["runtime"]
        assert rt["req_count"] == 2                # method + pattern must match
        assert rt["confidence"] == "probable"

    def test_routed_plus_fallback_stays_certain(self, tmp_path):
        g = Graph()
        ep = _endpoint(g, "GET", "/api/users/{user_id}")
        merge_runtime(g, _store_file(
            tmp_path,
            routed={"GET /api/users/:p": [obs(50)]},
            unrouted=[[NOW_MS - 60_000, "GET", "/api/users/4", 60.0, 200]],
        ), DEFAULT_CONFIG, NOW_MS)
        rt = ep.meta["runtime"]
        assert rt["req_count"] == 2
        assert rt["confidence"] == "certain"

    def test_confirmed_dead_replaces_unused_endpoint(self, tmp_path):
        g = Graph()
        ep = _endpoint(g, "GET", "/dead", flags=[("unused_endpoint", "low")])
        merge_runtime(g, _store_file(tmp_path, routed={}),
                      DEFAULT_CONFIG, NOW_MS)
        codes = {f["code"] for f in ep.risk_flags}
        assert codes == {"confirmed_dead"}
        assert ep.meta["runtime"]["req_count"] == 0

    def test_unused_endpoint_with_traffic_not_upgraded(self, tmp_path):
        g = Graph()
        ep = _endpoint(g, "GET", "/ext", flags=[("unused_endpoint", "low")])
        merge_runtime(g, _store_file(tmp_path, routed={"GET /ext": [obs(50)]}),
                      DEFAULT_CONFIG, NOW_MS)
        codes = {f["code"] for f in ep.risk_flags}
        assert "unused_endpoint" in codes and "confirmed_dead" not in codes

    def test_hot_fragile_fires_on_hot_risky_endpoint(self, tmp_path):
        g = Graph()
        hot = _endpoint(g, "POST", "/orders",
                        flags=[("sql_injection_risk", "critical")])
        _endpoint(g, "GET", "/users")
        merge_runtime(g, _store_file(tmp_path, routed={
            "POST /orders": [obs(100) for _ in range(50)],
            "GET /users": [obs(50) for _ in range(10)],
        }), DEFAULT_CONFIG, NOW_MS)
        flag = [f for f in hot.risk_flags if f["code"] == "hot_fragile"]
        assert flag and flag[0]["severity"] == "critical"
        assert "sql_injection_risk" in flag[0]["evidence"]

    def test_no_hot_fragile_on_clean_hot_endpoint(self, tmp_path):
        g = Graph()
        hot = _endpoint(g, "POST", "/orders")
        merge_runtime(g, _store_file(tmp_path, routed={
            "POST /orders": [obs(100) for _ in range(50)],
        }), DEFAULT_CONFIG, NOW_MS)
        assert not any(f["code"] == "hot_fragile" for f in hot.risk_flags)

    def test_no_hot_fragile_on_cold_risky_endpoint(self, tmp_path):
        g = Graph()
        cold = _endpoint(g, "POST", "/rare",
                         flags=[("sql_injection_risk", "critical")])
        _endpoint(g, "GET", "/busy")
        merge_runtime(g, _store_file(tmp_path, routed={
            "POST /rare": [obs(100)],
            "GET /busy": [obs(50) for _ in range(100)],
        }), DEFAULT_CONFIG, NOW_MS)
        assert not any(f["code"] == "hot_fragile" for f in cold.risk_flags)


def _load_replay_module():
    spec = importlib.util.spec_from_file_location(
        "replay_spans", f"{PROJECT_ROOT}/demo/replay_spans.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestReceiver:
    @pytest.fixture
    def server(self, tmp_path):
        store = RuntimeStore(str(tmp_path / ".wiremap" / "runtime.json"))
        srv = make_collector(store, 0)
        thread = threading.Thread(target=srv.serve_forever, daemon=True)
        thread.start()
        yield srv
        srv.shutdown()

    def _post(self, srv, body: bytes, ctype="application/json",
              path="/v1/traces"):
        req = urllib.request.Request(
            f"http://localhost:{srv.server_address[1]}{path}",
            data=body, headers={"Content-Type": ctype}, method="POST")
        try:
            with urllib.request.urlopen(req) as resp:
                return resp.status
        except urllib.error.HTTPError as e:
            return e.code

    def test_accepts_replay_batches_and_persists(self, server):
        replay = _load_replay_module()
        for batch in replay.build_batches():
            assert self._post(server, json.dumps(batch).encode()) == 200
        store = RuntimeStore(server.store.path)
        assert store.routed["POST /api/orders"]
        assert len(store.unrouted) >= 25

    def test_rejects_protobuf_content_type(self, server):
        assert self._post(server, b"\x00", ctype="application/x-protobuf") == 415

    def test_rejects_wrong_path(self, server):
        assert self._post(server, b"{}", path="/v1/metrics") == 404

    def test_rejects_bad_json(self, server):
        assert self._post(server, b"{nope") == 400


class TestDemoAcceptance:
    """ROADMAP 2.3 acceptance: replay demo spans, scan, verify hot_fragile."""

    def test_full_pipeline_on_demo_copy(self, tmp_path, capsys):
        demo = tmp_path / "demo"
        shutil.copytree(DEMO_DIR, demo, ignore=shutil.ignore_patterns(".wiremap"))

        replay = _load_replay_module()
        store = RuntimeStore(str(demo / ".wiremap" / "runtime.json"))
        for batch in replay.build_batches():
            store.ingest(parse_otlp_traces(batch))
        store.save()

        assert cli.main(["scan", str(demo)]) == 0
        out = capsys.readouterr().out
        assert "runtime overlay   3 endpoints with traffic" in out

        with open(demo / ".wiremap" / "graph.json", encoding="utf-8") as f:
            nodes = {n["id"]: n for n in json.load(f)["nodes"]}

        orders = nodes["ep:POST /api/orders"]
        codes = {f["code"]: f["severity"] for f in orders["risk_flags"]}
        assert codes.get("hot_fragile") == "critical"
        assert codes.get("high_latency") == "medium"
        assert codes.get("high_error_rate") == "high"
        assert orders["meta"]["runtime"]["req_count"] == 60

        detail = nodes["ep:GET /api/orders/{order_id}"]
        assert detail["meta"]["runtime"]["confidence"] == "probable"
        assert detail["meta"]["runtime"]["req_count"] == 25

        summary = nodes["ep:GET /api/reports/summary"]
        sum_codes = {f["code"] for f in summary["risk_flags"]}
        assert "confirmed_dead" in sum_codes
        assert "unused_endpoint" not in sum_codes

        users = nodes["ep:GET /api/users/{user_id}"]
        assert users["meta"]["runtime"]["req_count"] == 40
        assert users["meta"]["runtime"]["error_rate"] == 0.0
