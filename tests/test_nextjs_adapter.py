"""Next.js API routes + tRPC adapter (ROADMAP-v2 5.4)."""
import json
import os
import shutil

import pytest

from wiremap import cli
from wiremap.graph import Graph, NodeType
from wiremap.extractors.nextjs_backend import extract_nextjs, _next_route_path

from conftest import FIXTURES

NEXT_FIXTURE = os.path.join(FIXTURES, "nextjs_app")


class TestNextPath:
    @pytest.mark.parametrize("rel,expected", [
        ("pages/api/users/[id].ts", ("/api/users/:id", "pages")),
        ("pages/api/health.ts", ("/api/health", "pages")),
        ("pages/api/index.ts", ("/api", "pages")),
        ("app/api/orders/route.ts", ("/api/orders", "app")),
        ("app/api/blog/[...slug]/route.ts", ("/api/blog/:slug", "app")),
        ("src/components/Button.tsx", None),
        ("pages/index.tsx", None),
    ])
    def test_paths(self, rel, expected):
        assert _next_route_path(rel) == expected


@pytest.fixture(scope="module")
def next_graph():
    g = Graph()
    extract_nextjs(NEXT_FIXTURE, g)
    return g


class TestNextRoutes:
    def test_all_routes(self, next_graph):
        eps = {n.id for n in next_graph.nodes_of(NodeType.ENDPOINT)}
        assert {"ep:GET /api/users/:id", "ep:DELETE /api/users/:id",
                "ep:GET /api/health", "ep:GET /api/orders",
                "ep:POST /api/orders"} <= eps

    def test_pages_method_switch(self, next_graph):
        eps = {n.id for n in next_graph.nodes_of(NodeType.ENDPOINT)}
        assert "ep:GET /api/users/:id" in eps
        assert "ep:DELETE /api/users/:id" in eps

    def test_app_router_exports(self, next_graph):
        ep = next_graph.nodes["ep:GET /api/orders"]
        assert ep.meta["framework"] == "nextjs"

    def test_unauthed_app_post_flagged(self, next_graph):
        codes = {f["code"] for f in
                 next_graph.nodes["ep:POST /api/orders"].risk_flags}
        assert "missing_auth" in codes

    def test_get_routes_clean(self, next_graph):
        assert next_graph.nodes["ep:GET /api/health"].risk_flags == []


class TestTrpc:
    def test_procedures_become_endpoints(self, next_graph):
        eps = {n.id for n in next_graph.nodes_of(NodeType.ENDPOINT)}
        assert "ep:QUERY /trpc#user.byId" in eps
        assert "ep:MUTATION /trpc#user.update" in eps
        assert "ep:QUERY /trpc#health" in eps

    def test_trpc_framework_tag(self, next_graph):
        assert next_graph.nodes["ep:QUERY /trpc#user.byId"].meta["framework"] \
            == "trpc"


class TestEndToEnd:
    def test_scan_wires_trpc_and_flags_orphan(self, tmp_path, capsys):
        app = tmp_path / "app"
        # single-tree app: routes + client together
        shutil.copytree(NEXT_FIXTURE, app / "frontend")
        assert cli.main(["scan", str(app), "--frontend",
                         str(app / "frontend"), "--backend",
                         str(app / "frontend")]) == 0
        out = capsys.readouterr().out
        assert "next/trpc routes" in out

        with open(app / ".wiremap" / "graph.json", encoding="utf-8") as f:
            data = json.load(f)
        calls = {n["meta"]["url"]: n for n in data["nodes"]
                 if n["type"] == "api_call"}
        wires = {(e["source"], e["target"]) for e in data["edges"]
                 if e["type"] == "http"}

        assert any(t == "ep:QUERY /trpc#user.byId" for _, t in wires)
        assert any(t == "ep:MUTATION /trpc#user.update" for _, t in wires)

        phantom = calls["/trpc#user.phantom"]
        assert phantom["meta"]["trpc"] is True
        assert any(f["code"] == "orphan_call" for f in phantom["risk_flags"])
        # library-managed: no error/timeout noise on trpc calls
        codes = {f["code"] for f in phantom["risk_flags"]}
        assert "no_error_handling" not in codes and "no_timeout" not in codes