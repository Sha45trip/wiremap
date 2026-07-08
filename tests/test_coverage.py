"""Coverage mapping (ROADMAP 2.2): report parsing, body-pct math, flags.

Coverage reports are fabricated relative to the extracted node line ranges,
so the tests stay valid if fixture line numbers shift.
"""
import json
import shutil

import pytest

from wiremap import cli
from wiremap.coverage import apply_coverage, load_coverage
from wiremap.graph import Graph, NodeType
from wiremap.extractors.python_backend import extract_backend

from conftest import BACKEND_FIXTURE, DEMO_DIR

EP_GET_ITEM = "ep:GET /items/{item_id}"
EP_CREATE = "ep:POST /items"
EP_HEALTH = "ep:GET /api/v2/health"


@pytest.fixture
def graph():
    g = Graph()
    extract_backend(BACKEND_FIXTURE, g)
    return g


def body_lines(node, end_key: str) -> list[int]:
    return list(range(node.line + 1, node.meta[end_key] + 1))


def report_for(files: dict) -> dict:
    """{path: (executed, missing)} -> parsed-report shape (keys normalized
    to forward slashes, as load_coverage produces)."""
    return {path.replace("\\", "/"): {"executed": set(ex), "missing": set(mi)}
            for path, (ex, mi) in files.items()}


def flags(node) -> dict[str, str]:
    return {f["code"]: f["severity"] for f in node.risk_flags}


class TestLoadCoverage:
    def test_parses_coverage_py_json(self, tmp_path):
        p = tmp_path / "coverage.json"
        p.write_text(json.dumps({"files": {
            "app\\main.py": {"executed_lines": [1, 2], "missing_lines": [3]},
        }}))
        cov = load_coverage(str(p))
        assert cov["app/main.py"]["executed"] == {1, 2}
        assert cov["app/main.py"]["missing"] == {3}

    def test_rejects_non_coverage_json(self, tmp_path):
        p = tmp_path / "other.json"
        p.write_text('{"nodes": []}')
        with pytest.raises(ValueError, match="coverage.py JSON"):
            load_coverage(str(p))


class TestApplyCoverage:
    def test_fully_covered_handler_no_flag(self, graph):
        ep = graph.nodes[EP_GET_ITEM]
        body = body_lines(ep, "handler_end_line")
        apply_coverage(graph, report_for({ep.file: (body, [])}))
        assert ep.meta["coverage_pct"] == 100.0
        assert "untested_handler" not in flags(ep)

    def test_zero_coverage_flags_high(self, graph):
        ep = graph.nodes[EP_CREATE]
        body = body_lines(ep, "handler_end_line")
        stats = apply_coverage(graph, report_for({ep.file: ([], body)}))
        assert ep.meta["coverage_pct"] == 0.0
        assert flags(ep)["untested_handler"] == "high"
        assert stats["untested_handlers"] == 1

    def test_partial_coverage_flags_medium(self, graph):
        ep = graph.nodes[EP_GET_ITEM]
        body = body_lines(ep, "handler_end_line")
        assert len(body) >= 3
        apply_coverage(graph, report_for({ep.file: (body[:1], body[1:])}))
        assert 0 < ep.meta["coverage_pct"] < 50
        assert flags(ep)["untested_handler"] == "medium"

    def test_at_least_half_covered_no_flag(self, graph):
        ep = graph.nodes[EP_GET_ITEM]
        body = body_lines(ep, "handler_end_line")
        half = (len(body) + 1) // 2
        apply_coverage(graph, report_for({ep.file: (body[:half], body[half:])}))
        assert ep.meta["coverage_pct"] >= 50
        assert "untested_handler" not in flags(ep)

    def test_function_nodes_get_pct_but_never_flag(self, graph):
        fn = graph.nodes["fn:load_items"]
        body = body_lines(fn, "end_line")
        apply_coverage(graph, report_for({fn.file: ([], body)}))
        assert fn.meta["coverage_pct"] == 0.0
        assert fn.risk_flags == []

    def test_file_absent_from_report_stays_unmapped(self, graph):
        ep = graph.nodes[EP_HEALTH]
        apply_coverage(graph, report_for({"somewhere/else.py": ([1], [])}))
        assert "coverage_pct" not in ep.meta
        assert "untested_handler" not in flags(ep)

    def test_report_paths_may_be_absolute(self, graph):
        ep = graph.nodes[EP_CREATE]
        body = body_lines(ep, "handler_end_line")
        abs_key = "C:/repo/backend/" + ep.file.replace("\\", "/")
        apply_coverage(graph, report_for({abs_key: ([], body)}))
        assert ep.meta["coverage_pct"] == 0.0

    def test_ambiguous_suffix_match_is_skipped(self, graph):
        ep = graph.nodes[EP_CREATE]
        rel = ep.file.replace("\\", "/")
        apply_coverage(graph, report_for({
            f"a/{rel}": ([1], []), f"b/{rel}": ([1], []),
        }))
        assert "coverage_pct" not in ep.meta

    def test_comment_only_lines_do_not_count(self, graph):
        # only lines present in executed/missing are statements; a body whose
        # report lists no statements yields no pct and no flag
        ep = graph.nodes[EP_HEALTH]
        apply_coverage(graph, report_for({ep.file: ([], [])}))
        assert "coverage_pct" not in ep.meta
        assert "untested_handler" not in flags(ep)


class TestCliAcceptance:
    """ROADMAP 2.2 acceptance on demo/ with its canned coverage.json."""

    @pytest.fixture
    def demo_copy(self, tmp_path):
        dst = tmp_path / "demo"
        shutil.copytree(DEMO_DIR, dst,
                        ignore=shutil.ignore_patterns(".wiremap"))
        return dst

    def test_scan_with_coverage_flags_untested_handler(self, demo_copy, capsys):
        rc = cli.main(["scan", str(demo_copy),
                       "--coverage", str(demo_copy / "coverage.json")])
        assert rc == 0
        assert "coverage mapped" in capsys.readouterr().out

        with open(demo_copy / ".wiremap" / "graph.json", encoding="utf-8") as f:
            data = json.load(f)
        nodes = {n["id"]: n for n in data["nodes"]}

        create = nodes["ep:POST /api/orders"]
        assert create["meta"]["coverage_pct"] == 0.0
        assert any(fl["code"] == "untested_handler" and fl["severity"] == "high"
                   for fl in create["risk_flags"])

        get_order = nodes["ep:GET /api/orders/{order_id}"]
        assert get_order["meta"]["coverage_pct"] == 40.0
        assert any(fl["code"] == "untested_handler" and fl["severity"] == "medium"
                   for fl in get_order["risk_flags"])

        get_user = nodes["ep:GET /api/users/{user_id}"]
        assert get_user["meta"]["coverage_pct"] == 100.0
        assert not any(fl["code"] == "untested_handler"
                       for fl in get_user["risk_flags"])

        assert nodes["fn:log_event"]["meta"]["coverage_pct"] == 0.0

    def test_unreadable_coverage_report_exits_2(self, demo_copy, capsys):
        rc = cli.main(["scan", str(demo_copy), "--coverage", "missing.json"])
        assert rc == 2
        assert "cannot read coverage report" in capsys.readouterr().err

    def test_scan_without_coverage_unchanged(self, demo_copy, capsys):
        assert cli.main(["scan", str(demo_copy)]) == 0
        assert "coverage mapped" not in capsys.readouterr().out
