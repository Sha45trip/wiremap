"""Graph diff + merge gate (ROADMAP 3.2).

Unit tests on hand-built graph dicts, CLI behavior including exit codes,
and an end-to-end run: scan demo, plant a risky route, re-scan, diff.
"""
import json
import shutil

import pytest

from wiremap import cli
from wiremap.diff import diff_graphs, gate_failed

from conftest import DEMO_DIR


def node(nid, risk=0.0, flags=()):
    return {"id": nid, "type": "endpoint", "label": nid, "file": "f.py",
            "line": 1, "meta": {}, "risk_score": risk,
            "risk_flags": [{"code": c, "severity": s, "category": "quality",
                            "message": f"{c} msg", "evidence": "f.py:1",
                            "suggestion": "fix"} for c, s in flags]}


def edge(eid, risk=0.0, confidence="certain"):
    return {"id": eid, "source": "a", "target": "b", "type": "http",
            "confidence": confidence, "meta": {}, "risk_score": risk,
            "risk_flags": []}


def graph(nodes=(), edges=()):
    return {"nodes": list(nodes), "edges": list(edges)}


class TestDiffGraphs:
    def test_nodes_added_and_removed(self):
        d = diff_graphs(graph([node("ep:A"), node("ep:B")]),
                        graph([node("ep:B"), node("ep:C")]))
        assert d["nodes"]["added"] == ["ep:C"]
        assert d["nodes"]["removed"] == ["ep:A"]
        assert d["nodes"]["old_count"] == 2 and d["nodes"]["new_count"] == 2

    def test_wires_added_removed_changed(self):
        old = graph(edges=[edge("w1"), edge("w2", risk=10.0),
                           edge("w3", confidence="certain"), edge("w4")])
        new = graph(edges=[edge("w2", risk=50.0),
                           edge("w3", confidence="probable"), edge("w4"),
                           edge("w5")])
        d = diff_graphs(old, new)
        assert d["wires"]["added"] == ["w5"]
        assert d["wires"]["removed"] == ["w1"]
        changed = {c["id"]: c for c in d["wires"]["changed"]}
        assert set(changed) == {"w2", "w3"}
        assert changed["w2"]["old_risk"] == 10.0
        assert changed["w2"]["new_risk"] == 50.0
        assert changed["w3"]["new_confidence"] == "probable"

    def test_flag_keyed_by_owner_and_code(self):
        old = graph([node("ep:A", flags=[("missing_auth", "high")])])
        new = graph([node("ep:A", flags=[("missing_auth", "high"),
                                         ("sql_injection_risk", "critical")]),
                     node("ep:B", flags=[("missing_auth", "high")])])
        d = diff_graphs(old, new)
        introduced = {(f["owner"], f["code"]) for f in d["flags"]["introduced"]}
        assert introduced == {("ep:A", "sql_injection_risk"),
                              ("ep:B", "missing_auth")}
        assert d["flags"]["resolved"] == []

    def test_resolved_flags(self):
        old = graph([node("ep:A", flags=[("unused_endpoint", "low")])])
        new = graph([node("ep:A")])
        d = diff_graphs(old, new)
        assert [f["code"] for f in d["flags"]["resolved"]] == ["unused_endpoint"]

    def test_introduced_sorted_most_severe_first(self):
        old = graph([node("ep:A")])
        new = graph([node("ep:A", flags=[("low_thing", "low"),
                                         ("crit_thing", "critical"),
                                         ("high_thing", "high")])])
        codes = [f["code"] for f in diff_graphs(old, new)["flags"]["introduced"]]
        assert codes == ["crit_thing", "high_thing", "low_thing"]

    def test_risk_delta_sums_node_scores(self):
        d = diff_graphs(graph([node("a", risk=10.0), node("b", risk=5.5)]),
                        graph([node("a", risk=60.0)]))
        assert d["risk"] == {"old_total": 15.5, "new_total": 60.0,
                             "delta": 44.5}


class TestGate:
    def _diff_with(self, severity):
        return diff_graphs(graph([node("ep:A")]),
                           graph([node("ep:A", flags=[("x", severity)])]))

    def test_fails_at_threshold(self):
        assert gate_failed(self._diff_with("critical"), "critical")

    def test_fails_above_threshold(self):
        assert gate_failed(self._diff_with("critical"), "high")

    def test_passes_below_threshold(self):
        assert not gate_failed(self._diff_with("high"), "critical")

    def test_resolved_flags_never_fail(self):
        d = diff_graphs(graph([node("ep:A", flags=[("x", "critical")])]),
                        graph([node("ep:A")]))
        assert not gate_failed(d, "low")


class TestCli:
    @pytest.fixture
    def pair(self, tmp_path):
        old = tmp_path / "old.json"
        new = tmp_path / "new.json"
        old.write_text(json.dumps(graph([node("ep:A")])))
        new.write_text(json.dumps(graph(
            [node("ep:A", risk=60.0, flags=[("sql_injection_risk", "critical")])])))
        return str(old), str(new)

    def test_text_output_and_exit_zero(self, pair, capsys):
        assert cli.main(["diff", *pair]) == 0
        out = capsys.readouterr().out
        assert "wiremap diff" in out
        assert "sql_injection_risk" in out

    def test_md_output(self, pair, capsys):
        assert cli.main(["diff", *pair, "--format", "md"]) == 0
        out = capsys.readouterr().out
        assert "## wiremap diff" in out
        assert "| critical | `sql_injection_risk` | `ep:A` |" in out

    def test_json_output(self, pair, capsys):
        assert cli.main(["diff", *pair, "--format", "json"]) == 0
        d = json.loads(capsys.readouterr().out)
        assert d["risk"]["delta"] == 60.0

    def test_gate_exit_code_and_stderr(self, pair, capsys):
        assert cli.main(["diff", *pair, "--fail-on", "critical"]) == 1
        assert "severity >= critical" in capsys.readouterr().err

    def test_gate_passes_when_below(self, pair, capsys):
        old, new = pair
        assert cli.main(["diff", new, new, "--fail-on", "low"]) == 0

    def test_missing_file_exits_2(self, tmp_path, capsys):
        assert cli.main(["diff", "nope.json", "nope.json"]) == 2
        assert "cannot read graph" in capsys.readouterr().err

    def test_non_graph_json_exits_2(self, tmp_path, capsys):
        p = tmp_path / "x.json"
        p.write_text('{"files": {}}')
        assert cli.main(["diff", str(p), str(p)]) == 2
        assert "not a wiremap graph" in capsys.readouterr().err


class TestEndToEnd:
    """Scan demo, plant a risky route, re-scan, diff — the action's flow."""

    def test_planted_route_gates_the_merge(self, tmp_path, capsys):
        demo = tmp_path / "demo"
        shutil.copytree(DEMO_DIR, demo, ignore=shutil.ignore_patterns(".wiremap"))
        assert cli.main(["scan", str(demo)]) == 0
        base = tmp_path / "base.json"
        shutil.copy(demo / ".wiremap" / "graph.json", base)

        target = demo / "backend" / "app" / "main.py"
        target.write_text(target.read_text() + """

@app.post("/api/danger")
def danger(payload: dict):
    return db.execute(f"DELETE FROM x WHERE id = {payload['id']}")
""")
        assert cli.main(["scan", str(demo)]) == 0
        head = str(demo / ".wiremap" / "graph.json")
        capsys.readouterr()

        rc = cli.main(["diff", str(base), head, "--fail-on", "critical"])
        out, err = capsys.readouterr().out, capsys.readouterr().err
        assert rc == 1
        assert "ep:POST /api/danger" in out
        introduced_codes = [line for line in out.splitlines()
                            if "sql_injection_risk" in line
                            or "missing_auth" in line]
        assert len(introduced_codes) >= 2

    def test_no_changes_diff_is_quiet_and_passes(self, tmp_path, capsys):
        demo = tmp_path / "demo"
        shutil.copytree(DEMO_DIR, demo, ignore=shutil.ignore_patterns(".wiremap"))
        cli.main(["scan", str(demo)])
        g = str(demo / ".wiremap" / "graph.json")
        capsys.readouterr()
        assert cli.main(["diff", g, g, "--fail-on", "low"]) == 0
        assert cli.main(["diff", g, g, "--format", "md"]) == 0
        assert "No wire or flag changes." in capsys.readouterr().out