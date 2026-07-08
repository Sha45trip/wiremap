"""Team-mode server (ROADMAP 3.1): routes, auth, re-scans.

Each test runs a real TeamServer on an ephemeral port against a demo copy.
"""
import importlib.util
import json
import shutil
import threading
import time
import urllib.error
import urllib.request

import pytest

from wiremap.server import TeamServer

from conftest import DEMO_DIR, PROJECT_ROOT

TOKEN = "s3cret"


@pytest.fixture
def demo(tmp_path):
    dst = tmp_path / "demo"
    shutil.copytree(DEMO_DIR, dst, ignore=shutil.ignore_patterns(".wiremap"))
    return dst


def _serve(demo, token=None, rescan_interval=0):
    srv = TeamServer(("", 0), root=str(demo), out_dir=str(demo / ".wiremap"),
                     token=token, rescan_interval=rescan_interval)
    srv.rescan()
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    return srv


def _req(srv, path, method="GET", body=None, ctype="application/json",
         token=None):
    headers = {}
    if body is not None:
        headers["Content-Type"] = ctype
    if token:
        headers["Authorization"] = f"Bearer {token}"
    req = urllib.request.Request(
        f"http://localhost:{srv.server_address[1]}{path}",
        data=body, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


class TestReadRoutes:
    def test_viewer_and_graph_served(self, demo):
        srv = _serve(demo)
        try:
            code, html = _req(srv, "/wiremap.html")
            assert code == 200 and b"wiremap" in html
            code, graph = _req(srv, "/graph.json")
            assert code == 200
            assert json.loads(graph)["nodes"]
        finally:
            srv.shutdown()

    def test_root_redirects_to_viewer(self, demo):
        srv = _serve(demo)
        try:
            code, _ = _req(srv, "/")     # urllib follows the 302
            assert code == 200
        finally:
            srv.shutdown()

    def test_healthz(self, demo):
        srv = _serve(demo)
        try:
            code, body = _req(srv, "/healthz")
            health = json.loads(body)
            assert code == 200 and health["status"] == "ok"
            assert health["last_scan"] is not None
        finally:
            srv.shutdown()

    def test_unknown_path_404(self, demo):
        srv = _serve(demo)
        try:
            assert _req(srv, "/etc/passwd")[0] == 404
        finally:
            srv.shutdown()

    def test_read_routes_never_need_token(self, demo):
        srv = _serve(demo, token=TOKEN)
        try:
            assert _req(srv, "/wiremap.html")[0] == 200
            assert _req(srv, "/graph.json")[0] == 200
        finally:
            srv.shutdown()


def _replay_batches():
    spec = importlib.util.spec_from_file_location(
        "replay_spans", f"{PROJECT_ROOT}/demo/replay_spans.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.build_batches()


class TestAuth:
    def test_mutating_routes_open_without_token(self, demo):
        srv = _serve(demo)
        try:
            assert _req(srv, "/rescan", "POST", b"{}")[0] == 200
        finally:
            srv.shutdown()

    def test_mutating_routes_401_without_bearer(self, demo):
        srv = _serve(demo, token=TOKEN)
        try:
            assert _req(srv, "/rescan", "POST", b"{}")[0] == 401
            assert _req(srv, "/v1/traces", "POST", b"{}")[0] == 401
            assert _req(srv, "/rescan", "POST", b"{}", token="wrong")[0] == 401
        finally:
            srv.shutdown()

    def test_correct_bearer_accepted(self, demo):
        srv = _serve(demo, token=TOKEN)
        try:
            code, body = _req(srv, "/rescan", "POST", b"{}", token=TOKEN)
            assert code == 200 and json.loads(body)["rescanned"] is True
        finally:
            srv.shutdown()


class TestRescan:
    def test_rescan_picks_up_source_changes(self, demo):
        srv = _serve(demo)
        try:
            target = demo / "backend" / "app" / "main.py"
            target.write_text(target.read_text() + """

@app.get("/api/new-route")
def new_route(user=Depends(get_current_user)):
    return {}
""")
            code, body = _req(srv, "/rescan", "POST", b"{}")
            assert code == 200 and json.loads(body)["routes"] == 5
            _, graph = _req(srv, "/graph.json")
            assert "ep:GET /api/new-route" in {
                n["id"] for n in json.loads(graph)["nodes"]}
        finally:
            srv.shutdown()

    def test_traces_then_rescan_shows_runtime(self, demo):
        srv = _serve(demo, token=TOKEN)
        try:
            for batch in _replay_batches():
                code, _ = _req(srv, "/v1/traces", "POST",
                               json.dumps(batch).encode(), token=TOKEN)
                assert code == 200
            _req(srv, "/rescan", "POST", b"{}", token=TOKEN)
            _, graph = _req(srv, "/graph.json")
            nodes = {n["id"]: n for n in json.loads(graph)["nodes"]}
            orders = nodes["ep:POST /api/orders"]
            assert orders["meta"]["runtime"]["req_count"] == 60
            assert any(f["code"] == "hot_fragile" for f in orders["risk_flags"])
        finally:
            srv.shutdown()

    def test_traces_reject_non_json(self, demo):
        srv = _serve(demo)
        try:
            code, _ = _req(srv, "/v1/traces", "POST", b"\x00",
                           ctype="application/x-protobuf")
            assert code == 415
        finally:
            srv.shutdown()

    def test_interval_loop_rescans(self, demo):
        srv = _serve(demo, rescan_interval=0.2)
        try:
            first = srv.last_scan
            srv.start_rescan_loop()
            deadline = time.time() + 5
            while srv.last_scan == first and time.time() < deadline:
                time.sleep(0.05)
            assert srv.last_scan != first, "interval loop never re-scanned"
        finally:
            srv.shutdown()
