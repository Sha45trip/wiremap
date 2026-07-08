"""Static extractor for React frontends using tree-sitter.

Finds every fetch/axios/api-client call site, resolves the URL (literals and
template literals -> :param placeholders), infers the HTTP method, records
whether the call has error handling / timeout, and links call sites to the
component or function that contains them.
"""
from __future__ import annotations

import os
import re

import tree_sitter_javascript as tsjs
import tree_sitter_typescript as tsts
from tree_sitter import Language, Parser

from ..cache import FileCache, content_hash
from ..graph import Graph, Node, Edge, NodeType, EdgeType, Confidence, RiskFlag

JS_LANG = Language(tsjs.language())
TSX_LANG = Language(tsts.language_tsx())
TS_LANG = Language(tsts.language_typescript())

EXT_LANG = {
    ".js": JS_LANG, ".jsx": JS_LANG, ".mjs": JS_LANG,
    ".ts": TS_LANG, ".tsx": TSX_LANG,
}

AXIOS_METHODS = {"get", "post", "put", "delete", "patch"}


def _lang_for(path: str):
    return EXT_LANG.get(os.path.splitext(path)[1])


def _text(node, src: bytes) -> str:
    return src[node.start_byte:node.end_byte].decode("utf-8", errors="replace")


def _resolve_url(arg_node, src: bytes) -> tuple[str, Confidence]:
    """Turn a URL argument node into a canonical-ish path string.

    'string'            -> certain
    `template ${x}`     -> substitutions become :param, probable
    anything else       -> <dynamic>, inferred
    """
    t = arg_node.type
    if t == "string":
        raw = _text(arg_node, src).strip("'\"`")
        return raw, Confidence.CERTAIN
    if t == "template_string":
        parts = []
        for child in arg_node.children:
            if child.type == "string_fragment":
                parts.append(_text(child, src))
            elif child.type == "template_substitution":
                parts.append(":param")
        return "".join(parts), Confidence.PROBABLE
    if t == "binary_expression":  # 'a' + id
        raw = _text(arg_node, src)
        # crude: string literals kept, everything else becomes :param
        pieces = re.split(r"\s*\+\s*", raw)
        out = []
        for p in pieces:
            p = p.strip()
            if p and p[0] in "'\"`":
                out.append(p.strip("'\"`"))
            else:
                out.append(":param")
        return "".join(out), Confidence.PROBABLE
    return "<dynamic>", Confidence.INFERRED


def _enclosing_component(node, src: bytes) -> tuple[str, int]:
    """Walk up to the nearest named function/component definition."""
    cur = node
    while cur is not None:
        if cur.type in ("function_declaration", "method_definition"):
            name_node = cur.child_by_field_name("name")
            if name_node is not None:
                return _text(name_node, src), cur.start_point[0] + 1
        if cur.type == "variable_declarator":
            name_node = cur.child_by_field_name("name")
            value = cur.child_by_field_name("value")
            if name_node is not None and value is not None and value.type in (
                    "arrow_function", "function_expression", "function"):
                return _text(name_node, src), cur.start_point[0] + 1
        cur = cur.parent
    return "<module>", node.start_point[0] + 1


def _method_from_fetch_options(call_node, src: bytes) -> str:
    """Second arg of fetch(): { method: 'POST', ... }"""
    args = call_node.child_by_field_name("arguments")
    if args is None or args.named_child_count < 2:
        return "GET"
    opts = args.named_children[1]
    if opts.type != "object":
        return "GET"
    m = re.search(r"method\s*:\s*['\"`](\w+)['\"`]", _text(opts, src), re.I)
    return m.group(1).upper() if m else "GET"


def _call_features(call_node, src: bytes) -> dict:
    """Detect .catch / try context / AbortController-timeout near the call."""
    # walk up through the full fluent chain: fetch(x).then(...).catch(...)
    chain_top = call_node
    while chain_top.parent is not None and chain_top.parent.type in (
            "member_expression", "call_expression", "await_expression"):
        chain_top = chain_top.parent
    text_around = _text(chain_top, src)
    in_try = False
    cur = call_node
    while cur is not None:
        if cur.type == "try_statement":
            in_try = True
            break
        cur = cur.parent
    has_catch = ".catch(" in text_around or in_try
    has_timeout = ("AbortController" in text_around or "signal" in text_around
                   or "timeout" in text_around)
    return {"has_error_handling": has_catch, "has_timeout": has_timeout}


def extract_frontend(root: str, graph: Graph,
                     cache: FileCache | None = None) -> dict:
    n_calls = 0
    seen_components: set[str] = set()
    files_parsed = files_cached = 0
    seen_files: set[str] = set()

    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in
                       (".git", "node_modules", "dist", "build", ".next", "coverage")]
        for fn in filenames:
            lang = _lang_for(fn)
            if lang is None:
                continue
            full = os.path.join(dirpath, fn)
            rel = os.path.relpath(full, root)
            with open(full, "rb") as f:
                src = f.read()
            seen_files.add(rel)
            sha = content_hash(src)
            records = cache.get("frontend", rel, sha) if cache else None
            if records is None:
                records = _parse_source(src, rel, lang)
                files_parsed += 1
                if cache:
                    cache.put("frontend", rel, sha, records)
            else:
                files_cached += 1
            n_calls += _assemble(records, rel, graph, seen_components)

    if cache:
        cache.prune("frontend", seen_files)

    return {"api_calls": n_calls, "components": len(seen_components),
            "files_parsed": files_parsed, "files_cached": files_cached}


def _parse_source(src: bytes, rel: str, lang) -> list[dict]:
    """Parse one file into JSON-serializable call records (cacheable)."""
    parser = Parser(lang)
    tree = parser.parse(src)
    records: list[dict] = []
    stack = [tree.root_node]
    while stack:
        node = stack.pop()
        stack.extend(node.children)
        if node.type != "call_expression":
            continue
        fn = node.child_by_field_name("function")
        if fn is None:
            continue
        fn_text = _text(fn, src)
        args = node.child_by_field_name("arguments")

        method = None
        url_node = None

        if fn_text == "fetch" and args and args.named_child_count >= 1:
            url_node = args.named_children[0]
            method = _method_from_fetch_options(node, src)
        elif fn.type == "member_expression":
            obj = fn.child_by_field_name("object")
            prop = fn.child_by_field_name("property")
            obj_t = _text(obj, src) if obj else ""
            prop_t = _text(prop, src) if prop else ""
            # axios.get(...) / api.post(...) / apiClient.delete(...)
            if prop_t in AXIOS_METHODS and re.match(r"^(axios|api\w*|client|http)$", obj_t, re.I):
                if args and args.named_child_count >= 1:
                    url_node = args.named_children[0]
                    method = prop_t.upper()
            # axios({url, method})
            elif obj_t == "axios" and prop_t == "request":
                pass  # rare; skip in v1

        if url_node is None or method is None:
            continue

        url, confidence = _resolve_url(url_node, src)
        # strip origin if present (http://localhost:8000/api/x -> /api/x)
        url = re.sub(r"^https?://[^/]+", "", url) or "/"
        if not url.startswith("/") and url != "<dynamic>":
            url = "/" + url

        comp_name, comp_line = _enclosing_component(node, src)
        features = _call_features(node, src)
        records.append({
            "method": method, "url": url, "confidence": confidence.value,
            "component": comp_name, "component_line": comp_line,
            "line": node.start_point[0] + 1,
            "has_error_handling": features["has_error_handling"],
            "has_timeout": features["has_timeout"],
        })
    return records


def _assemble(records: list[dict], rel: str, graph: Graph,
              seen_components: set[str]) -> int:
    """Turn one file's call records into graph nodes, edges, and flags."""
    for rec in records:
        method, url, line = rec["method"], rec["url"], rec["line"]
        seen_components.add(rec["component"])

        comp_id = f"cmp:{rec['component']}"
        graph.add_node(Node(
            id=comp_id, type=NodeType.COMPONENT, label=rec["component"],
            file=rel, line=rec["component_line"],
        ))

        call_id = f"call:{rel}:{line}"
        graph.add_node(Node(
            id=call_id, type=NodeType.API_CALL,
            label=f"{method} {url}", file=rel, line=line,
            meta={"method": method, "url": url,
                  "confidence": rec["confidence"],
                  "has_error_handling": rec["has_error_handling"],
                  "has_timeout": rec["has_timeout"]},
        ))
        graph.add_edge(Edge(
            id=f"{comp_id}->{call_id}", source=comp_id, target=call_id,
            type=EdgeType.MAKES_CALL,
        ))

        if not rec["has_error_handling"]:
            graph.flag_node(call_id, RiskFlag(
                code="no_error_handling", severity="medium", category="quality",
                message="API call has no .catch and is not inside try/catch",
                evidence=f"{rel}:{line} `{method} {url}`",
                suggestion="Handle the rejected promise; show an error state in the UI",
            ))
        if not rec["has_timeout"]:
            graph.flag_node(call_id, RiskFlag(
                code="no_timeout", severity="low", category="operational",
                message="API call has no timeout/abort signal",
                evidence=f"{rel}:{line} `{method} {url}`",
                suggestion="Pass an AbortController signal or axios timeout",
            ))
    return len(records)
