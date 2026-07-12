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
from ..gql import parse_document
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


_JS_BUILTINS = frozenset((
    "then", "catch", "finally", "json", "map", "filter", "forEach", "find",
    "findIndex", "reduce", "some", "every", "includes", "concat", "join",
    "sort", "reverse", "push", "pop", "shift", "slice", "splice", "keys",
    "values", "entries", "length", "indexOf", "at", "flat", "flatMap",
    "toString", "hasOwnProperty",
))


def _iter_tree(node):
    stack = [node]
    while stack:
        n = stack.pop()
        yield n
        stack.extend(n.children)


def _enclosing_function_node(node):
    cur = node.parent
    while cur is not None:
        if cur.type in ("arrow_function", "function_expression",
                        "function_declaration", "method_definition"):
            return cur
        cur = cur.parent
    return None


def _pattern_keys(pattern_node, src: bytes) -> list[str]:
    keys = []
    for child in pattern_node.named_children:
        if child.type in ("shorthand_property_identifier_pattern",
                          "shorthand_property_identifier"):
            keys.append(_text(child, src))
        elif child.type in ("pair_pattern", "pair"):
            k = child.child_by_field_name("key")
            if k is not None and k.type == "property_identifier":
                keys.append(_text(k, src))
    return keys


def _reads_on(scope, src: bytes, obj_text: str) -> set[str]:
    """Field names read off `obj_text` inside `scope`: member accesses
    (`obj.field`) and destructuring (`const {field} = obj`)."""
    fields = set()
    for n in _iter_tree(scope):
        if n.type == "member_expression":
            obj = n.child_by_field_name("object")
            prop = n.child_by_field_name("property")
            if (obj is not None and prop is not None
                    and prop.type == "property_identifier"
                    and _text(obj, src) == obj_text):
                name = _text(prop, src)
                if name not in _JS_BUILTINS:
                    fields.add(name)
        elif n.type == "variable_declarator":
            name_n = n.child_by_field_name("name")
            value = n.child_by_field_name("value")
            if (name_n is not None and value is not None
                    and name_n.type == "object_pattern"
                    and _text(value, src) == obj_text):
                fields.update(k for k in _pattern_keys(name_n, src)
                              if k not in _JS_BUILTINS)
    return fields


def _cb_param(cb):
    p = cb.child_by_field_name("parameter")
    if p is not None:
        return p
    ps = cb.child_by_field_name("parameters")
    if ps is not None and ps.named_child_count == 1:
        return ps.named_children[0]
    return None


def _then_callbacks(call_node, src: bytes) -> list:
    """First args of each `.then(...)` up the fluent chain, in call order.
    `.catch`/`.finally` links are traversed but not collected."""
    cbs = []
    cur = call_node
    while True:
        parent = cur.parent
        while parent is not None and parent.type in ("await_expression",
                                                     "parenthesized_expression"):
            cur = parent
            parent = cur.parent
        if parent is None or parent.type != "member_expression":
            break
        outer = parent.parent
        if outer is None or outer.type != "call_expression":
            break
        prop = parent.child_by_field_name("property")
        if prop is not None and _text(prop, src) == "then":
            args = outer.child_by_field_name("arguments")
            cbs.append(args.named_children[0]
                       if args and args.named_child_count >= 1 else None)
        cur = outer
    return cbs


def _await_binding(call_node, src: bytes) -> str | None:
    """`const V = await <call>` -> "V"."""
    aw = call_node.parent
    if aw is not None and aw.type == "await_expression":
        decl = aw.parent
        if decl is not None and decl.type == "variable_declarator":
            name = decl.child_by_field_name("name")
            if name is not None and name.type == "identifier":
                return _text(name, src)
    return None


def _fetch_json_var(scope, src: bytes, resp_var: str) -> str | None:
    """`const D = await V.json()` -> "D"."""
    for n in _iter_tree(scope):
        if n.type != "variable_declarator":
            continue
        name_n = n.child_by_field_name("name")
        value = n.child_by_field_name("value")
        if name_n is None or value is None or name_n.type != "identifier":
            continue
        expr = value
        if expr.type == "await_expression" and expr.named_child_count:
            expr = expr.named_children[0]
        if expr.type == "call_expression":
            fn = expr.child_by_field_name("function")
            if fn is not None and fn.type == "member_expression":
                obj = fn.child_by_field_name("object")
                prop = fn.child_by_field_name("property")
                if (obj is not None and prop is not None
                        and _text(obj, src) == resp_var
                        and _text(prop, src) == "json"):
                    return _text(name_n, src)
    return None


def _axios_inline_fields(call_node, src: bytes) -> set[str]:
    """`(await axios.get(u)).data.field` / `const {f} = (await ...).data`."""
    fields = set()
    aw = call_node.parent
    if aw is None or aw.type != "await_expression":
        return fields
    par = aw.parent
    if par is None or par.type != "parenthesized_expression":
        return fields
    mem = par.parent
    if mem is None or mem.type != "member_expression":
        return fields
    prop = mem.child_by_field_name("property")
    if prop is None or _text(prop, src) != "data":
        return fields
    outer = mem.parent
    if outer is not None and outer.type == "member_expression":
        p2 = outer.child_by_field_name("property")
        if p2 is not None and p2.type == "property_identifier":
            name = _text(p2, src)
            if name not in _JS_BUILTINS:
                fields.add(name)
    elif outer is not None and outer.type == "variable_declarator":
        name_n = outer.child_by_field_name("name")
        if name_n is not None and name_n.type == "object_pattern":
            fields.update(k for k in _pattern_keys(name_n, src)
                          if k not in _JS_BUILTINS)
    return fields


def _expected_fields(call_node, src: bytes, is_axios: bool) -> list[str]:
    """Best-effort field reads off this call's response (ROADMAP 2.4).

    Supported patterns — anything else yields no fields (precision rule):
      axios: `V = await <call>` + `V.data.f` / `{f} = V.data`,
             `<call>.then(r => ... r.data.f ...)`,
             `(await <call>).data.f` / `{f} = (await <call>).data`
      fetch: `<call>.then(r => r.json()).then(d => ... d.f ...)`,
             `V = await <call>` + `D = await V.json()` + `D.f`
    """
    fields: set[str] = set()
    scope = _enclosing_function_node(call_node)
    bound = _await_binding(call_node, src)
    if bound and scope is not None:
        if is_axios:
            fields |= _reads_on(scope, src, f"{bound}.data")
        else:
            data_var = _fetch_json_var(scope, src, bound)
            if data_var:
                fields |= _reads_on(scope, src, data_var)
    cbs = _then_callbacks(call_node, src)
    if is_axios:
        fields |= _axios_inline_fields(call_node, src)
        if cbs and cbs[0] is not None and cbs[0].type == "arrow_function":
            p = _cb_param(cbs[0])
            if p is not None and p.type == "identifier":
                fields |= _reads_on(cbs[0], src, f"{_text(p, src)}.data")
    else:
        for i, cb in enumerate(cbs):
            if cb is None or cb.type != "arrow_function":
                continue
            body = cb.child_by_field_name("body")
            p = _cb_param(cb)
            if body is None or p is None or p.type != "identifier":
                continue
            # the `r => r.json()` link; the next .then sees the data
            if body.type == "call_expression":
                fn = body.child_by_field_name("function")
                if (fn is not None and fn.type == "member_expression"
                        and _text(fn.child_by_field_name("property"), src) == "json"
                        and _text(fn.child_by_field_name("object"), src)
                        == _text(p, src)
                        and i + 1 < len(cbs)):
                    nxt = cbs[i + 1]
                    if nxt is not None and nxt.type == "arrow_function":
                        np = _cb_param(nxt)
                        if np is not None and np.type == "identifier":
                            fields |= _reads_on(nxt, src, _text(np, src))
                        elif np is not None and np.type == "object_pattern":
                            fields.update(k for k in _pattern_keys(np, src)
                                          if k not in _JS_BUILTINS)
                    break
    return sorted(fields)


_REACT_QUERY_HOOKS = ("useQuery", "useMutation", "useInfiniteQuery",
                      "useSuspenseQuery")


def _in_react_query(call_node, src: bytes) -> bool:
    """True when the call sits inside a React Query hook's options —
    the hook owns error handling, so no_error_handling must not fire."""
    cur = call_node.parent
    while cur is not None:
        if cur.type == "call_expression":
            fn = cur.child_by_field_name("function")
            if fn is not None and fn.type == "identifier" \
                    and _text(fn, src) in _REACT_QUERY_HOOKS:
                return True
        cur = cur.parent
    return False


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
                     cache: FileCache | None = None,
                     client_ops: dict | None = None) -> dict:
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
            # forward slashes everywhere: rel feeds node ids, evidence
            # strings, and cache keys, which must match across OSes
            rel = os.path.relpath(full, root).replace(os.sep, "/")
            try:
                with open(full, "rb") as f:
                    src = f.read()
            except OSError:
                continue    # unreadable (permissions, >MAX_PATH on Windows)
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
            n_calls += _assemble(records, rel, graph, seen_components,
                                 client_ops)

    if cache:
        cache.prune("frontend", seen_files)

    return {"api_calls": n_calls, "components": len(seen_components),
            "files_parsed": files_parsed, "files_cached": files_cached}


_TRPC_HOOKS = {"useQuery": "QUERY", "useMutation": "MUTATION",
               "useInfiniteQuery": "QUERY", "useSuspenseQuery": "QUERY",
               "query": "QUERY", "mutate": "MUTATION", "fetch": "QUERY"}


def _trpc_call_record(call_node, fn, src: bytes) -> dict | None:
    """`trpc.user.byId.useQuery()` -> a tRPC wire record, or None.

    The chain must start at a `trpc`/`api` root and end at a known hook;
    the dotted procedure path between them keys the endpoint
    (/trpc#user.byId). Method comes from the hook (query vs mutation)."""
    if fn is None or fn.type != "member_expression":
        return None
    prop = fn.child_by_field_name("property")
    if prop is None or prop.type != "property_identifier":
        return None
    hook = _text(prop, src)
    verb = _TRPC_HOOKS.get(hook)
    if verb is None:
        return None
    # collect the dotted segments walking down the object chain
    segs: list[str] = []
    cur = fn.child_by_field_name("object")
    while cur is not None and cur.type == "member_expression":
        p = cur.child_by_field_name("property")
        if p is None:
            return None
        segs.append(_text(p, src))
        cur = cur.child_by_field_name("object")
    if cur is None or cur.type != "identifier":
        return None
    root = _text(cur, src)
    if not re.match(r"^(trpc|api|client)$", root, re.I) or not segs:
        return None
    path = ".".join(reversed(segs))
    comp_name, comp_line = _enclosing_component(call_node, src)
    return {"method": verb, "url": f"/trpc#{path}", "confidence": "certain",
            "component": comp_name, "component_line": comp_line,
            "line": call_node.start_point[0] + 1,
            "has_error_handling": True, "has_timeout": True,
            "trpc": True, "expected_fields": []}


def _gql_records(node, src: bytes) -> list[dict]:
    """gql`query { user { ... } }` -> one record per selected root field.
    Apollo/urql own error handling and timeouts, so neither flag applies."""
    tag = node.named_children[0] if node.named_child_count else None
    if tag is None or tag.type != "identifier" \
            or _text(tag, src) not in ("gql", "graphql"):
        return []
    template = node.named_children[node.named_child_count - 1]
    if template.type != "template_string":
        return []
    doc = re.sub(r"\$\{[^}]*\}", " ", _text(template, src).strip("`"))
    parsed = parse_document(doc)
    if parsed is None:
        return []
    kind, fields = parsed

    comp_name, comp_line = _enclosing_component(node, src)
    decl = node.parent
    if decl is not None and decl.type == "variable_declarator":
        nm = decl.child_by_field_name("name")
        if nm is not None and nm.type == "identifier":
            comp_name = _text(nm, src)
            comp_line = decl.start_point[0] + 1
    line = node.start_point[0] + 1
    return [{"method": kind, "url": f"/graphql#{f}", "confidence": "certain",
             "component": comp_name, "component_line": comp_line,
             "line": line, "has_error_handling": True, "has_timeout": True,
             "graphql": True, "expected_fields": []}
            for f in fields]


def _parse_source(src: bytes, rel: str, lang) -> list[dict]:
    """Parse one file into JSON-serializable call records (cacheable)."""
    parser = Parser(lang)
    tree = parser.parse(src)
    records: list[dict] = []
    stack = [tree.root_node]
    while stack:
        node = stack.pop()
        stack.extend(node.children)
        if node.type == "tagged_template_expression":
            records.extend(_gql_records(node, src))
            continue
        if node.type != "call_expression":
            continue
        # some grammar versions parse gql`...` as a call_expression whose
        # last named child is the template_string
        gql_recs = _gql_records(node, src)
        if gql_recs:
            records.extend(gql_recs)
            continue
        fn = node.child_by_field_name("function")
        if fn is None:
            continue
        fn_text = _text(fn, src)
        args = node.child_by_field_name("arguments")

        method = None
        url_node = None
        is_axios = False

        # tRPC client: trpc.user.byId.useQuery() / .useMutation() / .query()
        trpc_rec = _trpc_call_record(node, fn, src)
        if trpc_rec:
            records.append(trpc_rec)
            continue

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
                    is_axios = True
            # generated OpenAPI client: api.getPetById(...) — record the
            # method name; assembly maps it to an operationId if a spec
            # was ingested, otherwise the record is dropped silently
            elif (re.match(r"^(api\w*|client|http)$", obj_t, re.I)
                  and prop_t not in _JS_BUILTINS and args is not None):
                comp_name, comp_line = _enclosing_component(node, src)
                features = _call_features(node, src)
                records.append({
                    "op": prop_t, "component": comp_name,
                    "component_line": comp_line,
                    "line": node.start_point[0] + 1,
                    "has_error_handling": (features["has_error_handling"]
                                           or _in_react_query(node, src)),
                    "has_timeout": features["has_timeout"],
                })
                continue
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
        rq = _in_react_query(node, src)
        records.append({
            "method": method, "url": url, "confidence": confidence.value,
            "component": comp_name, "component_line": comp_line,
            "line": node.start_point[0] + 1,
            "has_error_handling": features["has_error_handling"] or rq,
            "has_timeout": features["has_timeout"],
            "react_query": rq,
            "expected_fields": _expected_fields(node, src, is_axios),
        })
    return records


def _assemble(records: list[dict], rel: str, graph: Graph,
              seen_components: set[str],
              client_ops: dict | None = None) -> int:
    """Turn one file's call records into graph nodes, edges, and flags."""
    count = 0
    for rec in records:
        if "op" in rec:
            hit = (client_ops or {}).get(rec["op"])
            if not hit:
                continue
            method, url, confidence = hit["method"], hit["path"], "probable"
        else:
            method, url = rec["method"], rec["url"]
            confidence = rec["confidence"]
        line = rec["line"]
        count += 1
        seen_components.add(rec["component"])

        comp_id = f"cmp:{rec['component']}"
        graph.add_node(Node(
            id=comp_id, type=NodeType.COMPONENT, label=rec["component"],
            file=rel, line=rec["component_line"],
        ))

        call_id = f"call:{rel}:{line}"
        meta = {"method": method, "url": url,
                "confidence": confidence,
                "has_error_handling": rec["has_error_handling"],
                "has_timeout": rec["has_timeout"]}
        if "op" in rec:
            meta["operation_id"] = rec["op"]
        if rec.get("react_query"):
            meta["react_query"] = True
        if rec.get("graphql"):
            meta["graphql"] = True
        if rec.get("trpc"):
            meta["trpc"] = True
        if rec.get("expected_fields"):
            meta["expected_fields"] = rec["expected_fields"]
            meta["fields_confidence"] = "inferred"
        graph.add_node(Node(
            id=call_id, type=NodeType.API_CALL,
            label=f"{method} {url}", file=rel, line=line, meta=meta,
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
    return count
