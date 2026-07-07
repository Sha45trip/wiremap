"""Cross-stack matcher — the core wire-builder.

Canonicalizes both frontend call URLs and backend route paths to
`METHOD /users/:p/orders/:p` form, then matches them into HTTP edges.
Unmatched call sites and endpoints become orphan flags.
"""
from __future__ import annotations

import re

from .graph import Graph, Edge, NodeType, EdgeType, Confidence, RiskFlag

_PARAM_PATTERNS = (
    re.compile(r"\{[^}/]+\}"),      # FastAPI {user_id}
    re.compile(r"<[^>/]+>"),        # Flask <int:user_id>
    re.compile(r":param"),          # frontend template substitution
    re.compile(r":[A-Za-z_]\w*"),   # express-style, just in case
)


def canonicalize(path: str) -> str:
    p = path.split("?")[0].rstrip("/") or "/"
    for pat in _PARAM_PATTERNS:
        p = pat.sub(":p", p)
    # numeric literal segments in frontend URLs are almost always params
    p = re.sub(r"/\d+(?=/|$)", "/:p", p)
    return p


def match(graph: Graph) -> dict:
    endpoints = {}
    for ep in graph.nodes_of(NodeType.ENDPOINT):
        key = (ep.meta["raw_path"] and ep.label.split(" ", 1)[0],
               canonicalize(ep.meta["raw_path"]))
        endpoints.setdefault(key, []).append(ep)

    matched, orphan_calls = 0, 0
    matched_endpoint_ids: set[str] = set()

    for call in graph.nodes_of(NodeType.API_CALL):
        url = call.meta["url"]
        if url == "<dynamic>":
            graph.flag_node(call.id, RiskFlag(
                code="unresolvable_url", severity="low", category="contract",
                message="URL is fully dynamic; wire could not be traced statically",
                evidence=f"{call.file}:{call.line}",
                suggestion="Use a literal or template-literal URL, or an API client "
                           "with typed routes, so the wire is traceable",
            ))
            continue
        key = (call.meta["method"], canonicalize(url))
        targets = endpoints.get(key, [])
        if targets:
            ep = targets[0]
            conf = Confidence(call.meta.get("confidence", "certain"))
            graph.add_edge(Edge(
                id=f"{call.id}=>{ep.id}", source=call.id, target=ep.id,
                type=EdgeType.HTTP, confidence=conf,
                meta={"method": call.meta["method"],
                      "pattern": canonicalize(url)},
            ))
            matched += 1
            matched_endpoint_ids.add(ep.id)
        else:
            orphan_calls += 1
            graph.flag_node(call.id, RiskFlag(
                code="orphan_call", severity="high", category="contract",
                message=f"Frontend calls {call.meta['method']} {url} "
                        "but no backend route matches",
                evidence=f"{call.file}:{call.line}",
                suggestion="Endpoint missing, renamed, or typo'd — this will 404 "
                           "in production",
            ))

    dead_endpoints = 0
    for ep in graph.nodes_of(NodeType.ENDPOINT):
        if ep.id not in matched_endpoint_ids:
            dead_endpoints += 1
            graph.flag_node(ep.id, RiskFlag(
                code="unused_endpoint", severity="low", category="contract",
                message="No frontend call site references this endpoint",
                evidence=f"{ep.file}:{ep.line}",
                suggestion="Dead code, an external consumer, or missing frontend "
                           "work — verify and document",
            ))

    return {"matched": matched, "orphan_calls": orphan_calls,
            "unused_endpoints": dead_endpoints}
