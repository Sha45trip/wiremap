"""TypeScript response-type integration (ROADMAP-v2 5.5).

Declared generics (`axios.get<ItemView>`) resolve against local
interfaces/type aliases: required fields become the expected contract at
PROBABLE confidence; optional fields are tolerated missing; unknown
imported types fall back to the read heuristic.
"""
from wiremap.graph import Graph, NodeType
from wiremap.matcher import match

from test_contract import _ep


def _typed_call(graph, url):
    for n in graph.nodes_of(NodeType.API_CALL):
        if n.meta["url"] == url and n.file.endswith("Typed.tsx"):
            return n
    raise AssertionError(f"no Typed.tsx call for {url}")


class TestTypeExtraction:
    def test_interface_required_fields_probable(self, frontend_graph):
        call = _typed_call(frontend_graph, "/contract/item")
        assert call.meta["response_type"] == "ItemView"
        assert call.meta["expected_fields"] == ["id", "name", "price"]
        assert call.meta["fields_confidence"] == "probable"

    def test_optional_fields_split_out(self, frontend_graph):
        call = _typed_call(frontend_graph, "/contract/item")
        assert call.meta["optional_fields"] == ["discount"]

    def test_type_alias_object_supported(self, frontend_graph):
        call = _typed_call(frontend_graph, "/contract/item2")
        assert call.meta["response_type"] == "GhostView"
        assert call.meta["expected_fields"] == ["ghost_total", "id"]

    def test_unknown_imported_type_falls_back(self, frontend_graph):
        call = _typed_call(frontend_graph, "/contract/raw")
        assert "response_type" not in call.meta
        assert call.meta.get("fields_confidence") != "probable"


class TestTypedContract:
    def _matched(self, frontend_graph, backend_graph):
        g = Graph()
        g.nodes.update(backend_graph.nodes)
        g.nodes.update(frontend_graph.nodes)
        match(g)
        return g

    def test_required_ghost_field_flags(self, frontend_graph, backend_graph):
        g = self._matched(frontend_graph, backend_graph)
        call = _typed_call(g, "/contract/item2")
        flags = [f for f in call.risk_flags
                 if f["code"] == "contract_mismatch"]
        assert flags and "ghost_total" in flags[0]["message"]

    def test_optional_field_missing_backend_side_is_silent(
            self, frontend_graph, backend_graph):
        # ItemView requires id/name/price (all declared by ItemOut);
        # optional discount is NOT declared -> must not flag
        g = self._matched(frontend_graph, backend_graph)
        call = _typed_call(g, "/contract/item")
        assert not any(f["code"] == "contract_mismatch"
                       for f in call.risk_flags)

    def test_typed_beats_heuristic_reads(self):
        # a call with BOTH a generic and body reads uses the declared set
        g = Graph()
        g.add_node(_ep("GET", "/x", fields=["id"], model="X"))
        from wiremap.extractors.react_frontend import _assemble
        n = _assemble([{"method": "GET", "url": "/x", "confidence": "certain",
                        "component": "C", "component_line": 1, "line": 2,
                        "has_error_handling": True, "has_timeout": True,
                        "response_type": "Resp",
                        "expected_fields": ["from_reads"]}],
                      "a.tsx", g, set(), None,
                      {"Resp": {"required": ["id", "declared"],
                                "optional": []}})
        assert n == 1
        call = next(c for c in g.nodes_of(NodeType.API_CALL))
        assert call.meta["expected_fields"] == ["declared", "id"]
        assert call.meta["fields_confidence"] == "probable"