"""Cross-function SQL-injection taint (ROADMAP-v2 6.1).

A request parameter that flows into raw SQL built in a *different* function
(up to 2 hops on the qualified call graph) must flag; parameterized
downstream queries must not.
"""
from wiremap.graph import Graph, NodeType
from wiremap.extractors.python_backend import (FunctionInfo, _sql_taint,
                                               extract_backend)

from conftest import BACKEND_FIXTURE


def _sql_flags(graph, ep_id):
    return [f for f in graph.nodes[ep_id].risk_flags
            if f["code"] == "sql_injection_risk"]


class TestFixtureTaint:
    def test_one_hop_taint_flagged(self, backend_graph):
        flags = _sql_flags(backend_graph, "ep:GET /report")
        assert flags
        assert "run_report" in flags[0]["message"]
        assert "passes `where`" in flags[0]["evidence"]

    def test_two_hop_taint_flagged(self, backend_graph):
        flags = _sql_flags(backend_graph, "ep:GET /deep-report")
        assert flags
        # witness is where the SQL is actually built, not the middle hop
        assert "run_report" in flags[0]["message"]

    def test_parameterized_downstream_not_flagged(self, backend_graph):
        assert _sql_flags(backend_graph, "ep:GET /safe-report") == []

    def test_direct_fstring_still_single_flag(self, backend_graph):
        # DELETE handler builds SQL itself -> the direct flag, not a
        # cross-function duplicate
        flags = _sql_flags(backend_graph, "ep:DELETE /items/{item_id}")
        assert len(flags) == 1
        assert "Request data flows" not in flags[0]["message"]


class TestTaintResolver:
    def _fn(self, module, qname, **kw):
        return FunctionInfo(qname=qname, module=module, file=f"{module}.py",
                            line=1, end_line=9, **kw)

    def test_direct_sink(self):
        fns = {"app.s.build": self._fn(
            "app.s", "build", params=["q"], sql_sink_params=["q"],
            raw_sql_interp=[(5, "f'... {q}'")])}
        assert _sql_taint("app.s.build", fns, {}) == ("q", "app.s.build", 5)

    def test_no_taint_when_param_not_interpolated(self):
        fns = {"app.s.build": self._fn("app.s", "build", params=["q"])}
        assert _sql_taint("app.s.build", fns, {}) is None

    def test_forward_resolves_through_imports(self):
        fns = {
            "app.h.handler": self._fn(
                "app.h", "handler", params=["where"],
                forwards=[{"callee": "run", "arg_params": ["where"]}]),
            "app.s.run": self._fn(
                "app.s", "run", params=["clause"], sql_sink_params=["clause"],
                raw_sql_interp=[(3, "f'...{clause}'")]),
        }
        imports = {"app.h": {"run": "app.s.run"}}
        got = _sql_taint("app.h.handler", fns, imports)
        assert got == ("where", "app.s.run", 3)

    def test_depth_limit_stops_at_two_hops(self):
        # h -> a -> b -> sink  (3 hops) must NOT resolve at depth 2
        def fwd(name):
            return [{"callee": name, "arg_params": ["x"]}]
        fns = {
            "m.h": self._fn("m", "h", params=["x"], forwards=fwd("a")),
            "m.a": self._fn("m", "a", params=["x"], forwards=fwd("b")),
            "m.b": self._fn("m", "b", params=["x"], forwards=fwd("c")),
            "m.c": self._fn("m", "c", params=["x"], sql_sink_params=["x"],
                            raw_sql_interp=[(1, "s")]),
        }
        imports = {"m": {}}
        assert _sql_taint("m.h", fns, imports) is None
        assert _sql_taint("m.h", fns, imports, depth=3) is not None

    def test_cycle_safe(self):
        fns = {
            "m.a": self._fn("m", "a", params=["x"],
                            forwards=[{"callee": "b", "arg_params": ["x"]}]),
            "m.b": self._fn("m", "b", params=["x"],
                            forwards=[{"callee": "a", "arg_params": ["x"]}]),
        }
        assert _sql_taint("m.a", fns, {"m": {}}) is None      # no infinite loop

    def test_position_must_match_sink(self):
        # handler forwards its param at position 0, but the sink is on the
        # callee's position 1 -> no taint
        fns = {
            "m.h": self._fn("m", "h", params=["tainted"],
                            forwards=[{"callee": "run",
                                       "arg_params": ["tainted", None]}]),
            "m.run": self._fn("m", "run", params=["safe", "q"],
                              sql_sink_params=["q"],
                              raw_sql_interp=[(2, "s")]),
        }
        assert _sql_taint("m.h", fns, {"m": {}}) is None
