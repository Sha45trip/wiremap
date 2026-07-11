"""Matcher: canonicalization rules and wire building on hand-built graphs."""
import pytest

from wiremap.graph import Graph, Node, NodeType, EdgeType
from wiremap.matcher import canonicalize, match


@pytest.mark.parametrize("raw,expected", [
    ("/users/{user_id}", "/users/:p"),                 # FastAPI
    ("/users/<int:user_id>", "/users/:p"),             # Flask converter
    ("/users/<user_id>/orders", "/users/:p/orders"),   # Flask bare
    ("/users/:param/orders", "/users/:p/orders"),      # frontend substitution
    ("/users/:id", "/users/:p"),                       # express style
    ("/users/123", "/users/:p"),                       # numeric literal segment
    ("/users/123/orders/456", "/users/:p/orders/:p"),
    ("/users/", "/users"),                             # trailing slash
    ("/users?page=2", "/users"),                       # query string
    ("/", "/"),
    ("/v2/things", "/v2/things"),                      # digits inside a segment stay
])
def test_canonicalize(raw, expected):
    assert canonicalize(raw) == expected


def _endpoint(method: str, path: str) -> Node:
    return Node(id=f"ep:{method} {path}", type=NodeType.ENDPOINT,
                label=f"{method} {path}", file="app.py", line=1,
                meta={"raw_path": path, "handler": "h", "framework": "fastapi",
                      "has_auth": True})


def _call(method: str, url: str, confidence: str = "certain",
          line: int = 1) -> Node:
    return Node(id=f"call:src/App.jsx:{line}", type=NodeType.API_CALL,
                label=f"{method} {url}", file="src/App.jsx", line=line,
                meta={"method": method, "url": url, "confidence": confidence,
                      "has_error_handling": True, "has_timeout": True})


def _flags(node: Node) -> set[str]:
    return {f["code"] for f in node.risk_flags}


class TestMatch:
    def test_literal_call_matches_templated_route(self):
        g = Graph()
        ep = g.add_node(_endpoint("GET", "/users/{user_id}"))
        call = g.add_node(_call("GET", "/users/42"))
        stats = match(g)
        assert stats == {"matched": 1, "orphan_calls": 0,
                         "unused_endpoints": 0, "discovery_guard": False}
        wires = g.edges_of(EdgeType.HTTP)
        assert len(wires) == 1
        assert wires[0].source == call.id and wires[0].target == ep.id

    def test_method_mismatch_does_not_match(self):
        g = Graph()
        g.add_node(_endpoint("GET", "/users"))
        call = g.add_node(_call("POST", "/users"))
        stats = match(g)
        assert stats["matched"] == 0
        assert "orphan_call" in _flags(call)

    def test_orphan_call_flagged(self):
        g = Graph()
        call = g.add_node(_call("GET", "/nowhere"))
        stats = match(g)
        assert stats["orphan_calls"] == 1
        flag = call.risk_flags[0]
        assert flag["code"] == "orphan_call"
        assert flag["severity"] == "high"
        assert "src/App.jsx:1" in flag["evidence"]

    def test_unused_endpoint_flagged(self):
        g = Graph()
        ep = g.add_node(_endpoint("GET", "/reports"))
        stats = match(g)
        assert stats["unused_endpoints"] == 1
        assert "unused_endpoint" in _flags(ep)

    def test_dynamic_url_gets_unresolvable_flag_not_orphan(self):
        g = Graph()
        call = g.add_node(_call("GET", "<dynamic>", confidence="inferred"))
        stats = match(g)
        assert stats["orphan_calls"] == 0
        assert _flags(call) == {"unresolvable_url"}
        assert not g.edges_of(EdgeType.HTTP)

    def test_call_confidence_propagates_to_wire(self):
        g = Graph()
        g.add_node(_endpoint("GET", "/items/{id}"))
        g.add_node(_call("GET", "/items/:param", confidence="probable"))
        match(g)
        wire = g.edges_of(EdgeType.HTTP)[0]
        assert wire.confidence.value == "probable"

    def test_matched_endpoint_not_marked_unused(self):
        g = Graph()
        ep = g.add_node(_endpoint("GET", "/users"))
        g.add_node(_call("GET", "/users"))
        match(g)
        assert "unused_endpoint" not in _flags(ep)


class TestDiscoveryGuard:
    """bench 4.1: mass orphans on unsupported stacks get downgraded."""

    def test_low_match_rate_downgrades_orphans(self):
        g = Graph()
        g.add_node(_endpoint("GET", "/only"))
        g.add_node(_call("GET", "/only", line=999))
        calls = [g.add_node(_call("GET", f"/missing/{i}", line=i))
                 for i in range(24)]
        stats = match(g)
        assert stats["discovery_guard"] is True
        flag = calls[0].risk_flags[0]
        assert flag["severity"] == "low"
        assert "route discovery may not cover" in flag["message"]

    def test_high_match_rate_keeps_orphans_high(self):
        g = Graph()
        for i in range(24):
            g.add_node(_endpoint("GET", f"/r{i}"))
            g.add_node(_call("GET", f"/r{i}", line=i))
        orphan = g.add_node(_call("GET", "/nowhere", line=999))
        stats = match(g)
        assert stats["discovery_guard"] is False
        assert orphan.risk_flags[0]["severity"] == "high"

    def test_small_apps_unaffected(self):
        g = Graph()
        orphan = g.add_node(_call("GET", "/nowhere"))
        stats = match(g)
        assert stats["discovery_guard"] is False
        assert orphan.risk_flags[0]["severity"] == "high"
