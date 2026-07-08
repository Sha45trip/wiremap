"""Runtime telemetry collector (ROADMAP 2.3).

A small OTLP/HTTP receiver (`POST /v1/traces`, JSON encoding — no protobuf
dependency) run via `wiremap collect --port 4318`. Users point standard
OpenTelemetry auto-instrumentation at it; wiremap never ships custom
instrumentation.

Server spans are reduced to per-endpoint observations `[ts_ms, dur_ms,
status]` in `.wiremap/runtime.json`, keyed by the same `canonicalize()` the
matcher uses. Spans carrying `http.route` (the templated route) are keyed
directly (CERTAIN); spans with only a concrete `url.path` are stored raw and
matched against endpoint route patterns at scan time (PROBABLE).

`wiremap scan` merges the store when present: endpoint nodes gain
`meta.runtime = {req_count, p50_ms, p95_ms, error_rate, ...}` over a rolling
window (default 24h) plus the runtime flags high_latency, high_error_rate,
confirmed_dead, and hot_fragile.
"""
from __future__ import annotations

import json
import os
import re
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .graph import Graph, NodeType, RiskFlag
from .matcher import canonicalize

STORE_VERSION = 1
DEFAULT_WINDOW_HOURS = 24.0

# span kind 2 = SPAN_KIND_SERVER (incoming request)
_SERVER_KINDS = (2, "SPAN_KIND_SERVER")


# --------------------------------------------------------------- OTLP parsing

def _attr_value(v: dict):
    if "stringValue" in v:
        return v["stringValue"]
    if "intValue" in v:
        return int(v["intValue"])          # JSON encoding sends int64 as string
    if "doubleValue" in v:
        return v["doubleValue"]
    if "boolValue" in v:
        return v["boolValue"]
    return None


def _attrs(span: dict) -> dict:
    out = {}
    for a in span.get("attributes", []):
        if "key" in a and isinstance(a.get("value"), dict):
            out[a["key"]] = _attr_value(a["value"])
    return out


def parse_otlp_traces(payload: dict) -> list[dict]:
    """OTLP/JSON ExportTraceServiceRequest -> list of server-span records.

    Accepts both current (`http.request.method`) and legacy (`http.method`)
    semantic-convention attribute names — real-world auto-instrumentation
    still emits both generations.
    """
    records = []
    for rs in payload.get("resourceSpans", []):
        scope_spans = rs.get("scopeSpans") or rs.get("instrumentationLibrarySpans") or []
        for ss in scope_spans:
            for span in ss.get("spans", []):
                if span.get("kind") not in _SERVER_KINDS:
                    continue
                attrs = _attrs(span)
                method = attrs.get("http.request.method") or attrs.get("http.method")
                if not method:
                    continue
                try:
                    start = int(span["startTimeUnixNano"])
                    end = int(span["endTimeUnixNano"])
                except (KeyError, TypeError, ValueError):
                    continue
                status = (attrs.get("http.response.status_code")
                          or attrs.get("http.status_code") or 0)
                records.append({
                    "method": str(method).upper(),
                    "route": attrs.get("http.route"),
                    "path": attrs.get("url.path") or attrs.get("http.target"),
                    "status": int(status),
                    "ts_ms": start // 1_000_000,
                    "dur_ms": round((end - start) / 1_000_000, 1),
                })
    return records


# ---------------------------------------------------------------------- store

class RuntimeStore:
    """Rolling-window store of endpoint observations, persisted as JSON.

    routed:   {"GET /api/users/:p": [[ts_ms, dur_ms, status], ...]}
    unrouted: [[ts_ms, "GET", "/api/users/42", dur_ms, status], ...]
    """

    def __init__(self, path: str, window_hours: float = DEFAULT_WINDOW_HOURS):
        self.path = path
        self.window_hours = window_hours
        self.routed: dict[str, list] = {}
        self.unrouted: list = []
        if os.path.exists(path):
            try:
                with open(path, encoding="utf-8") as f:
                    raw = json.load(f)
                if isinstance(raw, dict) and raw.get("version") == STORE_VERSION:
                    self.routed = raw.get("routed", {})
                    self.unrouted = raw.get("unrouted", [])
                    self.window_hours = raw.get("window_hours", window_hours)
            except (json.JSONDecodeError, OSError):
                pass

    def ingest(self, records: list[dict]) -> int:
        n = 0
        for r in records:
            if r["route"]:
                key = f"{r['method']} {canonicalize(r['route'])}"
                self.routed.setdefault(key, []).append(
                    [r["ts_ms"], r["dur_ms"], r["status"]])
            elif r["path"]:
                self.unrouted.append(
                    [r["ts_ms"], r["method"], r["path"], r["dur_ms"], r["status"]])
            else:
                continue
            n += 1
        return n

    def prune(self, now_ms: int | None = None) -> None:
        cutoff = (now_ms or int(time.time() * 1000)) - self.window_hours * 3_600_000
        self.routed = {k: kept for k, v in self.routed.items()
                       if (kept := [o for o in v if o[0] >= cutoff])}
        self.unrouted = [o for o in self.unrouted if o[0] >= cutoff]

    def save(self) -> None:
        self.prune()
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with open(self.path, "w", encoding="utf-8") as f:
            json.dump({"version": STORE_VERSION, "window_hours": self.window_hours,
                       "routed": self.routed, "unrouted": self.unrouted}, f)


# ---------------------------------------------------------------- scan merge

def _percentile(sorted_vals: list, q: float) -> float:
    idx = min(len(sorted_vals) - 1, round(q * (len(sorted_vals) - 1)))
    return round(sorted_vals[idx], 1)


def _pattern_regex(canonical: str) -> re.Pattern:
    parts = [("[^/]+" if seg == ":p" else re.escape(seg))
             for seg in canonical.split("/")]
    return re.compile("^" + "/".join(parts) + "$")


def merge_runtime(graph: Graph, runtime_path: str, config: dict,
                  now_ms: int | None = None) -> dict | None:
    """Overlay runtime.json onto endpoint nodes; returns stats or None if
    the store file does not exist."""
    if not os.path.exists(runtime_path):
        return None
    store = RuntimeStore(runtime_path)
    store.prune(now_ms)
    window = store.window_hours
    rt_cfg = config.get("runtime", {})
    p95_threshold = rt_cfg.get("p95_ms_threshold", 1000)
    err_threshold = rt_cfg.get("error_rate_threshold", 0.02)

    endpoints = graph.nodes_of(NodeType.ENDPOINT)
    # static severity snapshot BEFORE runtime flags are added, for hot_fragile
    static_high = {ep.id: any(f["severity"] in ("high", "critical")
                              for f in ep.risk_flags) for ep in endpoints}

    with_traffic = 0
    for ep in endpoints:
        method = ep.label.split(" ", 1)[0]
        canon = canonicalize(ep.meta["raw_path"])
        obs = list(store.routed.get(f"{method} {canon}", []))
        confidence = "certain"
        pattern = _pattern_regex(canon)
        fallback = [[ts, dur, status] for ts, m, path, dur, status in store.unrouted
                    if m == method and pattern.match(path.split("?")[0].rstrip("/") or "/")]
        if fallback:
            if not obs:
                confidence = "probable"
            obs.extend(fallback)

        if not obs:
            ep.meta["runtime"] = {"req_count": 0, "window_hours": window}
            continue
        with_traffic += 1
        durations = sorted(o[1] for o in obs)
        errors = sum(1 for o in obs if o[2] >= 500)
        ep.meta["runtime"] = {
            "req_count": len(obs),
            "p50_ms": _percentile(durations, 0.50),
            "p95_ms": _percentile(durations, 0.95),
            "error_rate": round(errors / len(obs), 4),
            "window_hours": window,
            "confidence": confidence,
        }

    # ---- runtime flags -----------------------------------------------------
    flags_added = 0
    counts = sorted(ep.meta["runtime"]["req_count"] for ep in endpoints
                    if ep.meta.get("runtime", {}).get("req_count", 0) > 0)
    hot_threshold = _percentile(counts, 0.90) if counts else None

    for ep in endpoints:
        rt = ep.meta.get("runtime")
        if rt is None:
            continue
        if rt["req_count"] == 0:
            if any(f["code"] == "unused_endpoint" for f in ep.risk_flags):
                ep.risk_flags = [f for f in ep.risk_flags
                                 if f["code"] != "unused_endpoint"]
                graph.flag_node(ep.id, RiskFlag(
                    code="confirmed_dead", severity="medium", category="contract",
                    message="Statically unreferenced and received no traffic "
                            f"in the last {window:g}h",
                    evidence=f"{ep.file}:{ep.line} 0 requests in window, "
                             "no frontend call site",
                    suggestion="Remove the endpoint, or document the external "
                               "consumer that keeps it alive",
                ))
                flags_added += 1
            continue

        if rt["p95_ms"] > p95_threshold:
            graph.flag_node(ep.id, RiskFlag(
                code="high_latency", severity="medium", category="operational",
                message=f"p95 latency {rt['p95_ms']}ms exceeds "
                        f"{p95_threshold}ms",
                evidence=f"{ep.file}:{ep.line} p95 {rt['p95_ms']}ms over "
                         f"{rt['req_count']} requests in {window:g}h",
                suggestion="Profile the handler; cache or move slow work off "
                           "the request path",
            ))
            flags_added += 1
        if rt["error_rate"] > err_threshold:
            graph.flag_node(ep.id, RiskFlag(
                code="high_error_rate", severity="high", category="operational",
                message=f"{rt['error_rate'] * 100:.1f}% of responses are 5xx",
                evidence=f"{ep.file}:{ep.line} error rate "
                         f"{rt['error_rate'] * 100:.1f}% over "
                         f"{rt['req_count']} requests in {window:g}h",
                suggestion="Inspect server logs for the failing responses; "
                           "add error handling where they originate",
            ))
            flags_added += 1
        if (hot_threshold is not None and rt["req_count"] >= hot_threshold
                and static_high[ep.id]):
            codes = sorted({f["code"] for f in ep.risk_flags
                            if f["severity"] in ("high", "critical")
                            and f["code"] not in ("high_latency",
                                                  "high_error_rate")})
            graph.flag_node(ep.id, RiskFlag(
                code="hot_fragile", severity="critical", category="operational",
                message=f"Top-decile traffic ({rt['req_count']} req/"
                        f"{window:g}h) on an endpoint with high/critical "
                        "static flags",
                evidence=f"{ep.file}:{ep.line} carries {', '.join(codes)} "
                         f"while serving {rt['req_count']} requests",
                suggestion="Fix the static issues here first — this endpoint "
                           "has the highest blast radius in the system",
            ))
            flags_added += 1

    return {"endpoints_with_traffic": with_traffic, "runtime_flags": flags_added}


# ------------------------------------------------------------------- receiver

class _OTLPHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        if self.path.split("?")[0] != "/v1/traces":
            self._reply(404, {"error": "only POST /v1/traces is supported"})
            return
        ctype = self.headers.get("Content-Type", "")
        if "application/json" not in ctype:
            self._reply(415, {"error": "only the OTLP JSON encoding is "
                                       "supported; set OTEL_EXPORTER_OTLP_PROTOCOL=http/json"})
            return
        try:
            length = int(self.headers.get("Content-Length", 0))
            payload = json.loads(self.rfile.read(length))
        except (ValueError, json.JSONDecodeError):
            self._reply(400, {"error": "invalid JSON body"})
            return
        records = parse_otlp_traces(payload)
        n = self.server.store.ingest(records)
        self.server.store.save()
        self.server.spans_seen += n
        self._reply(200, {"partialSuccess": {}})

    def _reply(self, code: int, body: dict):
        data = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def log_message(self, fmt, *args):
        pass  # one summary line per batch is printed by the store owner


def make_collector(store: RuntimeStore, port: int) -> ThreadingHTTPServer:
    server = ThreadingHTTPServer(("", port), _OTLPHandler)
    server.store = store
    server.spans_seen = 0
    return server


def run_collector(root: str, port: int, window_hours: float) -> int:
    store_path = os.path.join(os.path.abspath(root), ".wiremap", "runtime.json")
    store = RuntimeStore(store_path, window_hours)
    server = make_collector(store, port)
    print(f"wiremap · collecting OTLP/JSON traces\n"
          f"  listening : http://localhost:{port}/v1/traces\n"
          f"  store     : {store_path}\n"
          f"  window    : {window_hours:g}h rolling\n\n"
          f"  point your app at it, e.g.:\n"
          f"    OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:{port} \\\n"
          f"    OTEL_EXPORTER_OTLP_PROTOCOL=http/json \\\n"
          f"    opentelemetry-instrument --traces_exporter otlp uvicorn app.main:app\n\n"
          f"  Ctrl-C to stop")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        store.save()
        print(f"\n  stored {server.spans_seen} spans -> {store_path}")
    return 0
