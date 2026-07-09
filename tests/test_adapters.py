"""Framework adapters (ROADMAP 3.3): Django, Flask blueprints, React Query,
OpenAPI clients. Matcher/risk/viewer stay framework-agnostic — everything
here only checks that adapters emit the same node/edge shapes.
"""
import json
import os
import shutil

import pytest

from wiremap import cli
from wiremap.graph import Graph, Node, NodeType, EdgeType
from wiremap.extractors.python_backend import extract_backend
from wiremap.openapi import ingest_endpoints, load_openapi, operation_map

from conftest import FIXTURES

DJANGO_FIXTURE = os.path.join(FIXTURES, "django_app")
OPENAPI_FIXTURE = os.path.join(FIXTURES, "openapi_app")


@pytest.fixture(scope="module")
def django_graph():
    g = Graph()
    extract_backend(DJANGO_FIXTURE, g)
    return g


class TestDjangoAdapter:
    def test_all_routes_discovered(self, django_graph):
        eps = {n.id for n in django_graph.nodes_of(NodeType.ENDPOINT)}
        assert eps == {
            "ep:GET /api/users/<int:user_id>",     # path() FBV via views.*
            "ep:GET /api/orders",                  # CBV .as_view() get
            "ep:POST /api/orders",                 # CBV .as_view() post
            "ep:GET /api/legacy/<slug>",           # re_path named group
            "ep:POST /api/health",                 # direct import + @require_POST
            "ep:GET /api/items",                   # DRF router list
            "ep:GET /api/items/<pk>",              # DRF router retrieve
        }
        assert all(n.meta["framework"] == "django"
                   for n in django_graph.nodes_of(NodeType.ENDPOINT))

    def test_include_prefix_applied(self, django_graph):
        # every route came from app.urls include()d under "api/"
        assert all(n.meta["raw_path"].startswith("/api/")
                   for n in django_graph.nodes_of(NodeType.ENDPOINT))

    def test_cbv_handlers_are_method_functions(self, django_graph):
        assert django_graph.nodes["ep:GET /api/orders"].meta["handler"] \
            == "app.views.OrderList.get"
        assert django_graph.nodes["ep:POST /api/orders"].meta["handler"] \
            == "app.views.OrderList.post"

    def test_login_required_detected(self, django_graph):
        assert django_graph.nodes[
            "ep:GET /api/users/<int:user_id>"].meta["has_auth"] is True

    def test_unauthed_cbv_post_flagged(self, django_graph):
        codes = {f["code"] for f in
                 django_graph.nodes["ep:POST /api/orders"].risk_flags}
        assert "missing_auth" in codes

    def test_require_post_pins_method(self, django_graph):
        eps = {n.id for n in django_graph.nodes_of(NodeType.ENDPOINT)}
        assert "ep:GET /api/health" not in eps

    def test_call_graph_walks_from_django_handlers(self, django_graph):
        edges = {e.id for e in django_graph.edges_of(EdgeType.CALLS)}
        assert "ep:POST /api/orders->fn:app.services.create_order_row" in edges
        assert ("ep:GET /api/users/<int:user_id>"
                "->fn:app.services.load_user") in edges

    def test_drf_handlers_are_viewset_actions(self, django_graph):
        assert django_graph.nodes["ep:GET /api/items"].meta["handler"] \
            == "app.viewsets.ItemViewSet.list"
        assert django_graph.nodes["ep:GET /api/items/<pk>"].meta["handler"] \
            == "app.viewsets.ItemViewSet.retrieve"


class TestFlaskBlueprints:
    def test_blueprint_prefix_and_multi_method(self, backend_graph):
        eps = {n.id for n in backend_graph.nodes_of(NodeType.ENDPOINT)}
        assert "ep:GET /inv/items" in eps
        assert "ep:POST /inv/items" in eps

    def test_blueprint_routes_are_flask(self, backend_graph):
        ep = backend_graph.nodes["ep:GET /inv/items"]
        assert ep.meta["framework"] == "flask"
        assert ep.meta["handler"] == "app.bp.inv_items"

    def test_unauthed_blueprint_post_flagged(self, backend_graph):
        codes = {f["code"] for f in
                 backend_graph.nodes["ep:POST /inv/items"].risk_flags}
        assert "missing_auth" in codes


class TestReactQuery:
    def _call(self, frontend_graph, url):
        for n in frontend_graph.nodes_of(NodeType.API_CALL):
            if n.meta["url"] == url:
                return n
        raise AssertionError(url)

    def test_query_fn_call_not_flagged_for_errors(self, frontend_graph):
        call = self._call(frontend_graph, "/items")
        rq = [n for n in frontend_graph.nodes_of(NodeType.API_CALL)
              if n.meta.get("react_query")]
        assert rq and rq[0].meta["has_error_handling"] is True
        assert not any(f["code"] == "no_error_handling"
                       for f in rq[0].risk_flags)

    def test_call_outside_hook_still_flags(self, frontend_graph):
        call = self._call(frontend_graph, "/query-miss")
        assert "react_query" not in call.meta
        assert any(f["code"] == "no_error_handling" for f in call.risk_flags)


class TestOpenAPI:
    def test_spec_ingested_as_certain_endpoints(self):
        g = Graph()
        spec, rel = load_openapi(OPENAPI_FIXTURE, OPENAPI_FIXTURE)
        stats = ingest_endpoints(spec, g, rel)
        assert stats == {"endpoints": 4}
        ep = g.nodes["ep:GET /pets/{petId}"]
        assert ep.meta["framework"] == "openapi"
        assert ep.meta["handler"] == "getPetById"

    def test_security_maps_to_has_auth(self):
        g = Graph()
        spec, rel = load_openapi(OPENAPI_FIXTURE, OPENAPI_FIXTURE)
        ingest_endpoints(spec, g, rel)
        assert g.nodes["ep:POST /pets"].meta["has_auth"] is True
        assert g.nodes["ep:GET /pets"].meta["has_auth"] is False

    def test_source_discovered_endpoint_wins(self):
        g = Graph()
        g.add_node(Node(id="ep:GET /pets", type=NodeType.ENDPOINT,
                        label="GET /pets", file="app.py", line=1,
                        meta={"framework": "fastapi", "raw_path": "/pets"}))
        spec, rel = load_openapi(OPENAPI_FIXTURE, OPENAPI_FIXTURE)
        stats = ingest_endpoints(spec, g, rel)
        assert stats == {"endpoints": 3}
        assert g.nodes["ep:GET /pets"].meta["framework"] == "fastapi"

    def test_operation_map(self):
        spec, _ = load_openapi(OPENAPI_FIXTURE, OPENAPI_FIXTURE)
        ops = operation_map(spec)
        assert ops["getPetById"] == {"method": "GET", "path": "/pets/{petId}"}
        assert len(ops) == 4

    def test_end_to_end_client_matching(self, tmp_path, capsys):
        app = tmp_path / "app"
        shutil.copytree(OPENAPI_FIXTURE, app)
        assert cli.main(["scan", str(app)]) == 0
        out = capsys.readouterr().out
        assert "openapi ingested  4 spec endpoints" in out

        with open(app / ".wiremap" / "graph.json", encoding="utf-8") as f:
            data = json.load(f)
        calls = {n["meta"].get("operation_id"): n for n in data["nodes"]
                 if n["type"] == "api_call"}
        assert set(calls) >= {"listPets", "getPetById"}
        assert "notARealOp" not in calls
        assert calls["getPetById"]["meta"]["url"] == "/pets/{petId}"
        assert calls["getPetById"]["meta"]["confidence"] == "probable"

        wires = {e["source"]: e["target"] for e in data["edges"]
                 if e["type"] == "http"}
        by_id = {n["id"]: n for n in data["nodes"]}
        targets = set(wires.values())
        assert "ep:GET /pets/{petId}" in targets
        assert "ep:GET /pets" in targets
        audit_flags = {f["code"] for f in
                       by_id["ep:GET /internal/audit"]["risk_flags"]}
        assert "unused_endpoint" in audit_flags