"""Scan history & trends (ROADMAP-v2 6.5)."""
import json
import os
import shutil

from wiremap import cli
from wiremap.graph import Graph, Node, NodeType
from wiremap.history import (flag_churn, graph_hash, load_history,
                             record_snapshot, snapshot)

from conftest import DEMO_DIR


def _graph(nodes):
    g = Graph()
    for nid, risk, flags in nodes:
        n = g.add_node(Node(id=nid, type=NodeType.ENDPOINT, label=nid,
                            file="f.py", line=1, risk_score=risk))
        for code, sev in flags:
            n.risk_flags.append({"code": code, "severity": sev,
                                 "category": "x", "message": "", "evidence": "",
                                 "suggestion": ""})
    return g


class TestSnapshot:
    def test_fields(self):
        g = _graph([("ep:A", 60.0, [("sql_injection_risk", "critical")]),
                    ("ep:B", 5.0, [])])
        s = snapshot(g, now=1_800_000_000)
        assert s["total_risk"] == 65.0
        assert s["nodes"] == 2
        assert s["flags_total"] == 1
        assert s["flags_critical"] == 1
        assert s["by_code"] == {"sql_injection_risk": 1}
        assert s["iso"].endswith("Z")

    def test_graph_hash_stable_and_sensitive(self):
        a = _graph([("ep:A", 10.0, [("missing_auth", "high")])])
        b = _graph([("ep:A", 99.0, [("missing_auth", "high")])])  # score-only
        c = _graph([("ep:A", 10.0, [("sql_injection_risk", "critical")])])
        assert graph_hash(a) == graph_hash(b)     # risk_score not hashed
        assert graph_hash(a) != graph_hash(c)     # flag set changes hash


class TestRecord:
    def test_appends_and_loads(self, tmp_path):
        out = str(tmp_path)
        record_snapshot(out, _graph([("ep:A", 10.0, [])]), now=1)
        record_snapshot(out, _graph([("ep:A", 10.0, [("x", "high")])]), now=2)
        hist = load_history(out)
        assert len(hist) == 2
        assert hist[0]["ts"] == 1 and hist[1]["ts"] == 2

    def test_unchanged_graph_skipped(self, tmp_path):
        out = str(tmp_path)
        g = _graph([("ep:A", 10.0, [("x", "high")])])
        assert record_snapshot(out, g, now=1) is not None
        assert record_snapshot(out, g, now=2) is None   # same hash -> skip
        assert len(load_history(out)) == 1

    def test_cap(self, tmp_path):
        from wiremap.history import HISTORY_CAP
        out = str(tmp_path)
        for i in range(HISTORY_CAP + 20):
            record_snapshot(out, _graph([("ep:A", float(i), [("c", "low")]),
                                         (f"ep:{i}", 1.0, [])]), now=i)
        hist = load_history(out)
        assert len(hist) == HISTORY_CAP
        assert hist[-1]["ts"] == HISTORY_CAP + 19    # newest kept

    def test_corrupt_history_ignored(self, tmp_path):
        (tmp_path / "history.json").write_text("{oops")
        assert load_history(str(tmp_path)) == []
        assert record_snapshot(str(tmp_path), _graph([("ep:A", 1.0, [])])) \
            is not None


class TestChurn:
    def test_introduced_and_resolved(self):
        hist = [{"total_risk": 10.0, "by_code": {"missing_auth": 1}},
                {"total_risk": 30.0,
                 "by_code": {"sql_injection_risk": 1, "no_timeout": 2}}]
        c = flag_churn(hist)
        assert c["introduced"] == ["no_timeout", "sql_injection_risk"]
        assert c["resolved"] == ["missing_auth"]
        assert c["risk_delta"] == 20.0

    def test_single_snapshot_no_churn(self):
        assert flag_churn([{"total_risk": 1.0, "by_code": {}}]) == {
            "introduced": [], "resolved": [], "risk_delta": 0.0}


class TestScanIntegration:
    def test_scan_records_history_and_injects_viewer(self, tmp_path, capsys):
        demo = tmp_path / "demo"
        shutil.copytree(DEMO_DIR, demo, ignore=shutil.ignore_patterns(".wiremap"))
        assert cli.main(["scan", str(demo)]) == 0
        capsys.readouterr()
        hist = load_history(str(demo / ".wiremap"))
        assert len(hist) == 1 and hist[0]["flags_total"] > 0

        html = (demo / ".wiremap" / "wiremap.html").read_text(encoding="utf-8")
        assert "__HISTORY_JSON__" not in html
        assert hist[0]["graph_hash"] in html

    def test_rescan_unchanged_keeps_one_point(self, tmp_path, capsys):
        demo = tmp_path / "demo"
        shutil.copytree(DEMO_DIR, demo, ignore=shutil.ignore_patterns(".wiremap"))
        cli.main(["scan", str(demo)])
        cli.main(["scan", str(demo)])
        capsys.readouterr()
        assert len(load_history(str(demo / ".wiremap"))) == 1

    def test_source_change_adds_point(self, tmp_path, capsys):
        demo = tmp_path / "demo"
        shutil.copytree(DEMO_DIR, demo, ignore=shutil.ignore_patterns(".wiremap"))
        cli.main(["scan", str(demo)])
        target = demo / "backend" / "app" / "main.py"
        target.write_text(target.read_text() + """

@app.post("/api/danger")
def danger(payload: dict):
    return db.execute(f"DELETE FROM x WHERE id = {payload['id']}")
""")
        cli.main(["scan", str(demo)])
        capsys.readouterr()
        hist = load_history(str(demo / ".wiremap"))
        assert len(hist) == 2
        assert hist[1]["total_risk"] > hist[0]["total_risk"]
