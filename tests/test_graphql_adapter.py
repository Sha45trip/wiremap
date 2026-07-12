"""GraphQL adapter (ROADMAP-v2 5.3): SDL, resolvers, client documents.

Root fields are endpoints at pseudo-path /graphql#<field>, so the standard
matcher provides orphan/unused semantics unchanged.
"""
import json
import os
import shutil

import pytest

from wiremap import cli
from wiremap.gql import camel, parse_document, sdl_root_fields
from wiremap.graph import Graph, NodeType, EdgeType
from wiremap.extractors.python_backend import extract_backend

from conftest import FIXTURES

GQL_FIXTURE = os.path.join(FIXTURES, "graphql_app")


class TestHelpers:
    @pytest.mark.parametrize("snake,expected", [
        ("user", "user"), ("order_history", "orderHistory"),
        ("a_b_c", "aBC"), ("already", "already"),
    ])
    def test_camel(self, snake, expected):
        assert camel(snake) == expected

    def test_sdl_root_fields(self):
        with open(os.path.join(GQL_FIXTURE, "backend", "schema.graphql"),
                  encoding="utf-8") as f:
            fields = {(k, n) for k, n, _ in sdl_root_fields(f.read())}
        assert fields == {("QUERY", "user"), ("QUERY", "orders"),
                          ("QUERY", "unusedReport"),
                          ("MUTATION", "createOrder")}

    def test_sdl_ignores_non_root_types(self):
        fields = sdl_root_fields("type User { id: ID! }")
        assert fields == []

    @pytest.mark.parametrize("doc,expected", [
        ("query GetUser($id: ID!) { user(id: $id) { name } }",
         ("QUERY", ["user"])),
        ("mutation M { createOrder(input: $i) { id } }",
         ("MUTATION", ["createOrder"])),
        ("{ user { name } orders { id } }", ("QUERY", ["user", "orders"])),
        ("query { latest: orders { ...bits } }", ("QUERY", ["orders"])),
        ("not graphql at all", None),
    ])
    def test_parse_document(self, doc, expected):
        assert parse_document(doc) == expected


@pytest.fixture(scope="module")
def gql_backend():
    g = Graph()
    extract_backend(os.path.join(GQL_FIXTURE, "backend"), g)
    return g


class TestResolverEndpoints:
    def test_strawberry_fields_discovered(self, gql_backend):
        eps = {n.id for n in gql_backend.nodes_of(NodeType.ENDPOINT)}
        assert "ep:QUERY /graphql#user" in eps
        assert "ep:QUERY /graphql#orderHistory" in eps        # snake -> camel
        assert "ep:MUTATION /graphql#createOrder" in eps

    def test_graphene_resolve_prefix(self, gql_backend):
        assert "ep:QUERY /graphql#legacyStats" in {
            n.id for n in gql_backend.nodes_of(NodeType.ENDPOINT)}

    def test_near_misses_are_not_fields(self, gql_backend):
        eps = {n.id for n in gql_backend.nodes_of(NodeType.ENDPOINT)}
        assert "ep:QUERY /graphql#helper" not in eps
        assert "ep:QUERY /graphql#notAResolver" not in eps

    def test_resolver_handler_and_framework(self, gql_backend):
        ep = gql_backend.nodes["ep:QUERY /graphql#user"]
        assert ep.meta["framework"] == "graphql"
        assert ep.meta["handler"] == "app.resolvers.Query.user"

    def test_call_graph_continues_from_resolver(self, gql_backend):
        edges = {e.id for e in gql_backend.edges_of(EdgeType.CALLS)}
        assert ("ep:QUERY /graphql#user"
                "->fn:app.services.load_user") in edges

    def test_static_flags_apply_to_resolvers(self, gql_backend):
        flags = {f["code"] for f in
                 gql_backend.nodes["ep:QUERY /graphql#orderHistory"].risk_flags}
        assert "sql_injection_risk" in flags


class TestEndToEnd:
    def test_scan_wires_orphans_and_unused(self, tmp_path, capsys):
        app = tmp_path / "app"
        shutil.copytree(GQL_FIXTURE, app)
        assert cli.main(["scan", str(app)]) == 0
        out = capsys.readouterr().out
        assert "graphql schema" in out

        with open(app / ".wiremap" / "graph.json", encoding="utf-8") as f:
            data = json.load(f)
        nodes = {n["id"]: n for n in data["nodes"]}
        wires = {(e["source"], e["target"]) for e in data["edges"]
                 if e["type"] == "http"}

        # matched wires: GET_USER doc -> resolver field; CREATE_ORDER;
        # aliased orders -> SDL-only field
        assert any(t == "ep:QUERY /graphql#user" for _, t in wires)
        assert any(t == "ep:MUTATION /graphql#createOrder" for _, t in wires)
        assert any(t == "ep:QUERY /graphql#orders" for _, t in wires)

        # planted orphan: phantomField is in no schema
        phantom = next(n for n in data["nodes"]
                       if n["type"] == "api_call"
                       and n["meta"]["url"] == "/graphql#phantomField")
        assert any(f["code"] == "orphan_call" for f in phantom["risk_flags"])
        assert phantom["meta"]["graphql"] is True

        # planted unused: unusedReport declared, never queried
        unused = nodes["ep:QUERY /graphql#unusedReport"]
        assert any(f["code"] == "unused_endpoint"
                   for f in unused["risk_flags"])
        assert unused["meta"]["framework"] == "graphql"

        # library-managed: gql docs never get error/timeout flags
        codes = {f["code"] for f in phantom["risk_flags"]}
        assert "no_error_handling" not in codes
        assert "no_timeout" not in codes

    def test_sdl_does_not_duplicate_resolver_endpoints(self, tmp_path,
                                                       capsys):
        app = tmp_path / "app"
        shutil.copytree(GQL_FIXTURE, app)
        cli.main(["scan", str(app)])
        capsys.readouterr()
        with open(app / ".wiremap" / "graph.json", encoding="utf-8") as f:
            nodes = [n for n in json.load(f)["nodes"]
                     if n["id"] == "ep:QUERY /graphql#user"]
        assert len(nodes) == 1
        assert nodes[0]["meta"]["handler"] == "app.resolvers.Query.user"