"""Module-qualified call graph (ROADMAP 3.4).

The planted collision: app.pricing.compute and app.tax.compute share a bare
name. Under bare-name resolution one overwrote the other; qualified
resolution must keep them apart and route each handler to its own target.
"""
import pytest

from wiremap.graph import EdgeType, NodeType
from wiremap.extractors.python_backend import (FunctionInfo, _module_of,
                                               _resolve_callee,
                                               _resolve_relative)


class TestModuleOf:
    @pytest.mark.parametrize("rel,expected", [
        ("app/services.py", ("app.services", False)),
        ("app\\services.py", ("app.services", False)),      # windows sep
        ("app/__init__.py", ("app", True)),
        ("main.py", ("main", False)),
        ("a/b/c.py", ("a.b.c", False)),
    ])
    def test_paths(self, rel, expected):
        assert _module_of(rel) == expected


class TestResolveRelative:
    def test_absolute_import(self):
        assert _resolve_relative("app.main", False, 0, "app.services") == "app.services"

    def test_single_dot(self):
        assert _resolve_relative("app.main", False, 1, "services") == "app.services"

    def test_single_dot_from_init(self):
        assert _resolve_relative("app", True, 1, "services") == "app.services"

    def test_double_dot(self):
        assert _resolve_relative("app.api.routes", False, 2, "core") == "app.core"

    def test_bare_dot_import(self):
        # `from . import tax` -> base is the package itself
        assert _resolve_relative("app.pricing", False, 1, None) == "app"

    def test_escaping_the_tree_returns_none(self):
        assert _resolve_relative("main", False, 2, "x") is None


class TestResolveCallee:
    def _functions(self):
        mk = lambda mod, q: FunctionInfo(qname=q, module=mod, file="f.py",
                                         line=1, end_line=2)
        return {
            "app.pricing.compute": mk("app.pricing", "compute"),
            "app.tax.compute": mk("app.tax", "compute"),
            "app.pricing.helper": mk("app.pricing", "helper"),
            "app.api.Handler.get": mk("app.api", "Handler.get"),
            "app.api.Handler.check": mk("app.api", "Handler.check"),
        }

    def test_imported_name(self):
        info = FunctionInfo(qname="h", module="app.calc", file="f", line=1, end_line=1)
        got = _resolve_callee("compute", info,
                              {"compute": "app.pricing.compute"},
                              self._functions())
        assert got == "app.pricing.compute"

    def test_same_module_beats_nothing(self):
        info = FunctionInfo(qname="compute", module="app.pricing",
                            file="f", line=1, end_line=1)
        assert _resolve_callee("helper", info, {}, self._functions()) \
            == "app.pricing.helper"

    def test_dotted_module_alias(self):
        info = FunctionInfo(qname="compute", module="app.pricing",
                            file="f", line=1, end_line=1)
        assert _resolve_callee("tax.compute", info, {"tax": "app.tax"},
                               self._functions()) == "app.tax.compute"

    def test_self_method(self):
        info = FunctionInfo(qname="Handler.get", module="app.api",
                            file="f", line=1, end_line=1)
        assert _resolve_callee("self.check", info, {}, self._functions()) \
            == "app.api.Handler.check"

    def test_unknown_name_unresolved(self):
        info = FunctionInfo(qname="h", module="app.calc", file="f",
                            line=1, end_line=1)
        assert _resolve_callee("execute", info, {}, self._functions()) is None

    def test_import_of_unknown_symbol_does_not_fall_through(self):
        # `from x import compute` where x isn't in the tree must NOT match
        # some other module's compute
        info = FunctionInfo(qname="h", module="app.calc", file="f",
                            line=1, end_line=1)
        assert _resolve_callee("compute", info,
                               {"compute": "vendor.lib.compute"},
                               self._functions()) is None


class TestCollisionFixture:
    def test_both_computes_exist_as_distinct_nodes(self, backend_graph):
        fn_ids = {n.id for n in backend_graph.nodes_of(NodeType.FUNCTION)}
        assert "fn:app.pricing.compute" in fn_ids
        assert "fn:app.tax.compute" in fn_ids

    def test_each_handler_wires_to_its_own_compute(self, backend_graph):
        edges = {e.id for e in backend_graph.edges_of(EdgeType.CALLS)}
        assert "ep:GET /calc/price->fn:app.pricing.compute" in edges
        assert "ep:GET /calc/tax->fn:app.tax.compute" in edges
        assert "ep:GET /calc/price->fn:app.tax.compute" not in edges
        assert "ep:GET /calc/tax->fn:app.pricing.compute" not in edges

    def test_same_module_and_dotted_calls_walked(self, backend_graph):
        edges = {e.id for e in backend_graph.edges_of(EdgeType.CALLS)}
        assert "fn:app.pricing.compute->fn:app.pricing.helper" in edges
        assert "fn:app.pricing.compute->fn:app.tax.compute" in edges

    def test_function_labels_stay_short(self, backend_graph):
        n = backend_graph.nodes["fn:app.pricing.compute"]
        assert n.label == "compute"
        assert n.meta["module"] == "app.pricing"

    def test_handler_meta_is_qualified(self, backend_graph):
        ep = backend_graph.nodes["ep:GET /calc/price"]
        assert ep.meta["handler"] == "app.calc.price"