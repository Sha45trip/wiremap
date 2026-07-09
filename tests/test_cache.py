"""Incremental scans (ROADMAP 2.1): cache correctness and acceptance.

Acceptance criteria under test:
- second scan of an unchanged tree parses 0 files (and the CLI logs it)
- editing one file re-parses exactly that file
- --no-cache forces a full re-parse
- a cached scan produces a graph identical to an uncached one
"""
import json
import os
import shutil

import pytest

from wiremap import cli
from wiremap.cache import CACHE_VERSION, FileCache
from wiremap.graph import Graph
from wiremap.extractors.python_backend import extract_backend
from wiremap.extractors.react_frontend import extract_frontend
from wiremap.matcher import match
from wiremap.risk import DEFAULT_CONFIG, score

from conftest import BACKEND_FIXTURE, FRONTEND_FIXTURE, DEMO_DIR, normalize

N_BACKEND_FILES = 12  # tests/fixtures/backend_app/app/*.py
N_FRONTEND_FILES = 4  # tests/fixtures/frontend_app/src/*.jsx


@pytest.fixture
def project(tmp_path):
    """Editable copy of both fixture apps plus a cache location."""
    shutil.copytree(BACKEND_FIXTURE, tmp_path / "backend")
    shutil.copytree(FRONTEND_FIXTURE, tmp_path / "frontend")
    return tmp_path


def cache_path(project) -> str:
    return str(project / ".wiremap" / "cache.json")


def scan(project, cache: FileCache | None):
    graph = Graph()
    b = extract_backend(str(project / "backend"), graph, cache)
    f = extract_frontend(str(project / "frontend"), graph, cache)
    match(graph)
    score(graph, DEFAULT_CONFIG)
    return graph, b, f


def scan_saving(project):
    """One full scan cycle: load cache, scan, persist cache."""
    cache = FileCache(cache_path(project))
    result = scan(project, cache)
    cache.save()
    return result


class TestIncrementalScan:
    def test_second_scan_parses_zero_files(self, project):
        _, b1, f1 = scan_saving(project)
        assert b1["files_parsed"] == N_BACKEND_FILES
        assert f1["files_parsed"] == N_FRONTEND_FILES

        _, b2, f2 = scan_saving(project)
        assert b2["files_parsed"] == 0
        assert f2["files_parsed"] == 0
        assert b2["files_cached"] == N_BACKEND_FILES
        assert f2["files_cached"] == N_FRONTEND_FILES

    def test_cached_graph_identical_to_uncached(self, project):
        scan_saving(project)                       # warm the cache
        cached_graph, _, _ = scan_saving(project)  # fully from cache
        fresh_graph, _, _ = scan(project, None)    # no cache at all
        assert normalize(cached_graph.to_dict()) == normalize(fresh_graph.to_dict())

    def test_editing_one_file_reparses_exactly_that_file(self, project):
        scan_saving(project)
        target = project / "backend" / "app" / "services.py"
        target.write_text(target.read_text() + "\n# touched\n")

        _, b, f = scan_saving(project)
        assert b["files_parsed"] == 1
        assert b["files_cached"] == N_BACKEND_FILES - 1
        assert f["files_parsed"] == 0

    def test_edited_file_content_is_reflected(self, project):
        scan_saving(project)
        target = project / "backend" / "app" / "routers.py"
        target.write_text(target.read_text() + """

@router.get("/added")
def added():
    return {}
""")
        graph, _, _ = scan_saving(project)
        assert "ep:GET /api/v2/added" in graph.nodes

    def test_deleted_file_is_pruned(self, project):
        scan_saving(project)
        (project / "backend" / "app" / "legacy.py").unlink()

        graph, b, _ = scan_saving(project)
        assert "ep:POST /legacy/ping" not in graph.nodes
        assert b["files_parsed"] == 0                 # nothing changed content-wise
        with open(cache_path(project), encoding="utf-8") as f:
            stored = json.load(f)
        assert not any("legacy" in rel for rel in stored["sections"]["backend"])


class TestCacheRobustness:
    def test_corrupt_cache_file_is_ignored(self, project):
        os.makedirs(project / ".wiremap")
        (project / ".wiremap" / "cache.json").write_text("{not json!")
        _, b, f = scan_saving(project)                # must not raise
        assert b["files_parsed"] == N_BACKEND_FILES
        assert f["files_parsed"] == N_FRONTEND_FILES

    def test_version_mismatch_discards_cache(self, project):
        scan_saving(project)
        p = cache_path(project)
        with open(p, encoding="utf-8") as f:
            stored = json.load(f)
        stored["version"] = CACHE_VERSION - 1
        with open(p, "w", encoding="utf-8") as f:
            json.dump(stored, f)

        _, b, _ = scan_saving(project)
        assert b["files_parsed"] == N_BACKEND_FILES

    def test_no_cache_object_means_full_parse(self, project):
        _, b, f = scan(project, None)
        assert b["files_parsed"] == N_BACKEND_FILES
        assert f["files_parsed"] == N_FRONTEND_FILES
        assert b["files_cached"] == 0 and f["files_cached"] == 0


class TestCliAcceptance:
    """Mirrors the ROADMAP 2.1 acceptance script, on a copy of demo/."""

    @pytest.fixture
    def demo_copy(self, tmp_path):
        dst = tmp_path / "demo"
        shutil.copytree(DEMO_DIR, dst,
                        ignore=shutil.ignore_patterns(".wiremap"))
        return dst

    def test_second_cli_scan_logs_zero_parses(self, demo_copy, capsys):
        assert cli.main(["scan", str(demo_copy)]) == 0
        first = capsys.readouterr().out
        assert "files parsed      8" in first

        assert cli.main(["scan", str(demo_copy)]) == 0
        second = capsys.readouterr().out
        assert "files parsed      0  (8 unchanged, from cache)" in second

    def test_no_cache_flag_forces_full_parse(self, demo_copy, capsys):
        cli.main(["scan", str(demo_copy)])
        capsys.readouterr()
        cli.main(["scan", str(demo_copy), "--no-cache"])
        out = capsys.readouterr().out
        assert "files parsed      8  (cache disabled)" in out
