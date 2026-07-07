"""Risk engine: config loading, composite scoring, hub detection, inheritance."""
from dataclasses import asdict

from wiremap.graph import (Graph, Node, Edge, NodeType, EdgeType, RiskFlag)
from wiremap.risk import DEFAULT_CONFIG, load_config, score


def _node(nid: str, ntype: NodeType = NodeType.ENDPOINT) -> Node:
    return Node(id=nid, type=ntype, label=nid, file="f.py", line=1)


def _flag(severity: str, category: str) -> dict:
    return asdict(RiskFlag(code="x", severity=severity, category=category,
                           message="m", evidence="f.py:1", suggestion="s"))


class TestLoadConfig:
    def test_defaults_when_no_yaml(self, tmp_path):
        assert load_config(str(tmp_path)) == DEFAULT_CONFIG

    def test_yaml_overrides_merge_with_defaults(self, tmp_path):
        (tmp_path / "wiremap.yaml").write_text(
            "weights:\n  security: 5.0\nhub_fanin_threshold: 2\n")
        cfg = load_config(str(tmp_path))
        assert cfg["weights"]["security"] == 5.0
        assert cfg["weights"]["quality"] == 1.0          # default preserved
        assert cfg["severity"] == DEFAULT_CONFIG["severity"]
        assert cfg["hub_fanin_threshold"] == 2


class TestScoring:
    def test_critical_security_flag_maxes_out(self):
        g = Graph()
        n = g.add_node(_node("ep:X"))
        n.risk_flags.append(_flag("critical", "security"))
        score(g, DEFAULT_CONFIG)
        assert n.risk_score == 100.0                     # 10 * 2.0 / 20 * 100

    def test_low_quality_flag_scores_five(self):
        g = Graph()
        n = g.add_node(_node("ep:X"))
        n.risk_flags.append(_flag("low", "quality"))
        score(g, DEFAULT_CONFIG)
        assert n.risk_score == 5.0                       # 1 * 1.0 / 20 * 100

    def test_flags_accumulate_and_cap_at_100(self):
        g = Graph()
        n = g.add_node(_node("ep:X"))
        for _ in range(5):
            n.risk_flags.append(_flag("critical", "security"))
        score(g, DEFAULT_CONFIG)
        assert n.risk_score == 100.0

    def test_stats_returned(self):
        g = Graph()
        n = g.add_node(_node("ep:X"))
        n.risk_flags.append(_flag("critical", "security"))
        n.risk_flags.append(_flag("low", "quality"))
        stats = score(g, DEFAULT_CONFIG)
        assert stats["total_flags"] == 2
        assert stats["critical_flags"] == 1
        assert stats["max_risk_score"] == 100.0


class TestEdgeInheritance:
    def test_wire_inherits_80_percent_of_riskier_end(self):
        g = Graph()
        call = g.add_node(_node("call:a", NodeType.API_CALL))
        ep = g.add_node(_node("ep:b"))
        ep.risk_flags.append(_flag("critical", "security"))  # -> 100
        g.add_edge(Edge(id="w", source=call.id, target=ep.id, type=EdgeType.HTTP))
        score(g, DEFAULT_CONFIG)
        assert g.edges["w"].risk_score == 80.0

    def test_edge_own_flags_win_when_higher(self):
        g = Graph()
        g.add_node(_node("call:a", NodeType.API_CALL))
        g.add_node(_node("ep:b"))
        e = g.add_edge(Edge(id="w", source="call:a", target="ep:b",
                            type=EdgeType.HTTP))
        e.risk_flags.append(_flag("critical", "security"))
        score(g, DEFAULT_CONFIG)
        assert e.risk_score == 100.0


class TestHubDetection:
    def _graph_with_fanin(self, count: int) -> tuple[Graph, Node]:
        g = Graph()
        hub = g.add_node(_node("fn:hub", NodeType.FUNCTION))
        for i in range(count):
            src = g.add_node(_node(f"ep:{i}"))
            g.add_edge(Edge(id=f"e{i}", source=src.id, target=hub.id,
                            type=EdgeType.CALLS))
        return g, hub

    def test_hub_flagged_at_threshold(self):
        g, hub = self._graph_with_fanin(3)
        score(g, DEFAULT_CONFIG)
        codes = {f["code"] for f in hub.risk_flags}
        assert "hub_function" in codes

    def test_no_hub_flag_below_threshold(self):
        g, hub = self._graph_with_fanin(2)
        score(g, DEFAULT_CONFIG)
        assert hub.risk_flags == []

    def test_hub_flag_only_applies_to_functions(self):
        g = Graph()
        model = g.add_node(_node("db:M", NodeType.DB_MODEL))
        for i in range(4):
            src = g.add_node(_node(f"fn:{i}", NodeType.FUNCTION))
            g.add_edge(Edge(id=f"q{i}", source=src.id, target=model.id,
                            type=EdgeType.QUERIES))
        score(g, DEFAULT_CONFIG)
        assert model.risk_flags == []
