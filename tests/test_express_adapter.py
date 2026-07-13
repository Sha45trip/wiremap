"""Express/Node backend adapter (ROADMAP-v2 5.2)."""
import os

import pytest

from wiremap.graph import Graph, Node, NodeType
from wiremap.extractors.express_backend import extract_express
from wiremap.matcher import match

from conftest import FIXTURES

EXPRESS_FIXTURE = os.path.join(FIXTURES, "express_app")


@pytest.fixture(scope="module")
def express_graph():
    g = Graph()
    extract_express(EXPRESS_FIXTURE, g)
    return g


def flags(graph, ep_id):
    return {f["code"] for f in graph.nodes[ep_id].risk_flags}


class TestRouteDiscovery:
    def test_all_routes_found(self, express_graph):
        eps = {n.id for n in express_graph.nodes_of(NodeType.ENDPOINT)}
        assert eps == {
            "ep:GET /health",
            "ep:POST /webhook",
            "ep:GET /api/users",           # cross-file require() mount
            "ep:GET /api/users/:id",
            "ep:POST /api/users",
            "ep:DELETE /api/users/:id",
        }

    def test_framework_and_evidence(self, express_graph):
        ep = express_graph.nodes["ep:GET /api/users/:id"]
        assert ep.meta["framework"] == "express"
        assert ep.file == "routes/users.js"
        assert ep.meta["handler"] == "getUser"

    def test_inline_handler_labeled(self, express_graph):
        assert express_graph.nodes["ep:GET /health"].meta["handler"] \
            == "<inline>"

    def test_non_express_client_calls_ignored(self, express_graph):
        # notexpress.js: client.get("/some/key") must not become a route
        assert not any("/some/key" in n.id
                       for n in express_graph.nodes_of(NodeType.ENDPOINT))


class TestAuth:
    def test_auth_middleware_detected(self, express_graph):
        ep = express_graph.nodes["ep:POST /api/users"]
        assert ep.meta["has_auth"] is True
        assert "missing_auth" not in flags(express_graph, "ep:POST /api/users")

    def test_unauthed_mutations_flagged(self, express_graph):
        # /webhook is on `app`, which has no auth middleware
        assert "missing_auth" in flags(express_graph, "ep:POST /webhook")

    def test_router_use_auth_guards_all_routes(self, express_graph):
        # router.use(requireAuth) protects every route on the users router
        # (6.3), so the DELETE mutation must NOT flag
        assert express_graph.nodes["ep:DELETE /api/users/:id"].meta[
            "has_auth"] is True
        assert "missing_auth" not in flags(express_graph,
                                           "ep:DELETE /api/users/:id")

    def test_gets_never_flagged(self, express_graph):
        assert flags(express_graph, "ep:GET /health") == set()


class TestMatcherIntegration:
    def test_express_param_route_matches_frontend_call(self, express_graph):
        g = Graph()
        g.nodes.update(express_graph.nodes)
        g.add_node(Node(
            id="call:src/App.jsx:1", type=NodeType.API_CALL,
            label="GET /api/users/7", file="src/App.jsx", line=1,
            meta={"method": "GET", "url": "/api/users/7",
                  "confidence": "certain", "has_error_handling": True,
                  "has_timeout": True}))
        stats = match(g)
        assert stats["matched"] == 1
        wire = list(g.edges.values())[0]
        assert wire.target == "ep:GET /api/users/:id"