"""Router/decorator-scope auth modeling (ROADMAP-v2 6.3).

Kills the biggest missing_auth false-positive class: mutating endpoints
guarded at the router level (FastAPI APIRouter dependencies=, Express
router.use(auth)) or by a per-route decorator dependencies=.
"""
from wiremap.graph import NodeType


def flags(graph, ep_id):
    return {f["code"] for f in graph.nodes[ep_id].risk_flags}


class TestFastAPIRouterScope:
    def test_router_dependencies_guard_all_routes(self, backend_graph):
        # secure = APIRouter(dependencies=[Depends(get_current_user)])
        for ep in ("ep:POST /secure/thing", "ep:DELETE /secure/thing/{tid}"):
            assert backend_graph.nodes[ep].meta["has_auth"] is True
            assert "missing_auth" not in flags(backend_graph, ep)

    def test_unguarded_router_still_flags(self, backend_graph):
        ep = "ep:POST /open/thing"
        assert backend_graph.nodes[ep].meta["has_auth"] is False
        assert "missing_auth" in flags(backend_graph, ep)

    def test_per_route_decorator_dependencies_guard(self, backend_graph):
        # @open_router.post("/decorated", dependencies=[Depends(...)])
        ep = "ep:POST /open/decorated"
        assert backend_graph.nodes[ep].meta["has_auth"] is True
        assert "missing_auth" not in flags(backend_graph, ep)

    def test_cross_file_auth_type_alias_guards(self, backend_graph):
        # user: CurrentUser where CurrentUser = Annotated[.., Depends(auth)]
        # is defined in deps.py (another file) -> resolved at assembly
        ep = "ep:POST /open/annotated"
        assert backend_graph.nodes[ep].meta["has_auth"] is True
        assert "missing_auth" not in flags(backend_graph, ep)

    def test_non_auth_dependency_does_not_falsely_guard(self, backend_graph):
        # sanity: /open/thing has no dependencies at all -> unguarded
        assert backend_graph.nodes["ep:POST /open/thing"].meta[
            "has_auth"] is False


class TestExpressRouterScope:
    def test_router_use_auth_guards_routes(self):
        import os
        from wiremap.graph import Graph
        from wiremap.extractors.express_backend import extract_express
        from conftest import FIXTURES
        g = Graph()
        extract_express(os.path.join(FIXTURES, "express_app"), g)
        # every users-router route is guarded by router.use(requireAuth)
        for ep in g.nodes_of(NodeType.ENDPOINT):
            if ep.id.startswith("ep:") and "/api/users" in ep.id:
                assert ep.meta["has_auth"] is True
                assert "missing_auth" not in {f["code"] for f in ep.risk_flags}