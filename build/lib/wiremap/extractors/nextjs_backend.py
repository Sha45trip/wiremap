"""Next.js API routes + tRPC adapter (ROADMAP-v2 5.4).

Emits the same endpoint node shape as every other backend adapter.

Next.js (file-convention routes):
- pages router:  pages/api/users/[id].ts     -> /api/users/:id  (default
  handler; method GET unless the body switches on req.method — then one
  endpoint per handled method)
- app router:    app/api/users/[id]/route.ts -> /api/users/:id  (one
  endpoint per exported GET/POST/... function)
- [id] -> :id, [...slug] catch-all -> :slug. index files drop the segment.

tRPC:
- procedures on a router object: `t.router({ user: userRouter })` nesting
  and `foo: publicProcedure.query(...)` leaves become endpoints keyed
  `QUERY /trpc#user.byId` (dotted path mirrors the client call chain).
- client `trpc.user.byId.useQuery()` / `.useMutation()` / `.query()` in
  the FRONTEND extractor wires to those (handled in react_frontend).

Precision: only files under an `api/` segment (Next) or that build a
router via `t.router`/`router(` (tRPC) are considered.
"""
from __future__ import annotations

import os
import re

from ..cache import FileCache, content_hash
from ..graph import Graph, Node, NodeType, RiskFlag
from .react_frontend import EXT_LANG, _text
from tree_sitter import Parser

_NEXT_METHOD_EXPORTS = {"GET", "POST", "PUT", "DELETE", "PATCH", "HEAD",
                        "OPTIONS"}
_REQ_METHOD_RE = re.compile(r"req\.method\s*===?\s*['\"](\w+)['\"]")
_TRPC_VERBS = {"query": "QUERY", "mutation": "MUTATION",
               "subscription": "SUBSCRIPTION"}


def _iter(node):
    stack = [node]
    while stack:
        n = stack.pop()
        yield n
        stack.extend(n.children)


# --------------------------------------------------------------- next.js path

def _next_route_path(rel: str) -> tuple[str, str] | None:
    """A repo-relative file path -> (url_path, router_kind) or None.

    Recognizes .../pages/api/... (pages router) and .../app/api/.../route.*
    (app router). Returns None for files that aren't API routes.
    """
    parts = rel.replace(os.sep, "/").split("/")
    if "api" not in parts:
        return None
    kind = None
    if "route" == os.path.splitext(parts[-1])[0] and "app" in parts:
        kind = "app"
        seg = parts[parts.index("api"):-1]        # drop the route.* filename
    elif "pages" in parts and parts.index("pages") < parts.index("api"):
        kind = "pages"
        last = os.path.splitext(parts[-1])[0]
        seg = parts[parts.index("api"):-1] + ([] if last == "index" else [last])
    else:
        return None

    out = []
    for s in seg:
        m = re.fullmatch(r"\[\.\.\.(\w+)\]", s) or re.fullmatch(r"\[(\w+)\]", s)
        out.append(":" + m.group(1) if m else s)
    return "/" + "/".join(out), kind


def _pages_methods(src: bytes, tree) -> list[str]:
    """Pages-router handler: methods it switches on, or ['GET'] default."""
    methods = sorted(set(m.upper()
                         for m in _REQ_METHOD_RE.findall(src.decode(
                             "utf-8", "replace"))))
    return methods or ["GET"]


def _app_methods(src: bytes, tree) -> list[str]:
    """App-router route.ts: exported GET/POST/... function names."""
    out = []
    for n in _iter(tree.root_node):
        if n.type != "export_statement":
            continue
        for child in _iter(n):
            if child.type in ("function_declaration",
                              "generator_function_declaration"):
                name = child.child_by_field_name("name")
                if name is not None and _text(name, src) in _NEXT_METHOD_EXPORTS:
                    out.append(_text(name, src))
            elif child.type == "variable_declarator":
                name = child.child_by_field_name("name")
                if name is not None and name.type == "identifier" \
                        and _text(name, src) in _NEXT_METHOD_EXPORTS:
                    out.append(_text(name, src))
    return sorted(set(out))


# ------------------------------------------------------------------- trpc

def _trpc_procedures(src: bytes, tree) -> list[dict]:
    """Collect `name: <procedure>.query/mutation(...)` leaves and
    `name: <router>` nestings, so the caller can assemble dotted paths."""
    # map: router-variable -> list of {key, verb|nested_router, line}
    routers: dict[str, list] = {}
    exported_default = None

    for n in _iter(tree.root_node):
        # const appRouter = t.router({ ... }) / router({ ... })
        if n.type == "variable_declarator":
            name = n.child_by_field_name("name")
            value = n.child_by_field_name("value")
            if name is None or value is None or name.type != "identifier":
                continue
            if value.type != "call_expression":
                continue
            fn = value.child_by_field_name("function")
            fn_text = _text(fn, src) if fn is not None else ""
            if fn_text not in ("t.router", "router", "createTRPCRouter"):
                continue
            args = value.child_by_field_name("arguments")
            if args is None or args.named_child_count < 1:
                continue
            obj = args.named_children[0]
            if obj.type != "object":
                continue
            routers[_text(name, src)] = _router_entries(obj, src)
        elif n.type == "export_statement":
            for child in n.named_children:
                if child.type == "identifier":
                    exported_default = _text(child, src)

    return [{"routers": routers, "root": exported_default}]


def _router_entries(obj_node, src: bytes) -> list[dict]:
    entries = []
    for pair in obj_node.named_children:
        if pair.type != "pair":
            continue
        key = pair.child_by_field_name("key")
        val = pair.child_by_field_name("value")
        if key is None or val is None:
            continue
        key_txt = _text(key, src).strip("'\"")
        # nested router: another identifier or inline t.router({...})
        if val.type == "identifier":
            entries.append({"key": key_txt, "nested": _text(val, src),
                            "line": pair.start_point[0] + 1})
            continue
        # leaf: publicProcedure.input(...).query(cb)  -> find .query/.mutation
        verb = _procedure_verb(val, src)
        if verb:
            entries.append({"key": key_txt, "verb": verb,
                            "line": pair.start_point[0] + 1})
    return entries


def _procedure_verb(node, src: bytes) -> str | None:
    for n in _iter(node):
        if n.type == "member_expression":
            prop = n.child_by_field_name("property")
            if prop is not None and _text(prop, src) in _TRPC_VERBS:
                return _TRPC_VERBS[_text(prop, src)]
    return None


# ------------------------------------------------------------------ driver

def _parse_source(src: bytes, rel: str, lang) -> dict:
    empty = {"next": None, "trpc": []}
    tree = Parser(lang).parse(src)
    out = dict(empty)
    next_route = _next_route_path(rel)
    if next_route:
        path, kind = next_route
        methods = (_app_methods(src, tree) if kind == "app"
                   else _pages_methods(src, tree))
        out["next"] = {"path": path, "methods": methods}
    if b"router" in src and (b"t.router" in src or b"createTRPCRouter" in src
                             or b"procedure" in src):
        out["trpc"] = _trpc_procedures(src, tree)
    return out


def extract_nextjs(root: str, graph: Graph,
                   cache: FileCache | None = None) -> dict:
    files_parsed = files_cached = 0
    seen_files: set[str] = set()
    next_routes: list[tuple[str, dict]] = []
    trpc_files: list[tuple[str, dict]] = []

    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in
                       (".git", "node_modules", "dist", "build", "coverage")]
        for fn in filenames:
            lang = EXT_LANG.get(os.path.splitext(fn)[1])
            if lang is None or fn.endswith((".jsx", ".tsx")):
                continue
            full = os.path.join(dirpath, fn)
            rel = os.path.relpath(full, root).replace(os.sep, "/")
            try:
                with open(full, "rb") as f:
                    src = f.read()
            except OSError:
                continue
            seen_files.add(rel)
            sha = content_hash(src)
            data = cache.get("nextjs", rel, sha) if cache else None
            if data is None:
                data = _parse_source(src, rel, lang)
                files_parsed += 1
                if cache:
                    cache.put("nextjs", rel, sha, data)
            else:
                files_cached += 1
            if data["next"]:
                next_routes.append((rel, data["next"]))
            if data["trpc"]:
                trpc_files.append((rel, data["trpc"][0]))

    if cache:
        cache.prune("nextjs", seen_files)

    n_routes = 0
    for rel, nx in next_routes:
        for method in nx["methods"]:
            ep_id = f"ep:{method} {nx['path']}"
            if ep_id in graph.nodes:
                continue
            graph.add_node(Node(
                id=ep_id, type=NodeType.ENDPOINT,
                label=f"{method} {nx['path']}", file=rel, line=1,
                meta={"handler": "", "framework": "nextjs",
                      "has_auth": False, "raw_path": nx["path"],
                      "handler_end_line": 0}))
            n_routes += 1
            if method in ("POST", "PUT", "DELETE", "PATCH"):
                graph.flag_node(ep_id, RiskFlag(
                    code="missing_auth", severity="high", category="security",
                    message=f"{method} API route has no detectable auth check",
                    evidence=f"{rel}:1",
                    suggestion="Guard the route (middleware, getServerSession, "
                               "or an explicit auth check) or mark it public"))

    n_routes += _assemble_trpc(trpc_files, graph)
    return {"routes": n_routes, "files_parsed": files_parsed,
            "files_cached": files_cached}


def _assemble_trpc(trpc_files: list[tuple[str, dict]], graph: Graph) -> int:
    # merge every file's router map; resolve nestings into dotted paths
    all_routers: dict[str, list] = {}
    roots: list[str] = []
    file_of: dict[str, str] = {}
    for rel, data in trpc_files:
        for rname, entries in data["routers"].items():
            all_routers[rname] = entries
            file_of[rname] = rel
        if data.get("root"):
            roots.append(data["root"])

    if not all_routers:
        return 0
    # root routers = those never referenced as a nested value
    nested = {e["nested"] for entries in all_routers.values()
              for e in entries if "nested" in e}
    if not roots:
        roots = [r for r in all_routers if r not in nested]

    emitted: set[str] = set()
    n = 0

    def walk(rname: str, prefix: str, stack: frozenset):
        nonlocal n
        if rname not in all_routers or rname in stack:
            return
        stack = stack | {rname}
        for e in all_routers[rname]:
            path = f"{prefix}{e['key']}"
            if "nested" in e:
                walk(e["nested"], path + ".", stack)
            elif "verb" in e:
                ep_id = f"ep:{e['verb']} /trpc#{path}"
                if ep_id in emitted or ep_id in graph.nodes:
                    continue
                emitted.add(ep_id)
                graph.add_node(Node(
                    id=ep_id, type=NodeType.ENDPOINT,
                    label=f"{e['verb']} /trpc#{path}",
                    file=file_of.get(rname, ""), line=e["line"],
                    meta={"handler": "", "framework": "trpc",
                          "has_auth": False, "raw_path": f"/trpc#{path}",
                          "handler_end_line": 0}))
                n += 1

    for r in sorted(set(roots)):
        walk(r, "", frozenset())
    return n
