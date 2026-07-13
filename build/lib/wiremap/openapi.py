"""OpenAPI client support (ROADMAP 3.3 adapter #4).

When an `openapi.json` exists in the project (root or backend dir), its
paths are ingested as endpoint nodes with CERTAIN confidence — the spec is
the source of truth. Endpoints already discovered from source (same
`METHOD /path`) win; spec entries only fill gaps, and they carry no static
risk flags (there is no handler body to inspect — `security` presence maps
to `has_auth` so the matcher's downstream consumers stay honest).

Frontend calls through generated clients (`api.getPetById(...)`) are
matched by method name against operationIds, marked PROBABLE.
"""
from __future__ import annotations

import json
import os

from .graph import Graph, Node, NodeType

_HTTP_METHODS = ("get", "post", "put", "delete", "patch")


def load_openapi(root: str, backend: str) -> tuple[dict, str] | None:
    """Find and parse openapi.json; returns (spec, display_path) or None."""
    for base in (root, backend):
        path = os.path.join(base, "openapi.json")
        if os.path.exists(path):
            try:
                with open(path, encoding="utf-8") as f:
                    spec = json.load(f)
            except (OSError, json.JSONDecodeError):
                return None
            if isinstance(spec, dict) and isinstance(spec.get("paths"), dict):
                return spec, os.path.relpath(path, root)
    return None


def ingest_endpoints(spec: dict, graph: Graph, spec_rel: str) -> dict:
    """Spec paths -> endpoint nodes (CERTAIN); source-discovered nodes win."""
    added = 0
    global_security = bool(spec.get("security"))
    for path, item in spec.get("paths", {}).items():
        if not isinstance(item, dict):
            continue
        for method in _HTTP_METHODS:
            op = item.get(method)
            if not isinstance(op, dict):
                continue
            ep_id = f"ep:{method.upper()} {path}"
            if ep_id in graph.nodes:
                continue
            graph.add_node(Node(
                id=ep_id, type=NodeType.ENDPOINT,
                label=f"{method.upper()} {path}", file=spec_rel, line=1,
                meta={"handler": op.get("operationId", ""),
                      "framework": "openapi",
                      "has_auth": bool(op.get("security")) or global_security,
                      "raw_path": path, "handler_end_line": 0},
            ))
            added += 1
    return {"endpoints": added}


def operation_map(spec: dict) -> dict[str, dict]:
    """operationId -> {"method", "path"} for client-call matching."""
    ops = {}
    for path, item in spec.get("paths", {}).items():
        if not isinstance(item, dict):
            continue
        for method in _HTTP_METHODS:
            op = item.get(method)
            if isinstance(op, dict) and op.get("operationId"):
                ops[op["operationId"]] = {"method": method.upper(),
                                          "path": path}
    return ops
