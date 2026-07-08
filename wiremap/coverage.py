"""Coverage mapping (ROADMAP 2.2).

Reads coverage.py JSON output (`coverage json`) and maps covered line
ranges onto endpoint-handler and function nodes as `meta.coverage_pct`.

Coverage is measured over a function's *body* lines (after the `def`),
because decorators and the def line execute at import time even when the
handler is never called — counting them would hide fully untested handlers.

Precision rules: a source file absent from the coverage report is left
unmapped (the report may simply not cover that tree), and ambiguous
suffix matches are skipped rather than guessed.
"""
from __future__ import annotations

import json

from .graph import Graph, NodeType, RiskFlag


def load_coverage(path: str) -> dict[str, dict]:
    """Parse a coverage.py JSON report into {path: {executed, missing}}."""
    with open(path, encoding="utf-8") as f:
        report = json.load(f)
    files = report.get("files") if isinstance(report, dict) else None
    if not isinstance(files, dict):
        raise ValueError(
            f"{path} does not look like coverage.py JSON output "
            "(expected a top-level 'files' map — produce it with `coverage json`)")
    out = {}
    for fpath, data in files.items():
        out[fpath.replace("\\", "/")] = {
            "executed": set(data.get("executed_lines", [])),
            "missing": set(data.get("missing_lines", [])),
        }
    return out


def _entry_for(cov: dict[str, dict], node_file: str) -> dict | None:
    """Match a graph node's root-relative path against report paths, which
    may be absolute or relative to wherever coverage was run."""
    nf = node_file.replace("\\", "/")
    if nf in cov:
        return cov[nf]
    suffix = "/" + nf
    matches = [v for k, v in cov.items() if k.endswith(suffix)]
    if len(matches) == 1:
        return matches[0]
    return None


def _body_coverage_pct(entry: dict, def_line: int, end_line: int) -> float | None:
    """Percent of executable body lines that ran; None if no body statements."""
    covered = total = 0
    for line in range(def_line + 1, end_line + 1):
        if line in entry["executed"]:
            covered += 1
            total += 1
        elif line in entry["missing"]:
            total += 1
    if total == 0:
        return None
    return round(covered / total * 100, 1)


def apply_coverage(graph: Graph, cov: dict[str, dict]) -> dict:
    """Attach meta.coverage_pct to handler/function nodes and flag
    endpoint handlers with 0% (high) or <50% (medium) body coverage."""
    mapped = flagged = 0
    for n in graph.nodes.values():
        if n.type == NodeType.ENDPOINT:
            end_line = n.meta.get("handler_end_line", 0)
        elif n.type == NodeType.FUNCTION:
            end_line = n.meta.get("end_line", 0)
        else:
            continue
        if not end_line:
            continue
        entry = _entry_for(cov, n.file)
        if entry is None:
            continue
        pct = _body_coverage_pct(entry, n.line, end_line)
        if pct is None:
            continue
        n.meta["coverage_pct"] = pct
        mapped += 1

        if n.type == NodeType.ENDPOINT and pct < 50:
            severity = "high" if pct == 0 else "medium"
            message = ("Endpoint handler has no test coverage" if pct == 0
                       else f"Endpoint handler body is only {pct}% covered")
            graph.flag_node(n.id, RiskFlag(
                code="untested_handler", severity=severity, category="quality",
                message=message,
                evidence=f"{n.file}:{n.line} handler "
                         f"`{n.meta.get('handler', '?')}` body coverage {pct}%",
                suggestion="Add a test that exercises this endpoint "
                           "(e.g. via the framework's test client)",
            ))
            flagged += 1
    return {"nodes_with_coverage": mapped, "untested_handlers": flagged}
