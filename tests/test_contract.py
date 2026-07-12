"""Contract checking (ROADMAP 2.4): response models vs frontend field reads.

Precision rule under test throughout: contract_mismatch only fires when the
backend field set is CERTAIN (declared bare-Name model) and the missing
frontend field name is exact.
"""
import json
import shutil

import pytest

from wiremap import cli
from wiremap.graph import Graph, Node, NodeType
from wiremap.matcher import match

from conftest import DEMO_DIR


def find_call(graph, url):
    for n in graph.nodes_of(NodeType.API_CALL):
        if n.meta["url"] == url:
            return n
    raise AssertionError(f"no api_call for {url}")


class TestBackendResponseFields:
    def test_return_annotation_yields_certain_fields(self, backend_graph):
        ep = backend_graph.nodes["ep:GET /contract/item"]
        assert ep.meta["response_model"] == "ItemOut"
        assert ep.meta["response_fields"] == ["id", "name", "price"]

    def test_response_model_kwarg_yields_certain_fields(self, backend_graph):
        ep = backend_graph.nodes["ep:GET /contract/item2"]
        assert ep.meta["response_fields"] == ["id", "name", "price"]

    def test_inherited_fields_included(self, backend_graph):
        # `id` lives on ItemBase, not ItemOut itself
        ep = backend_graph.nodes["ep:GET /contract/item"]
        assert "id" in ep.meta["response_fields"]

    def test_subscripted_model_not_certain(self, backend_graph):
        ep = backend_graph.nodes["ep:GET /contract/items"]
        assert "response_fields" not in ep.meta

    def test_no_model_no_fields(self, backend_graph):
        ep = backend_graph.nodes["ep:GET /contract/raw"]
        assert "response_fields" not in ep.meta


class TestFrontendExpectedFields:
    def test_awaited_axios_member_reads(self, frontend_graph):
        call = find_call(frontend_graph, "/contract/item")
        assert call.meta["expected_fields"] == ["id", "missing_field", "name"]
        assert call.meta["fields_confidence"] == "inferred"

    def test_fetch_then_chain_reads(self, frontend_graph):
        call = find_call(frontend_graph, "/contract/item2")
        # the .then-chained call (Contract.jsx `chained`)
        chained = [n for n in frontend_graph.nodes_of(NodeType.API_CALL)
                   if n.meta["url"] == "/contract/item2"
                   and n.meta["method"] == "GET"
                   and "phantom" in n.meta.get("expected_fields", [])]
        assert chained, "then-chain reads not detected"
        assert chained[0].meta["expected_fields"] == ["phantom", "price"]

    def test_two_step_await_fetch_reads(self, frontend_graph):
        call = find_call(frontend_graph, "/contract/raw")
        assert call.meta["expected_fields"] == ["whatever"]

    def test_builtin_methods_and_array_items_not_tracked(self, frontend_graph):
        call = find_call(frontend_graph, "/contract/items")
        assert "expected_fields" not in call.meta

    def test_inline_destructuring_reads(self, frontend_graph):
        inline = [n for n in frontend_graph.nodes_of(NodeType.API_CALL)
                  if n.meta["url"] == "/contract/item2"
                  and n.meta.get("expected_fields") == ["name", "price"]]
        assert inline, "inline (await ...).data destructuring not detected"

    def test_call_without_reads_has_no_fields(self, frontend_graph):
        call = find_call(frontend_graph, "/widgets")
        assert "expected_fields" not in call.meta


def _ep(method, path, fields=None, model="M"):
    meta = {"raw_path": path, "handler": "h"}
    if fields is not None:
        meta["response_model"] = model
        meta["response_fields"] = sorted(fields)
    return Node(id=f"ep:{method} {path}", type=NodeType.ENDPOINT,
                label=f"{method} {path}", file="app.py", line=1, meta=meta)


def _call(method, url, expected=None, line=1):
    meta = {"method": method, "url": url, "confidence": "certain",
            "has_error_handling": True, "has_timeout": True}
    if expected:
        meta["expected_fields"] = sorted(expected)
        meta["fields_confidence"] = "inferred"
    return Node(id=f"call:a.jsx:{line}", type=NodeType.API_CALL,
                label=f"{method} {url}", file="a.jsx", line=line, meta=meta)


class TestContractMismatchFlag:
    def test_fires_on_undeclared_field(self):
        g = Graph()
        g.add_node(_ep("GET", "/x", fields=["id", "total"], model="OrderOut"))
        call = g.add_node(_call("GET", "/x", expected=["id", "status"]))
        match(g)
        flags = [f for f in call.risk_flags if f["code"] == "contract_mismatch"]
        assert flags and flags[0]["severity"] == "high"
        assert "status" in flags[0]["message"]
        assert "OrderOut" in flags[0]["message"]
        assert "id" not in flags[0]["message"].split("—")[0].replace(
            "status", "")          # only the missing field is named as missing

    def test_silent_when_all_fields_declared(self):
        g = Graph()
        g.add_node(_ep("GET", "/x", fields=["id", "total"]))
        call = g.add_node(_call("GET", "/x", expected=["id", "total"]))
        match(g)
        assert not any(f["code"] == "contract_mismatch" for f in call.risk_flags)

    def test_silent_without_certain_backend_fields(self):
        g = Graph()
        g.add_node(_ep("GET", "/x"))                     # no declared model
        call = g.add_node(_call("GET", "/x", expected=["anything"]))
        match(g)
        assert not any(f["code"] == "contract_mismatch" for f in call.risk_flags)

    def test_silent_without_frontend_reads(self):
        g = Graph()
        g.add_node(_ep("GET", "/x", fields=["id"]))
        call = g.add_node(_call("GET", "/x"))
        match(g)
        assert not any(f["code"] == "contract_mismatch" for f in call.risk_flags)

    def test_unmatched_call_never_contract_flagged(self):
        g = Graph()
        call = g.add_node(_call("GET", "/nowhere", expected=["ghost"]))
        match(g)
        codes = {f["code"] for f in call.risk_flags}
        assert "contract_mismatch" not in codes and "orphan_call" in codes


class TestFixturePipeline:
    """Full extract->match on the fixture apps: exactly the planted
    mismatches fire, none of the near-misses do."""

    def test_planted_and_near_miss_flags(self, backend_graph, frontend_graph):
        g = Graph()
        g.nodes.update(backend_graph.nodes)
        g.nodes.update(frontend_graph.nodes)
        match(g)
        # aggregate across calls: several call sites share a URL
        mismatches: dict = {}
        for n in g.nodes_of(NodeType.API_CALL):
            for f in n.risk_flags:
                if f["code"] == "contract_mismatch":
                    mismatches.setdefault(n.meta["url"], []).append(f)
        assert set(mismatches) == {"/contract/item", "/contract/item2"}
        assert any("missing_field" in f["message"]
                   for f in mismatches["/contract/item"])
        # item2: Contract.jsx phantom read + Typed.tsx ghost_total type
        messages = " ".join(f["message"] for f in mismatches["/contract/item2"])
        assert "phantom" in messages and "ghost_total" in messages


class TestDemoAcceptance:
    def test_demo_scan_flags_status_read(self, tmp_path, capsys):
        demo = tmp_path / "demo"
        shutil.copytree(DEMO_DIR, demo, ignore=shutil.ignore_patterns(".wiremap"))
        assert cli.main(["scan", str(demo)]) == 0
        with open(demo / ".wiremap" / "graph.json", encoding="utf-8") as f:
            nodes = {n["id"]: n for n in json.load(f)["nodes"]}
        load_order = next(n for n in nodes.values()
                          if n["type"] == "api_call"
                          and n["meta"]["url"] == "/api/orders/:param")
        assert load_order["meta"]["expected_fields"] == ["id", "status", "total"]
        flags = [f for f in load_order["risk_flags"]
                 if f["code"] == "contract_mismatch"]
        assert len(flags) == 1
        assert "status" in flags[0]["message"]
        assert "OrderOut" in flags[0]["message"]
        ep = nodes["ep:GET /api/orders/{order_id}"]
        assert ep["meta"]["response_fields"] == ["id", "total"]