"""Static extractor for Express/Node backends (ROADMAP-v2 5.2).

Emits the same endpoint node shape as the Python extractor; matcher, risk,
and viewer stay framework-agnostic.

What it understands, precision-first:
- Only files that import/require `express` are considered at all.
- Routes register on variables assigned from `express()` or
  `express.Router()` — name heuristics are not used.
- `app.use("/prefix", router)` mounts a same-file router; the ubiquitous
  cross-file form `app.use("/prefix", require("./routes/users"))` resolves
  the required file and prefixes its exported router's routes
  (`module.exports = router` / `export default router`).
- Middleware args between path and handler whose names smell like auth
  (requireAuth, passport, protect, ...) set `has_auth`; mutating routes
  without one get `missing_auth`.

Known limits (documented, not guessed around): one level of router
nesting; no call-graph walking into JS handlers yet; `app.all` is skipped
(no certain method).
"""
from __future__ import annotations

import os
import re

from ..cache import FileCache, content_hash
from ..graph import Graph, Node, NodeType, RiskFlag
from .react_frontend import EXT_LANG, _text
from tree_sitter import Parser

_VERBS = {"get", "post", "put", "delete", "patch"}
_AUTH_RE = re.compile(
    r"auth|login|jwt|passport|session|permission|protect|guard|verify",
    re.I)
_EXPRESS_IMPORT_RE = re.compile(
    rb"""require\(\s*['"]express['"]\s*\)|from\s+['"]express['"]""")


def _iter(node):
    stack = [node]
    while stack:
        n = stack.pop()
        yield n
        stack.extend(n.children)


def _parse_source(src: bytes, rel: str, lang) -> dict:
    """One file -> serializable record (cacheable)."""
    empty = {"routes": [], "mounts": [], "exported": None, "routers": [],
             "requires": {}}
    if not _EXPRESS_IMPORT_RE.search(src):
        return empty
    tree = Parser(lang).parse(src)
    routers: set[str] = set()      # vars from express() / express.Router()
    requires: dict[str, str] = {}  # var -> require("./spec")
    router_auth: set[str] = set()  # routers with an auth .use() (6.3)
    routes, mounts = [], []
    exported = None

    for n in _iter(tree.root_node):
        if n.type == "variable_declarator":
            name = n.child_by_field_name("name")
            value = n.child_by_field_name("value")
            if name is not None and value is not None \
                    and name.type == "identifier" \
                    and value.type == "call_expression":
                fn_text = _text(value.child_by_field_name("function"), src)
                if fn_text in ("express", "express.Router"):
                    routers.add(_text(name, src))
                elif fn_text == "require":
                    rargs = value.child_by_field_name("arguments")
                    if rargs and rargs.named_child_count >= 1 \
                            and rargs.named_children[0].type == "string":
                        requires[_text(name, src)] = _text(
                            rargs.named_children[0], src).strip("'\"`")
        elif n.type == "assignment_expression":
            left = n.child_by_field_name("left")
            right = n.child_by_field_name("right")
            if left is not None and right is not None \
                    and _text(left, src) == "module.exports" \
                    and right.type == "identifier":
                exported = _text(right, src)
        elif n.type == "export_statement":
            for child in n.named_children:
                if child.type == "identifier":
                    exported = _text(child, src)

    for n in _iter(tree.root_node):
        if n.type != "call_expression":
            continue
        fn = n.child_by_field_name("function")
        if fn is None or fn.type != "member_expression":
            continue
        obj = fn.child_by_field_name("object")
        prop = fn.child_by_field_name("property")
        if obj is None or prop is None or obj.type != "identifier":
            continue
        if _text(obj, src) not in routers:
            continue
        verb = _text(prop, src)
        args = n.child_by_field_name("arguments")
        if args is None or args.named_child_count < 1:
            continue
        # router-scope auth: router.use(requireAuth) / router.use(passport...)
        # — any auth-smelling identifier arg guards every route on it (6.3)
        if verb == "use" and any(
                a.type == "identifier" and _AUTH_RE.search(_text(a, src))
                for a in args.named_children):
            router_auth.add(_text(obj, src))
            continue
        first = args.named_children[0]
        if first.type != "string":
            continue                      # computed path — skip, never guess
        path = _text(first, src).strip("'\"`")

        if verb == "use" and args.named_child_count >= 2:
            target = args.named_children[1]
            if target.type == "identifier":
                mounts.append({"prefix": path, "router": _text(target, src),
                               "require": None})
            elif target.type == "call_expression":
                tfn = target.child_by_field_name("function")
                targs = target.child_by_field_name("arguments")
                if (tfn is not None and _text(tfn, src) == "require"
                        and targs and targs.named_child_count >= 1
                        and targs.named_children[0].type == "string"):
                    mounts.append({
                        "prefix": path, "router": None,
                        "require": _text(targs.named_children[0],
                                         src).strip("'\"`")})
            continue
        if verb not in _VERBS:
            continue

        middle = [args.named_children[i]
                  for i in range(1, args.named_child_count - 1)]
        has_auth = any(m.type == "identifier"
                       and _AUTH_RE.search(_text(m, src))
                       for m in middle)
        handler = args.named_children[args.named_child_count - 1]
        handler_name = (_text(handler, src)
                        if handler.type == "identifier" else "<inline>")
        routes.append({"method": verb.upper(), "path": path,
                       "owner": _text(obj, src), "auth": has_auth,
                       "line": n.start_point[0] + 1,
                       "handler": handler_name})

    # router-scope auth applies to every route on that router
    for r in routes:
        if r["owner"] in router_auth:
            r["auth"] = True

    return {"routes": routes, "mounts": mounts, "exported": exported,
            "routers": sorted(routers), "requires": requires}


def _resolve_require(from_rel: str, spec: str, files: dict) -> str | None:
    """'./routes/users' relative to the requiring file -> a parsed rel path."""
    if not spec.startswith("."):
        return None
    base = os.path.normpath(
        os.path.join(os.path.dirname(from_rel), spec)).replace(os.sep, "/")
    for cand in (base, base + ".js", base + ".mjs", base + ".ts",
                 base + "/index.js", base + "/index.ts"):
        if cand in files:
            return cand
    return None


def extract_express(root: str, graph: Graph,
                    cache: FileCache | None = None) -> dict:
    files: dict[str, dict] = {}
    files_parsed = files_cached = 0
    seen_files: set[str] = set()

    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in
                       (".git", "node_modules", "dist", "build", ".next",
                        "coverage")]
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
            data = cache.get("express", rel, sha) if cache else None
            if data is None:
                data = _parse_source(src, rel, lang)
                files_parsed += 1
                if cache:
                    cache.put("express", rel, sha, data)
            else:
                files_cached += 1
            if data["routes"] or data["mounts"]:
                files[rel] = data

    if cache:
        cache.prune("express", seen_files)

    # prefix map: (file, router var) -> mount prefix
    prefixes: dict[tuple[str, str], str] = {}
    for rel, data in files.items():
        for m in data["mounts"]:
            prefix = m["prefix"].rstrip("/")
            spec = m["require"]
            if m["router"]:
                if any(r["owner"] == m["router"] for r in data["routes"]):
                    prefixes[(rel, m["router"])] = prefix   # same-file router
                    continue
                spec = data["requires"].get(m["router"])    # required router
            if spec:
                target = _resolve_require(rel, spec, files)
                if target and files[target].get("exported"):
                    prefixes[(target, files[target]["exported"])] = prefix

    n_routes = 0
    for rel, data in files.items():
        for r in data["routes"]:
            prefix = prefixes.get((rel, r["owner"]), "")
            full_path = (prefix + "/" + r["path"].lstrip("/")).rstrip("/") \
                or "/"
            ep_id = f"ep:{r['method']} {full_path}"
            graph.add_node(Node(
                id=ep_id, type=NodeType.ENDPOINT,
                label=f"{r['method']} {full_path}", file=rel, line=r["line"],
                meta={"handler": r["handler"], "framework": "express",
                      "has_auth": r["auth"], "raw_path": full_path,
                      "handler_end_line": 0},
            ))
            n_routes += 1
            if r["method"] in ("POST", "PUT", "DELETE", "PATCH") \
                    and not r["auth"]:
                graph.flag_node(ep_id, RiskFlag(
                    code="missing_auth", severity="high", category="security",
                    message=f"{r['method']} endpoint has no detectable "
                            "auth middleware",
                    evidence=f"{rel}:{r['line']} handler `{r['handler']}`",
                    suggestion="Add an auth middleware (e.g. requireAuth) "
                               "before the handler, or mark it public "
                               "intentionally",
                ))

    return {"routes": n_routes, "files_parsed": files_parsed,
            "files_cached": files_cached}
