"""Risk engine — turns flags into weighted scores, adds structural signals.

Weights are configurable via wiremap.yaml:

    weights:
      quality: 1.0
      contract: 1.5
      operational: 1.0
      security: 2.0
    severity:
      low: 1
      medium: 3
      high: 6
      critical: 10
"""
from __future__ import annotations

import os
from collections import defaultdict

import yaml

from .graph import Graph, NodeType, EdgeType, RiskFlag

DEFAULT_CONFIG = {
    "weights": {"quality": 1.0, "contract": 1.5, "operational": 1.0, "security": 2.0},
    "severity": {"low": 1, "medium": 3, "high": 6, "critical": 10},
    "hub_fanin_threshold": 3,
    "runtime": {"p95_ms_threshold": 1000, "error_rate_threshold": 0.02},
}


def load_config(project_root: str) -> dict:
    path = os.path.join(project_root, "wiremap.yaml")
    cfg = dict(DEFAULT_CONFIG)
    if os.path.exists(path):
        with open(path) as f:
            user = yaml.safe_load(f) or {}
        for k in ("weights", "severity", "runtime"):
            if k in user:
                cfg[k] = {**cfg[k], **user[k]}
        if "hub_fanin_threshold" in user:
            cfg["hub_fanin_threshold"] = user["hub_fanin_threshold"]
    return cfg


def score(graph: Graph, config: dict) -> dict:
    weights = config["weights"]
    sev = config["severity"]

    # --- structural: single-point-of-failure hubs (high fan-in functions) ---
    fanin: dict[str, int] = defaultdict(int)
    for e in graph.edges.values():
        if e.type in (EdgeType.CALLS, EdgeType.QUERIES):
            fanin[e.target] += 1
    for nid, count in fanin.items():
        n = graph.nodes.get(nid)
        if n and n.type == NodeType.FUNCTION and count >= config["hub_fanin_threshold"]:
            graph.flag_node(nid, RiskFlag(
                code="hub_function", severity="medium", category="operational",
                message=f"{count} wires route through this single function",
                evidence=f"{n.file}:{n.line} `{n.label}` fan-in {count}",
                suggestion="A failure here cascades widely — prioritize tests and "
                           "error handling on this function",
            ))

    # --- composite scores ---
    def flags_to_score(flags: list) -> float:
        total = 0.0
        for f in flags:
            total += sev.get(f["severity"], 1) * weights.get(f["category"], 1.0)
        return round(min(total / 20.0 * 100, 100), 1)  # normalize to 0-100

    max_score = 0.0
    for n in graph.nodes.values():
        n.risk_score = flags_to_score(n.risk_flags)
        max_score = max(max_score, n.risk_score)
    for e in graph.edges.values():
        # an HTTP wire inherits the worse of its two ends, plus its own flags
        base = flags_to_score(e.risk_flags)
        src = graph.nodes.get(e.source)
        tgt = graph.nodes.get(e.target)
        endpoint_risk = max(src.risk_score if src else 0, tgt.risk_score if tgt else 0)
        e.risk_score = round(max(base, endpoint_risk * 0.8), 1)

    total_flags = sum(len(n.risk_flags) for n in graph.nodes.values())
    critical = sum(1 for n in graph.nodes.values()
                   for f in n.risk_flags if f["severity"] == "critical")
    return {"total_flags": total_flags, "critical_flags": critical,
            "max_risk_score": max_score}
