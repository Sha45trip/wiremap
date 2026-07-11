"""Cross-OS determinism (ROADMAP-v2 4.0): graph output must not embed the
host OS path separator — ids, files, and evidence feed the CI diff, which
compares graphs produced on different machines."""
from conftest import DEMO_DIR, scan_pipeline
import os


def test_no_backslashes_anywhere_in_graph():
    graph = scan_pipeline(
        backend_dir=os.path.join(DEMO_DIR, "backend"),
        frontend_dir=os.path.join(DEMO_DIR, "frontend"),
        config_root=DEMO_DIR,
    )

    def walk(value, where):
        if isinstance(value, str):
            assert "\\" not in value, f"backslash in {where}: {value!r}"
        elif isinstance(value, list):
            for i, v in enumerate(value):
                walk(v, f"{where}[{i}]")
        elif isinstance(value, dict):
            for k, v in value.items():
                walk(v, f"{where}.{k}")

    walk(graph.to_dict(), "graph")


def test_fixture_scan_ids_are_portable(backend_graph, frontend_graph):
    for g in (backend_graph, frontend_graph):
        for node in g.nodes.values():
            assert "\\" not in node.id
            assert "\\" not in node.file