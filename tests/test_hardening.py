"""Phase 7 hardening: per-user tokens, store rotation, version/release."""
import json
import threading
import urllib.error
import urllib.request

import pytest

import wiremap
from wiremap import cli
from wiremap.collector import MAX_OBS_PER_KEY, RuntimeStore
from wiremap.server import TeamServer, parse_tokens

from conftest import DEMO_DIR


class TestParseTokens:
    def test_labeled(self):
        assert parse_tokens("alice:t1,bob:t2") == {"t1": "alice", "t2": "bob"}

    def test_bare_tokens_get_labels(self):
        out = parse_tokens("t1,t2")
        assert set(out) == {"t1", "t2"}
        assert all(v.startswith("user") for v in out.values())

    def test_single_token_backcompat(self):
        assert parse_tokens(None, "solo") == {"solo": "default"}

    def test_combined_and_empty(self):
        assert parse_tokens("", None) == {}
        assert parse_tokens("a:t1", "t2") == {"t1": "a", "t2": "default"}

    def test_whitespace_tolerated(self):
        assert parse_tokens(" alice : t1 , bob:t2 ") == {"t1": "alice",
                                                         "t2": "bob"}


class TestMultiTokenAuth:
    @pytest.fixture
    def server(self, tmp_path):
        import shutil
        demo = tmp_path / "demo"
        shutil.copytree(DEMO_DIR, demo, ignore=shutil.ignore_patterns(".wiremap"))
        srv = TeamServer(("", 0), root=str(demo),
                         out_dir=str(demo / ".wiremap"),
                         tokens={"tok-a": "alice", "tok-b": "bob"})
        srv.rescan()
        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()
        yield srv
        srv.shutdown()

    def _rescan(self, srv, token=None):
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        req = urllib.request.Request(
            f"http://localhost:{srv.server_address[1]}/rescan",
            data=b"{}", headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req) as r:
                return r.status
        except urllib.error.HTTPError as e:
            return e.code

    def test_both_users_accepted(self, server):
        assert self._rescan(server, "tok-a") == 200
        assert self._rescan(server, "tok-b") == 200

    def test_unknown_token_rejected(self, server):
        assert self._rescan(server, "nope") == 401
        assert self._rescan(server, None) == 401

    def test_read_routes_stay_open(self, server):
        req = urllib.request.Request(
            f"http://localhost:{server.server_address[1]}/wiremap.html")
        with urllib.request.urlopen(req) as r:
            assert r.status == 200


class TestStoreRotation:
    def test_caps_observations_per_key(self, tmp_path):
        store = RuntimeStore(str(tmp_path / "r.json"))
        now = 1_800_000_000_000
        recs = [{"method": "GET", "route": "/a", "path": None, "status": 200,
                 "ts_ms": now - i, "dur_ms": 10.0, "trace_id": ""}
                for i in range(MAX_OBS_PER_KEY + 500)]
        store.ingest(recs)
        assert len(store.routed["GET /a"]) == MAX_OBS_PER_KEY + 500
        store.prune(now)          # cap applied on prune/save
        assert len(store.routed["GET /a"]) == MAX_OBS_PER_KEY

    def test_cap_keeps_most_recent(self, tmp_path):
        store = RuntimeStore(str(tmp_path / "r.json"))
        now = 1_800_000_000_000
        # oldest first, newest last; newest ts = now
        total = MAX_OBS_PER_KEY + 100
        recs = [{"method": "GET", "route": "/a", "path": None, "status": 200,
                 "ts_ms": now - (total - 1 - i), "dur_ms": 10.0,
                 "trace_id": ""} for i in range(total)]
        store.ingest(recs)
        store.prune(now)
        kept = store.routed["GET /a"]
        assert len(kept) == MAX_OBS_PER_KEY
        assert kept[-1][0] == now                  # newest retained


class TestRelease:
    def test_version_matches_package(self):
        assert wiremap.__version__ == "0.2.0"

    def test_cli_version_flag(self, capsys):
        with pytest.raises(SystemExit) as ex:
            cli.main(["--version"])
        assert ex.value.code == 0
        assert "0.2.0" in capsys.readouterr().out
