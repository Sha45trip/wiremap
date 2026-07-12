"""GraphQL support (ROADMAP-v2 5.3).

GraphQL has one HTTP endpoint but the meaningful wires are per root field,
so each Query/Mutation/Subscription root field becomes an endpoint node:
`ep:QUERY /graphql#user`. The pseudo-path keeps the existing matcher
working unchanged — client operations that select undeclared fields become
orphan_call, root fields nobody queries become unused_endpoint.

Two schema sources:
- SDL files (*.graphql / *.gql) anywhere in the backend tree — CERTAIN.
  Parsed with a small brace scanner (stdlib-first, no graphql-core).
- Python resolvers (Strawberry `@strawberry.type class Query`, Graphene
  `class Query(ObjectType)` with `resolve_*`), collected by the backend
  extractor — those carry handler fqns so the call graph continues.

Client documents (gql`` tagged templates) are parsed by the frontend
extractor with `parse_document` below.
"""
from __future__ import annotations

import os
import re

from .graph import Graph, Node, NodeType

_ROOT_TYPES = {"Query": "QUERY", "Mutation": "MUTATION",
               "Subscription": "SUBSCRIPTION"}
_SDL_FIELD_RE = re.compile(r"^\s*([A-Za-z_]\w*)\s*(?:\([^)]*\))?\s*:", re.M)
_SDL_BLOCK_RE = re.compile(
    r"(?:extend\s+)?type\s+(Query|Mutation|Subscription)\b[^{]*\{")


def camel(name: str) -> str:
    """snake_case -> camelCase (Strawberry/Graphene default field naming)."""
    parts = name.split("_")
    return parts[0] + "".join(p.title() for p in parts[1:] if p)


def gql_endpoint_id(kind: str, field: str) -> str:
    return f"ep:{kind} /graphql#{field}"


def sdl_root_fields(text: str) -> list[tuple[str, str, int]]:
    """SDL -> [(kind, field, line)] for root types, including extend type."""
    text_nc = re.sub(r"#[^\n]*", "", text)
    out = []
    for m in _SDL_BLOCK_RE.finditer(text_nc):
        kind = _ROOT_TYPES[m.group(1)]
        depth, i = 1, m.end()
        start = i
        while i < len(text_nc) and depth:
            if text_nc[i] == "{":
                depth += 1
            elif text_nc[i] == "}":
                depth -= 1
            i += 1
        block = text_nc[start:i - 1]
        # only top-level fields: strip nested braces first
        flat, d = [], 0
        for ch in block:
            if ch == "{":
                d += 1
            elif ch == "}":
                d -= 1
            elif d == 0:
                flat.append(ch)
        for fm in _SDL_FIELD_RE.finditer("".join(flat)):
            line = text_nc[:m.end()].count("\n") + 1
            out.append((kind, fm.group(1), line))
    return out


def ingest_sdl(backend_dir: str, graph: Graph) -> dict:
    """Scan *.graphql/*.gql under the backend tree; fill endpoint gaps.
    Resolver-discovered endpoints (which carry handlers) win on collision."""
    added = 0
    for dirpath, dirnames, filenames in os.walk(backend_dir):
        dirnames[:] = [d for d in dirnames if d not in
                       (".git", "node_modules", "__pycache__", ".venv",
                        "venv", "dist", "build")]
        for fn in filenames:
            if not fn.endswith((".graphql", ".gql")):
                continue
            full = os.path.join(dirpath, fn)
            rel = os.path.relpath(full, backend_dir).replace(os.sep, "/")
            try:
                with open(full, encoding="utf-8", errors="replace") as f:
                    text = f.read()
            except OSError:
                continue
            for kind, field, line in sdl_root_fields(text):
                ep_id = gql_endpoint_id(kind, field)
                if ep_id in graph.nodes:
                    continue
                graph.add_node(Node(
                    id=ep_id, type=NodeType.ENDPOINT,
                    label=f"{kind} {field}", file=rel, line=line,
                    meta={"handler": "", "framework": "graphql",
                          "has_auth": False,
                          "raw_path": f"/graphql#{field}",
                          "handler_end_line": 0},
                ))
                added += 1
    return {"root_fields": added}


def parse_document(text: str) -> tuple[str, list[str]] | None:
    """A gql document -> (kind, top-level selected fields).

    Handles operation headers, anonymous queries, aliases (`a: field`),
    argument lists, and fragment spreads. Best-effort scanner, not a full
    grammar — unparseable documents yield None rather than guesses.
    """
    text = re.sub(r"#[^\n]*", "", text)
    m = re.search(r"\b(query|mutation|subscription)\b[^{]*\{", text)
    if m:
        kind, i = m.group(1).upper(), m.end() - 1
    else:
        i = text.find("{")
        if i < 0:
            return None
        kind = "QUERY"

    fields: list[str] = []
    depth = 0
    expect_after_alias = False
    while i < len(text):
        ch = text[i]
        if ch == "{":
            depth += 1
            i += 1
        elif ch == "}":
            depth -= 1
            i += 1
            if depth == 0:
                break
        elif ch == "(":
            pdepth = 1
            i += 1
            while i < len(text) and pdepth:
                if text[i] == "(":
                    pdepth += 1
                elif text[i] == ")":
                    pdepth -= 1
                i += 1
        elif depth == 1:
            wm = re.match(r"\.\.\.\s*[A-Za-z_]\w*|[A-Za-z_]\w*|@\w+", text[i:])
            if wm:
                word = wm.group(0)
                i += len(word)
                if word.startswith("...") or word.startswith("@"):
                    expect_after_alias = False
                    continue
                rest = text[i:].lstrip()
                if rest.startswith(":") and not expect_after_alias:
                    i = len(text) - len(rest) + 1   # skip alias colon
                    expect_after_alias = True
                    continue
                fields.append(word)
                expect_after_alias = False
            else:
                i += 1
        else:
            i += 1
    return (kind, fields) if fields else None
