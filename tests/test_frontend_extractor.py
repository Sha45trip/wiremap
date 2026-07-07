"""Frontend extractor: call-site discovery, URL/method resolution, features.

Includes a regression guard for the Phase 1 fluent-chain bug: a .catch()
several links down the promise chain must count as error handling.
"""
from wiremap.graph import Graph, Node, NodeType, EdgeType


def find_call(graph: Graph, method: str, url: str) -> Node:
    for n in graph.nodes_of(NodeType.API_CALL):
        if n.meta["method"] == method and n.meta["url"] == url:
            return n
    raise AssertionError(f"no api_call node for {method} {url}")


def flags(node: Node) -> set[str]:
    return {f["code"] for f in node.risk_flags}


class TestCallDiscovery:
    def test_all_call_sites_found(self, frontend_graph):
        assert len(frontend_graph.nodes_of(NodeType.API_CALL)) == 7

    def test_calls_linked_to_enclosing_function(self, frontend_graph):
        call = find_call(frontend_graph, "GET", "/widgets")
        links = [e for e in frontend_graph.edges_of(EdgeType.MAKES_CALL)
                 if e.target == call.id]
        assert links, "call site has no makes_call edge"
        src = frontend_graph.nodes[links[0].source]
        assert src.type == NodeType.COMPONENT


class TestUrlAndMethodResolution:
    def test_literal_fetch_is_certain_get(self, frontend_graph):
        call = find_call(frontend_graph, "GET", "/widgets")
        assert call.meta["confidence"] == "certain"

    def test_method_read_from_fetch_options(self, frontend_graph):
        find_call(frontend_graph, "POST", "/items")

    def test_template_literal_becomes_param_probable(self, frontend_graph):
        call = find_call(frontend_graph, "GET", "/items/:param")
        assert call.meta["confidence"] == "probable"

    def test_string_concat_becomes_param_probable(self, frontend_graph):
        call = find_call(frontend_graph, "DELETE", "/items/:param")
        assert call.meta["confidence"] == "probable"

    def test_dynamic_url_marked_inferred(self, frontend_graph):
        call = find_call(frontend_graph, "GET", "<dynamic>")
        assert call.meta["confidence"] == "inferred"


class TestFeatureFlags:
    def test_bare_fetch_flags_both(self, frontend_graph):
        call = find_call(frontend_graph, "GET", "/widgets")
        assert {"no_error_handling", "no_timeout"} <= flags(call)

    def test_catch_deep_in_chain_counts_as_handled(self, frontend_graph):
        # fixtures/frontend_app/src/Safe.jsx `chained` — the Phase 1 bug class
        chained = [n for n in frontend_graph.nodes_of(NodeType.API_CALL)
                   if n.file.endswith("Safe.jsx") and n.meta["url"] == "/items"]
        assert chained, "Safe.jsx chained call not extracted"
        assert "no_error_handling" not in flags(chained[0])

    def test_try_catch_and_abort_signal_suppress_flags(self, frontend_graph):
        call = find_call(frontend_graph, "GET", "/api/v2/health")
        assert call.meta["has_error_handling"] is True
        assert call.meta["has_timeout"] is True
        assert flags(call) == set()

    def test_immediate_catch_suppresses_error_flag(self, frontend_graph):
        call = find_call(frontend_graph, "GET", "/items/:param")
        assert "no_error_handling" not in flags(call)
