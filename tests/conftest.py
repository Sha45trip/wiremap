"""Shared helpers for the wiremap test suite."""
from __future__ import annotations

import os

import pytest

from wiremap.graph import Graph
from wiremap.extractors.python_backend import extract_backend
from wiremap.extractors.react_frontend import extract_frontend
from wiremap.matcher import match
from wiremap.risk import load_config, score

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(TESTS_DIR)
FIXTURES = os.path.join(TESTS_DIR, "fixtures")
BACKEND_FIXTURE = os.path.join(FIXTURES, "backend_app")
FRONTEND_FIXTURE = os.path.join(FIXTURES, "frontend_app")
DEMO_DIR = os.path.join(PROJECT_ROOT, "demo")
GOLDEN_PATH = os.path.join(TESTS_DIR, "golden_graph.json")


def scan_pipeline(backend_dir: str, frontend_dir: str, config_root: str) -> Graph:
    """Run the full extract -> match -> score pipeline without touching disk."""
    graph = Graph()
    extract_backend(backend_dir, graph)
    extract_frontend(frontend_dir, graph)
    match(graph)
    score(graph, load_config(config_root))
    return graph


def normalize(graph_dict: dict) -> dict:
    """Make a graph dict comparable across platforms and walk orders.

    - nodes/edges sorted by id
    - risk flags within each element sorted by (code, evidence)
    - Windows path separators normalized to / in every string value
      (ids, file fields, and evidence strings all embed relative paths)
    """
    def fix(value):
        if isinstance(value, str):
            return value.replace("\\", "/")
        if isinstance(value, list):
            return [fix(v) for v in value]
        if isinstance(value, dict):
            return {k: fix(v) for k, v in value.items()}
        return value

    out = fix(graph_dict)
    for elem in out["nodes"] + out["edges"]:
        elem["risk_flags"].sort(key=lambda f: (f["code"], f["evidence"]))
    out["nodes"].sort(key=lambda n: n["id"])
    out["edges"].sort(key=lambda e: e["id"])
    return out


@pytest.fixture(scope="session")
def backend_graph() -> Graph:
    graph = Graph()
    extract_backend(BACKEND_FIXTURE, graph)
    return graph


@pytest.fixture(scope="session")
def frontend_graph() -> Graph:
    graph = Graph()
    extract_frontend(FRONTEND_FIXTURE, graph)
    return graph


def node_flags(graph: Graph, node_id: str) -> set[str]:
    return {f["code"] for f in graph.nodes[node_id].risk_flags}
