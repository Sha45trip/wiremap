"""Backend extractor: route discovery, call graph, ORM edges, static flags.

Every planted problem in fixtures/backend_app must fire; every near-miss
must not (precision beats recall).
"""
from wiremap.graph import NodeType, EdgeType

from conftest import node_flags

EP_GET_ITEM = "ep:GET /items/{item_id}"
EP_CREATE = "ep:POST /items"
EP_DELETE = "ep:DELETE /items/{item_id}"
EP_BRANCHY = "ep:GET /branchy"
EP_HEALTH = "ep:GET /api/v2/health"
EP_PING = "ep:POST /legacy/ping"


class TestRouteDiscovery:
    def test_all_routes_found(self, backend_graph):
        endpoint_ids = {n.id for n in backend_graph.nodes_of(NodeType.ENDPOINT)}
        assert endpoint_ids == {
            EP_GET_ITEM, EP_CREATE, EP_DELETE, EP_BRANCHY, EP_HEALTH, EP_PING,
            "ep:GET /contract/item", "ep:GET /contract/item2",
            "ep:GET /contract/items", "ep:GET /contract/raw",
            "ep:GET /calc/price", "ep:GET /calc/tax",
            "ep:GET /inv/items", "ep:POST /inv/items",
            "ep:GET /report", "ep:GET /deep-report", "ep:GET /safe-report",
        }

    def test_apirouter_prefix_applied(self, backend_graph):
        ep = backend_graph.nodes[EP_HEALTH]
        assert ep.meta["raw_path"] == "/api/v2/health"
        assert ep.meta["framework"] == "fastapi"

    def test_flask_route_method_from_kwarg(self, backend_graph):
        ep = backend_graph.nodes[EP_PING]
        assert ep.label == "POST /legacy/ping"
        assert ep.meta["framework"] == "flask"

    def test_endpoints_carry_evidence_location(self, backend_graph):
        ep = backend_graph.nodes[EP_CREATE]
        assert ep.file.replace("\\", "/") == "app/main.py"
        assert ep.line > 0


class TestCallGraphAndOrm:
    def test_handler_call_walked_to_service_function(self, backend_graph):
        assert "fn:app.services.load_items" in backend_graph.nodes
        edge_ids = set(backend_graph.edges)
        assert f"{EP_GET_ITEM}->fn:app.services.load_items" in edge_ids

    def test_orm_model_nodes_created(self, backend_graph):
        models = {n.label for n in backend_graph.nodes_of(NodeType.DB_MODEL)}
        assert "Item" in models

    def test_query_edge_from_service_to_model(self, backend_graph):
        queries = backend_graph.edges_of(EdgeType.QUERIES)
        assert any(e.source == "fn:app.services.load_items"
                   and e.target == "db:Item" for e in queries)


class TestPlantedFlags:
    def test_missing_auth_fires_on_unauthed_post(self, backend_graph):
        assert "missing_auth" in node_flags(backend_graph, EP_CREATE)

    def test_missing_auth_fires_on_flask_post(self, backend_graph):
        assert "missing_auth" in node_flags(backend_graph, EP_PING)

    def test_no_error_handling_fires_on_bare_io(self, backend_graph):
        assert "no_error_handling" in node_flags(backend_graph, EP_CREATE)

    def test_sql_injection_fires_on_fstring_execute(self, backend_graph):
        assert "sql_injection_risk" in node_flags(backend_graph, EP_DELETE)

    def test_high_complexity_fires_above_threshold(self, backend_graph):
        assert "high_complexity" in node_flags(backend_graph, EP_BRANCHY)

    def test_flags_carry_evidence_and_suggestion(self, backend_graph):
        for flag in backend_graph.nodes[EP_CREATE].risk_flags:
            assert ":" in flag["evidence"], flag
            assert flag["suggestion"], flag


class TestUnparseableFiles:
    def test_syntax_error_file_skipped_not_fatal(self, tmp_path):
        # found by the 4.1 corpus: real repos contain py2/broken files
        from wiremap.graph import Graph
        from wiremap.extractors.python_backend import extract_backend
        (tmp_path / "broken.py").write_text("def f(:\n  pass")
        (tmp_path / "ok.py").write_text(
            "from fastapi import FastAPI\napp = FastAPI()\n\n"
            "@app.get('/ok')\ndef ok():\n    return {}\n")
        g = Graph()
        stats = extract_backend(str(tmp_path), g)
        assert stats["files_parsed"] == 2
        assert "ep:GET /ok" in g.nodes


class TestNearMisses:
    def test_no_missing_auth_when_depends_present(self, backend_graph):
        assert "missing_auth" not in node_flags(backend_graph, EP_DELETE)

    def test_no_missing_auth_on_get(self, backend_graph):
        assert "missing_auth" not in node_flags(backend_graph, EP_GET_ITEM)

    def test_no_error_handling_flag_absent_when_io_in_try(self, backend_graph):
        assert "no_error_handling" not in node_flags(backend_graph, EP_GET_ITEM)

    def test_no_sql_injection_on_parameterized_execute(self, backend_graph):
        assert "sql_injection_risk" not in node_flags(backend_graph, EP_CREATE)

    def test_no_high_complexity_on_simple_handler(self, backend_graph):
        assert "high_complexity" not in node_flags(backend_graph, EP_HEALTH)
