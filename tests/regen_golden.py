"""Deliberately regenerate tests/golden_graph.json from a scan of demo/.

Run only after reviewing why the graph changed:  python tests/regen_golden.py
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from test_golden import build_normalized_demo_graph
from conftest import GOLDEN_PATH


def main() -> None:
    graph = build_normalized_demo_graph()
    with open(GOLDEN_PATH, "w", encoding="utf-8", newline="\n") as f:
        json.dump(graph, f, indent=2)
        f.write("\n")
    print(f"wrote {GOLDEN_PATH}: "
          f"{len(graph['nodes'])} nodes, {len(graph['edges'])} edges")


if __name__ == "__main__":
    main()
