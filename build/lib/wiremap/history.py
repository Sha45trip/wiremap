"""Scan history & trends (ROADMAP-v2 6.5).

Each scan appends a lightweight snapshot (aggregate stats + a graph hash,
never the full graph) to `.wiremap/history.json`, capped at the most recent
HISTORY_CAP entries. Re-scanning an unchanged graph does not add a point —
the series carries one point per distinct state, so trends stay meaningful.
The viewer renders risk-over-time and flag churn from this list.
"""
from __future__ import annotations

import datetime as _dt
import hashlib
import json
import os
from collections import Counter

from .graph import Graph, NodeType, EdgeType

HISTORY_CAP = 100
HISTORY_FILE = "history.json"


def graph_hash(graph: Graph) -> str:
    """Stable digest of the graph's shape + flags — identical graphs hash
    identically across machines (node ids are already forward-slashed)."""
    nodes = sorted(graph.nodes)
    flags = sorted(f"{n.id}\x1f{fl['code']}" for n in graph.nodes.values()
                   for fl in n.risk_flags)
    payload = "\x1e".join(nodes) + "\x1d" + "\x1e".join(flags)
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


def snapshot(graph: Graph, now: float | None = None) -> dict:
    import time
    ts = now if now is not None else time.time()
    by_code = Counter(fl["code"] for n in graph.nodes.values()
                      for fl in n.risk_flags)
    flags = [fl for n in graph.nodes.values() for fl in n.risk_flags]
    return {
        "ts": round(ts, 3),
        "iso": _dt.datetime.fromtimestamp(
            ts, _dt.timezone.utc).replace(microsecond=0).strftime(
            "%Y-%m-%dT%H:%M:%SZ"),
        "graph_hash": graph_hash(graph),
        "total_risk": round(sum(n.risk_score for n in graph.nodes.values()), 1),
        "nodes": len(graph.nodes),
        "wires": len(graph.edges_of(EdgeType.HTTP)),
        "endpoints": len(graph.nodes_of(NodeType.ENDPOINT)),
        "flags_total": len(flags),
        "flags_critical": sum(1 for f in flags if f["severity"] == "critical"),
        "by_code": dict(sorted(by_code.items())),
    }


def load_history(out_dir: str) -> list[dict]:
    path = os.path.join(out_dir, HISTORY_FILE)
    if not os.path.exists(path):
        return []
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except (json.JSONDecodeError, OSError):
        return []


def record_snapshot(out_dir: str, graph: Graph,
                    now: float | None = None) -> dict | None:
    """Append a snapshot unless the graph is unchanged since the last one.
    Returns the snapshot added, or None if skipped."""
    hist = load_history(out_dir)
    snap = snapshot(graph, now)
    if hist and hist[-1]["graph_hash"] == snap["graph_hash"]:
        return None                       # unchanged — don't spam the series
    hist.append(snap)
    hist = hist[-HISTORY_CAP:]
    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.join(out_dir, HISTORY_FILE), "w", encoding="utf-8") as f:
        json.dump(hist, f)
    return snap


def flag_churn(hist: list[dict]) -> dict:
    """Introduced/resolved flag codes between the last two snapshots."""
    if len(hist) < 2:
        return {"introduced": [], "resolved": [], "risk_delta": 0.0}
    prev, cur = hist[-2]["by_code"], hist[-1]["by_code"]
    introduced = sorted(c for c in cur if cur[c] > prev.get(c, 0))
    resolved = sorted(c for c in prev if prev[c] > cur.get(c, 0))
    return {"introduced": introduced, "resolved": resolved,
            "risk_delta": round(hist[-1]["total_risk"]
                                 - hist[-2]["total_risk"], 1)}
