"""Golden-file test: full pipeline on demo/ vs tests/golden_graph.json.

If this fails after an intentional behavior change, inspect the diff, then
regenerate deliberately with:  python tests/regen_golden.py
Never regenerate blindly.
"""
import json
import os

from conftest import DEMO_DIR, GOLDEN_PATH, normalize, scan_pipeline


def build_normalized_demo_graph() -> dict:
    graph = scan_pipeline(
        backend_dir=os.path.join(DEMO_DIR, "backend"),
        frontend_dir=os.path.join(DEMO_DIR, "frontend"),
        config_root=DEMO_DIR,
    )
    return normalize(graph.to_dict())


def test_demo_scan_matches_golden():
    assert os.path.exists(GOLDEN_PATH), \
        "tests/golden_graph.json missing — run python tests/regen_golden.py"
    with open(GOLDEN_PATH, encoding="utf-8") as f:
        golden = json.load(f)
    actual = build_normalized_demo_graph()

    golden_node_ids = [n["id"] for n in golden["nodes"]]
    actual_node_ids = [n["id"] for n in actual["nodes"]]
    assert actual_node_ids == golden_node_ids, "node set changed"

    golden_edge_ids = [e["id"] for e in golden["edges"]]
    actual_edge_ids = [e["id"] for e in actual["edges"]]
    assert actual_edge_ids == golden_edge_ids, "edge set changed"

    for got, want in zip(actual["nodes"], golden["nodes"]):
        assert got == want, f"node {want['id']} changed"
    for got, want in zip(actual["edges"], golden["edges"]):
        assert got == want, f"edge {want['id']} changed"


def test_demo_scan_is_deterministic():
    assert build_normalized_demo_graph() == build_normalized_demo_graph()
