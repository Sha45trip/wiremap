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
            # contract check: backend field set must be CERTAIN (declared
            # response model); frontend reads are inferred but exact names
            declared = ep.meta.get("response_fields")
            expected = call.meta.get("expected_fields")
            if declared is not None and expected:
                missing = sorted(set(expected) - set(declared))
                if missing:
                    graph.flag_node(call.id, RiskFlag(
                        code="contract_mismatch", severity="high",
                        category="contract",
                        message=f"Frontend reads {', '.join(missing)} — not "
                                "declared by response model "
                                f"{ep.meta.get('response_model', '?')}",
                        evidence=f"{call.file}:{call.line} reads "
                                 f"{', '.join(expected)}; {ep.file}:{ep.line} "
                                 f"declares {', '.join(declared)}",
                        suggestion="Add the field to the response model or "
                                   "stop reading it — it is undefined at "
                                   "runtime",
                    ))
            # request-body contract (6.2): backend request model is CERTAIN,
            # frontend body object is exact only when complete (no spread)
            req_fields = ep.meta.get("request_fields")
            sent = call.meta.get("sent_fields")
            if req_fields is not None and sent is not None \
                    and call.meta.get("sent_complete"):
                extra = sorted(set(sent) - set(req_fields))
                if extra:
                    graph.flag_node(call.id, RiskFlag(
                        code="request_contract_mismatch", severity="high",
                        category="contract",
                        message=f"Frontend sends {', '.join(extra)} — not "
                                "accepted by request model "
                                f"{ep.meta.get('request_model', '?')}; "
                                "silently dropped",
                        evidence=f"{call.file}:{call.line} sends "
                                 f"{', '.join(sent)}; {ep.file}:{ep.line} "
                                 f"accepts {', '.join(req_fields)}",
                        suggestion="Add the field to the request model or "
                                   "stop sending it — the backend ignores it",
                    ))
                missing = sorted(set(ep.meta.get("request_required", []))
                                 - set(sent))
                if missing:
                    graph.flag_node(call.id, RiskFlag(
                        code="missing_request_field", severity="high",
                        category="contract",
                        message=f"Frontend omits required {', '.join(missing)}"
                                f" for {ep.meta.get('request_model', '?')} — "
                                "request will 422",
                        evidence=f"{call.file}:{call.line} sends "
                                 f"{', '.join(sent) or '(none)'}; required: "
                                 f"{', '.join(ep.meta['request_required'])}",
                        suggestion="Include the required field in the request "
                                   "body",
                    ))
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

    # discovery guard (bench 4.1): when almost nothing matches on a codebase
    # with plenty of calls, the orphans are far more likely to reflect an
    # unsupported route-registration style than 90 real 404s — downgrade
    total = matched + orphan_calls
    guarded = total >= 20 and matched / total < 0.25
    if guarded:
        for call in graph.nodes_of(NodeType.API_CALL):
            for f in call.risk_flags:
                if f["code"] == "orphan_call":
                    f["severity"] = "low"
                    f["message"] += (" (low confidence: only "
                                     f"{matched}/{total} calls matched — "
                                     "route discovery may not cover this "
                                     "stack)")

    return {"matched": matched, "orphan_calls": orphan_calls,
            "unused_endpoints": dead_endpoints, "discovery_guard": guarded}
