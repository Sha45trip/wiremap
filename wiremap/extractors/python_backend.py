"""Static extractor for Python backends (FastAPI first, Flask basic).

Walks every .py file, finds route registrations, then walks the call graph
from each handler down through project-local functions to ORM usage.
Also attaches per-handler static risk signals with file:line evidence.

Call-graph resolution is module-qualified (ROADMAP 3.4): functions are
keyed by `pkg.module.qname`, and callees resolve through each module's
import map (`from .services import fetch_orders` → `app.services.
fetch_orders`), same-module names, module-alias attribute calls
(`svc.f()`), and `self.method()`. Unresolvable callees are skipped rather
than guessed — no more cross-module bare-name collisions.
"""
from __future__ import annotations

import ast
import os
import re
from dataclasses import dataclass, field, asdict

from ..cache import FileCache, content_hash
from ..gql import camel
from ..graph import Graph, Node, Edge, NodeType, EdgeType, Confidence, RiskFlag

HTTP_METHODS = {"get", "post", "put", "delete", "patch", "head", "options"}
_DJANGO_METHOD_DECS = {"require_GET": ["GET"], "require_POST": ["POST"],
                       "require_safe": ["GET"]}
# DRF DefaultRouter action -> (HTTP method, path suffix)
_DRF_ACTIONS = (("list", "GET", ""), ("create", "POST", ""),
                ("retrieve", "GET", "<pk>/"), ("update", "PUT", "<pk>/"),
                ("partial_update", "PATCH", "<pk>/"),
                ("destroy", "DELETE", "<pk>/"))
IO_CALL_HINTS = (
    "execute", "commit", "query", "get", "post", "put", "delete",
    "request", "fetch", "read", "write", "send",
)
AUTH_HINTS = ("auth", "token", "user", "login", "permission", "jwt", "session")


@dataclass
class RouteInfo:
    method: str
    path: str
    handler: str          # qualified function name
    file: str
    line: int
    framework: str
    has_auth_dep: bool = False
    router_prefix: str = ""
    response_model: str = ""   # bare Name from response_model= or -> annotation
    request_model: str = ""    # bare Name from a body-param annotation (6.2)


@dataclass
class FunctionInfo:
    qname: str            # local name, e.g. "fetch_orders" or "Cls.method"
    module: str           # dotted module path, e.g. "app.services"
    file: str
    line: int
    end_line: int
    calls: list = field(default_factory=list)      # names called inside
    has_try: bool = False
    io_calls_outside_try: list = field(default_factory=list)
    raw_sql_interp: list = field(default_factory=list)   # (line, snippet)
    complexity: int = 1
    orm_models: list = field(default_factory=list)
    methods: list = field(default_factory=list)    # from @require_GET etc.
    auth_hint: bool = False                        # decorator/class auth smell
    params: list = field(default_factory=list)     # ordered param names (6.1)
    sql_sink_params: list = field(default_factory=list)  # params interpolated
    forwards: list = field(default_factory=list)   # {callee, arg_params} calls


def _module_of(rel: str) -> tuple[str, bool]:
    """Relative file path -> (dotted module, is_package_init)."""
    parts = rel.replace("\\", "/").rsplit(".py", 1)[0].split("/")
    is_init = parts[-1] == "__init__"
    if is_init:
        parts = parts[:-1]
    return ".".join(p for p in parts if p), is_init


def _resolve_relative(module: str, is_init: bool, level: int,
                      target: str | None) -> str | None:
    """`from ..x import y` in `module` -> absolute base module, or None."""
    if level == 0:
        return target
    pkg = module.split(".") if module else []
    if not is_init:
        pkg = pkg[:-1]                       # drop the file component
    if level - 1 > len(pkg):
        return None                          # escapes the scanned tree
    base = pkg[:len(pkg) - (level - 1)]
    if target:
        base = base + target.split(".")
    return ".".join(base) or None


def _dotted_name(expr) -> str | None:
    """`views.OrderList` -> "views.OrderList"; anything non-Name-chain -> None."""
    if isinstance(expr, ast.Name):
        return expr.id
    if isinstance(expr, ast.Attribute):
        base = _dotted_name(expr.value)
        return f"{base}.{expr.attr}" if base else None
    return None


def _regex_to_template(pattern: str) -> str:
    """Django re_path regex -> path-ish template the canonicalizer groks."""
    p = pattern.lstrip("^").rstrip("$")
    p = re.sub(r"\(\?P<(\w+)>[^)]*\)", r"<\1>", p)     # named group -> <name>
    p = re.sub(r"\([^)]*\)", "<p>", p)                 # anonymous group
    return p.replace("\\/", "/").replace("\\.", ".")


def _methods_from_decorators(decorator_list) -> list[str]:
    """@require_GET / @require_http_methods([...]) / DRF @api_view([...])."""
    methods: list[str] = []
    for dec in decorator_list:
        target = dec.func if isinstance(dec, ast.Call) else dec
        name = target.attr if isinstance(target, ast.Attribute) \
            else getattr(target, "id", "")
        if name in _DJANGO_METHOD_DECS:
            methods += _DJANGO_METHOD_DECS[name]
        elif name in ("require_http_methods", "api_view") \
                and isinstance(dec, ast.Call) and dec.args \
                and isinstance(dec.args[0], (ast.List, ast.Tuple)):
            methods += [str(e.value).upper() for e in dec.args[0].elts
                        if isinstance(e, ast.Constant)]
    return methods


class _ModuleVisitor(ast.NodeVisitor):
    """Collects routes, functions, imports, prefixes, and models in one file."""

    def __init__(self, filepath: str, rel: str):
        self.filepath = filepath
        self.rel = rel
        self.module, self._is_init = _module_of(rel)
        self.routes: list[RouteInfo] = []
        self.functions: dict[str, FunctionInfo] = {}
        self.imports: dict[str, str] = {}           # local name -> dotted fqn
        self.router_prefixes: dict[str, str] = {}   # var name -> prefix
        self.orm_models: list[tuple[str, int]] = []
        self.pydantic_models: dict[str, dict] = {}  # name -> {fields, bases}
        self.url_entries: list[dict] = []           # django urlpatterns items
        self.drf_registrations: list[dict] = []     # router.register(...) calls
        self.gql_resolvers: list[dict] = []         # strawberry/graphene roots
        self._class_stack: list[str] = []
        self._class_auth: list[bool] = []
        self._gql_kind: list[str | None] = []       # QUERY/MUTATION context

    def visit_Import(self, node: ast.Import):
        for alias in node.names:
            if alias.asname:
                self.imports[alias.asname] = alias.name
            else:
                # `import a.b` binds `a`; only the root name is addressable
                root = alias.name.split(".")[0]
                self.imports[root] = root
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom):
        base = _resolve_relative(self.module, self._is_init,
                                 node.level, node.module)
        if base:
            for alias in node.names:
                if alias.name == "*":
                    continue
                self.imports[alias.asname or alias.name] = f"{base}.{alias.name}"
        self.generic_visit(node)

    # --- prefix detection: APIRouter(prefix=...) / Blueprint(url_prefix=...) ---
    def visit_Assign(self, node: ast.Assign):
        if isinstance(node.value, ast.Call):
            fn = node.value.func
            name = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", "")
            if name in ("APIRouter", "Blueprint"):
                prefix = ""
                for kw in node.value.keywords:
                    if kw.arg in ("prefix", "url_prefix") \
                            and isinstance(kw.value, ast.Constant):
                        prefix = kw.value.value
                for tgt in node.targets:
                    if isinstance(tgt, ast.Name):
                        self.router_prefixes[tgt.id] = prefix
        for tgt in node.targets:
            if isinstance(tgt, ast.Name) and tgt.id == "urlpatterns":
                self._collect_url_entries(node.value)
        self.generic_visit(node)

    def visit_AugAssign(self, node: ast.AugAssign):
        if isinstance(node.target, ast.Name) and node.target.id == "urlpatterns":
            self._collect_url_entries(node.value)
        self.generic_visit(node)

    def _collect_url_entries(self, value):
        """Django: path()/re_path()/url() items in a urlpatterns list."""
        if isinstance(value, ast.BinOp):            # urlpatterns = [...] + [...]
            self._collect_url_entries(value.left)
            self._collect_url_entries(value.right)
            return
        if not isinstance(value, (ast.List, ast.Tuple)):
            return
        for el in value.elts:
            if not isinstance(el, ast.Call):
                continue
            f = el.func
            fname = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", "")
            if fname not in ("path", "re_path", "url"):
                continue
            if len(el.args) < 2 or not isinstance(el.args[0], ast.Constant):
                continue
            pattern = str(el.args[0].value)
            if fname in ("re_path", "url"):
                pattern = _regex_to_template(pattern)
            view = el.args[1]
            entry = {"pattern": pattern, "line": el.lineno,
                     "view": None, "cbv": False, "include": None}
            if isinstance(view, ast.Call):
                vf = view.func
                vfname = vf.attr if isinstance(vf, ast.Attribute) \
                    else getattr(vf, "id", "")
                if vfname == "include" and view.args \
                        and isinstance(view.args[0], ast.Constant):
                    entry["include"] = str(view.args[0].value)
                elif vfname == "as_view":
                    entry["view"] = _dotted_name(vf.value)
                    entry["cbv"] = True
            else:
                entry["view"] = _dotted_name(view)
            if entry["view"] or entry["include"]:
                self.url_entries.append(entry)

    # --- DRF: router.register("items", ItemViewSet) ---
    def visit_Expr(self, node: ast.Expr):
        call = node.value
        if isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute) \
                and call.func.attr == "register" and len(call.args) >= 2 \
                and isinstance(call.args[0], ast.Constant):
            viewset = _dotted_name(call.args[1])
            if viewset:
                self.drf_registrations.append(
                    {"prefix": str(call.args[0].value).strip("/"),
                     "viewset": viewset, "line": node.lineno})
        self.generic_visit(node)

    def visit_ClassDef(self, node: ast.ClassDef):
        bases = []
        for b in node.bases:
            bases.append(b.attr if isinstance(b, ast.Attribute) else getattr(b, "id", ""))
        if any(b in ("Base", "Model", "DeclarativeBase") for b in bases):
            self.orm_models.append((node.name, node.lineno))
        if "BaseModel" in bases or any(b in self.pydantic_models for b in bases):
            fields, required = [], []
            for s in node.body:
                if not (isinstance(s, ast.AnnAssign)
                        and isinstance(s.target, ast.Name)):
                    continue
                fields.append(s.target.id)
                # required = no default value and not Optional/X|None (6.2)
                ann = ast.unparse(s.annotation)
                is_optional = (s.value is not None
                               or ann.startswith("Optional[")
                               or "None" in ann.split("|"))
                if not is_optional:
                    required.append(s.target.id)
            self.pydantic_models[node.name] = {"fields": fields, "bases": bases,
                                               "required": required}
        cls_text = " ".join(bases + [ast.unparse(d) for d in
                                     node.decorator_list]).lower()
        self._class_auth.append(any(h in cls_text for h in AUTH_HINTS))
        # graphql root types: @strawberry.type class Query / Mutation, or
        # graphene class Query(ObjectType)
        gql_kind = None
        if node.name in ("Query", "Mutation"):
            if any("strawberry.type" in ast.unparse(d)
                   for d in node.decorator_list) \
                    or "ObjectType" in bases:
                gql_kind = node.name.upper()
        self._gql_kind.append(gql_kind)
        self._class_stack.append(node.name)
        self.generic_visit(node)
        self._class_stack.pop()
        self._class_auth.pop()
        self._gql_kind.pop()

    def visit_FunctionDef(self, node):
        self._handle_function(node)

    def visit_AsyncFunctionDef(self, node):
        self._handle_function(node)

    def _handle_function(self, node):
        qname = ".".join(self._class_stack + [node.name]) if self._class_stack else node.name
        a = node.args
        params = [p.arg for p in
                  (list(a.posonlyargs) + list(a.args) + list(a.kwonlyargs))
                  if p.arg not in ("self", "cls")]
        info = FunctionInfo(
            qname=qname, module=self.module, file=self.rel, line=node.lineno,
            end_line=getattr(node, "end_lineno", node.lineno), params=params,
        )
        analyzer = _FunctionBodyAnalyzer(params)
        for stmt in node.body:
            analyzer.visit(stmt)
        info.calls = analyzer.calls
        info.has_try = analyzer.has_try
        info.io_calls_outside_try = analyzer.io_outside_try
        info.raw_sql_interp = analyzer.raw_sql
        info.complexity = analyzer.complexity
        info.orm_models = analyzer.orm_models
        info.sql_sink_params = sorted(analyzer.sink_params)
        info.forwards = analyzer.forwards
        info.methods = _methods_from_decorators(node.decorator_list)
        dec_text = " ".join(ast.unparse(d) for d in node.decorator_list).lower()
        info.auth_hint = (any(h in dec_text for h in AUTH_HINTS)
                          or any(self._class_auth))
        fqn = f"{self.module}.{qname}" if self.module else qname
        self.functions[fqn] = info

        # graphql resolvers inside a root type
        kind = self._gql_kind[-1] if self._gql_kind else None
        if kind:
            field = None
            if node.name.startswith("resolve_"):                 # graphene
                field = camel(node.name[len("resolve_"):])
            elif any("strawberry.field" in ast.unparse(d)
                     for d in node.decorator_list):              # strawberry
                field = camel(node.name)
            if field:
                self.gql_resolvers.append(
                    {"kind": kind, "field": field, "handler": fqn,
                     "line": node.lineno})

        # route decorators
        for dec in node.decorator_list:
            self.routes.extend(self._route_from_decorator(dec, qname, node))
        self.generic_visit(node)

    def _route_from_decorator(self, dec, qname, fn_node) -> list[RouteInfo]:
        if not isinstance(dec, ast.Call):
            return []
        f = dec.func
        # FastAPI: @app.get("/x") / @router.post("/y")
        if isinstance(f, ast.Attribute) and f.attr in HTTP_METHODS:
            if not (dec.args and isinstance(dec.args[0], ast.Constant)):
                return []   # computed path — never fabricate "/" (bench 4.1)
            owner = getattr(f.value, "id", "")
            path = str(dec.args[0].value)
            prefix = self.router_prefixes.get(owner, "")
            # response model: bare-Name only — List[X]/Optional[X] are not a
            # CERTAIN field set, and the precision rule says skip those
            response_model = ""
            for kw in dec.keywords:
                if kw.arg == "response_model" and isinstance(kw.value, ast.Name):
                    response_model = kw.value.id
            if not response_model and isinstance(fn_node.returns, ast.Name):
                response_model = fn_node.returns.id
            return [RouteInfo(
                method=f.attr.upper(), path=path,
                handler=f"{self.module}.{qname}" if self.module else qname,
                file=self.rel, line=fn_node.lineno, framework="fastapi",
                has_auth_dep=self._has_auth_dependency(fn_node),
                router_prefix=prefix, response_model=response_model,
                request_model=self._request_model(fn_node),
            )]
        # Flask: @app.route(...) / @bp.route(...), one RouteInfo per method
        if isinstance(f, ast.Attribute) and f.attr == "route":
            if not (dec.args and isinstance(dec.args[0], ast.Constant)):
                return []   # e.g. @routes.route(org_scoped_rule("/login"))
            owner = getattr(f.value, "id", "")
            path = str(dec.args[0].value)
            methods = ["GET"]
            for kw in dec.keywords:
                if kw.arg == "methods" and isinstance(kw.value, (ast.List, ast.Tuple)):
                    methods = [e.value for e in kw.value.elts if isinstance(e, ast.Constant)]
            return [RouteInfo(
                method=str(m).upper(), path=path,
                handler=f"{self.module}.{qname}" if self.module else qname,
                file=self.rel, line=fn_node.lineno, framework="flask",
                has_auth_dep=self._has_auth_dependency(fn_node),
                router_prefix=self.router_prefixes.get(owner, ""),
            ) for m in methods]
        return []

    _NON_BODY_ANNOT = {"int", "str", "float", "bool", "bytes", "dict", "list",
                       "tuple", "set", "UUID", "date", "datetime", "Decimal",
                       "Request", "BackgroundTasks", "Response"}

    @classmethod
    def _request_model(cls, fn_node) -> str:
        """First body-param annotation that's a bare Name and not a
        primitive/framework type — resolved against the Pydantic model set
        at assembly (so a non-model guess simply yields no request_fields)."""
        args = fn_node.args
        for a in list(args.posonlyargs) + list(args.args) + list(args.kwonlyargs):
            if a.arg in ("self", "cls"):
                continue
            if isinstance(a.annotation, ast.Name) \
                    and a.annotation.id not in cls._NON_BODY_ANNOT:
                return a.annotation.id
        return ""

    @staticmethod
    def _has_auth_dependency(fn_node) -> bool:
        """FastAPI: any param default Depends(x) where x smells auth-related,
        or any decorator like @login_required."""
        args = fn_node.args
        for default in list(args.defaults) + list(args.kw_defaults or []):
            if isinstance(default, ast.Call):
                callee = default.func
                cname = callee.attr if isinstance(callee, ast.Attribute) else getattr(callee, "id", "")
                if cname == "Depends":
                    inner = ast.unparse(default.args[0]).lower() if default.args else ""
                    if any(h in inner for h in AUTH_HINTS):
                        return True
        for dec in fn_node.decorator_list:
            name = ast.unparse(dec).lower()
            if any(h in name for h in AUTH_HINTS):
                return True
        return False


def _call_target(f) -> str:
    """The recorded call string for an ast call func (matches `calls`)."""
    if isinstance(f, ast.Name):
        return f.id
    if isinstance(f, ast.Attribute):
        return f"{f.value.id}.{f.attr}" if isinstance(f.value, ast.Name) \
            else f.attr
    return ""


class _FunctionBodyAnalyzer(ast.NodeVisitor):
    def __init__(self, params: list[str] | None = None):
        self.calls: list[str] = []
        self.has_try = False
        self._try_depth = 0
        self.io_outside_try: list[tuple[int, str]] = []
        self.raw_sql: list[tuple[int, str]] = []
        self.complexity = 1
        self.orm_models: list[str] = []
        # taint (6.1): local var -> the param it derives from
        self.alias: dict[str, str] = {p: p for p in (params or [])}
        self.sink_params: set[str] = set()
        self.forwards: list[dict] = []

    def _origin(self, node) -> str | None:
        """The param a value derives from: Name, or Subscript/Attribute of a
        tainted base (`payload`, `payload['id']`, `payload.id`)."""
        if isinstance(node, ast.Name):
            return self.alias.get(node.id)
        if isinstance(node, (ast.Subscript, ast.Attribute)):
            return self._origin(node.value)
        return None

    def _record_assign(self, target, value):
        if not isinstance(target, ast.Name):
            return
        origin = self._origin(value)
        if origin:
            self.alias[target.id] = origin
        else:
            self.alias.pop(target.id, None)   # reassigned to untainted

    def visit_Assign(self, node):
        for t in node.targets:
            self._record_assign(t, node.value)
        self.generic_visit(node)

    def visit_AnnAssign(self, node):
        if node.value is not None:
            self._record_assign(node.target, node.value)
        self.generic_visit(node)

    def _tainted_names_in(self, node) -> set[str]:
        out = set()
        for sub in ast.walk(node):
            if isinstance(sub, ast.Name) and sub.id in self.alias:
                out.add(self.alias[sub.id])
        return out

    def visit_Try(self, node):
        self.has_try = True
        self._try_depth += 1
        self.generic_visit(node)
        self._try_depth -= 1

    def visit_If(self, node):
        self.complexity += 1
        self.generic_visit(node)

    def visit_For(self, node):
        self.complexity += 1
        self.generic_visit(node)

    def visit_While(self, node):
        self.complexity += 1
        self.generic_visit(node)

    def visit_BoolOp(self, node):
        self.complexity += len(node.values) - 1
        self.generic_visit(node)

    def visit_ExceptHandler(self, node):
        self.complexity += 1
        self.generic_visit(node)

    def visit_Call(self, node):
        f = node.func
        if isinstance(f, ast.Name):
            self.calls.append(f.id)
            leaf = f.id
        elif isinstance(f, ast.Attribute):
            # keep `obj.attr` when obj is a simple name so the resolver can
            # qualify module-alias and self calls; deeper chains stay bare
            if isinstance(f.value, ast.Name):
                self.calls.append(f"{f.value.id}.{f.attr}")
            else:
                self.calls.append(f.attr)
            leaf = f.attr
            # session.query(User) / db.query(User)
            if f.attr == "query" and node.args:
                m = node.args[0]
                if isinstance(m, ast.Name):
                    self.orm_models.append(m.id)
        else:
            leaf = ""
        if leaf in IO_CALL_HINTS and self._try_depth == 0:
            self.io_outside_try.append((node.lineno, leaf))
        # raw SQL with interpolation: execute(f"...{x}...") or "..." % / +
        if leaf in ("execute", "executemany") and node.args:
            a = node.args[0]
            if isinstance(a, ast.JoinedStr) or isinstance(a, ast.BinOp):
                snippet = ast.unparse(a)[:80]
                self.raw_sql.append((node.lineno, snippet))
                # taint: which of our params flow into this interpolation
                self.sink_params.update(self._tainted_names_in(a))
        # forwards (6.1): tainted args passed to a project function
        target = _call_target(f)
        if target:
            arg_params = [self._origin(arg) for arg in node.args]
            if any(arg_params):
                self.forwards.append({"callee": target,
                                      "arg_params": arg_params})
        self.generic_visit(node)


def _parse_source(source: str, rel: str) -> dict:
    """Parse one file into a JSON-serializable extraction record (cacheable)."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        # same shape as a successful parse — downstream indexes these keys
        return {"routes": [], "functions": {}, "models": [],
                "pydantic_models": {}, "module": _module_of(rel)[0],
                "imports": {}, "url_entries": [], "drf": []}
    v = _ModuleVisitor(rel, rel)
    v.visit(tree)
    return {
        "routes": [asdict(r) for r in v.routes],
        "functions": {q: asdict(fi) for q, fi in v.functions.items()},
        "models": [[m, ln] for m, ln in v.orm_models],
        "pydantic_models": v.pydantic_models,
        "module": v.module,
        "imports": v.imports,
        "url_entries": v.url_entries,
        "drf": v.drf_registrations,
        "gql": v.gql_resolvers,
    }


def _django_routes(url_map: dict[str, list], drf_map: dict[str, list],
                   all_imports: dict[str, dict],
                   functions: dict[str, FunctionInfo]) -> list[RouteInfo]:
    """Resolve Django urlpatterns + DRF registrations into RouteInfos.

    urls modules that no other module include()s are treated as roots;
    include() nests prefixes. View refs resolve through the urls module's
    import map (same machinery as the call graph). Unresolvable views are
    skipped, never guessed.
    """
    included = {e["include"] for entries in url_map.values()
                for e in entries if e["include"]}
    routes: list[RouteInfo] = []
    emitted: set = set()

    def resolve(dotted: str, module: str) -> str:
        parts = dotted.split(".")
        base = all_imports.get(module, {}).get(parts[0])
        if base:
            return ".".join([base] + parts[1:])
        return f"{module}.{dotted}" if module else dotted

    def emit(method: str, full: str, handler_fqn: str):
        info = functions.get(handler_fqn)
        if info is None:
            return
        key = (method, full, handler_fqn)
        if key in emitted:
            return
        emitted.add(key)
        routes.append(RouteInfo(
            method=method, path="/" + full.lstrip("/"), handler=handler_fqn,
            file=info.file, line=info.line, framework="django",
            has_auth_dep=info.auth_hint))

    def walk(module: str, prefix: str, stack: frozenset):
        if module in stack:
            return
        stack = stack | {module}
        for e in url_map.get(module, []):
            full = prefix + e["pattern"]
            if e["include"]:
                walk(e["include"], full, stack)
                continue
            fqn = resolve(e["view"], module)
            if e["cbv"]:
                for m in sorted(HTTP_METHODS):
                    if f"{fqn}.{m}" in functions:
                        emit(m.upper(), full, f"{fqn}.{m}")
            else:
                info = functions.get(fqn)
                if info:
                    for m in (info.methods or ["GET"]):
                        emit(m, full, fqn)
        for reg in drf_map.get(module, []):
            fqn = resolve(reg["viewset"], module)
            for action, m, suffix in _DRF_ACTIONS:
                if f"{fqn}.{action}" in functions:
                    emit(m, f"{prefix}{reg['prefix']}/{suffix}",
                         f"{fqn}.{action}")

    roots = [m for m in url_map if m not in included]
    roots += [m for m in drf_map
              if m not in url_map and m not in included]
    for module in sorted(set(roots)):
        walk(module, "", frozenset())
    return routes


def _resolve_callee(callee: str, info: FunctionInfo, imports: dict[str, str],
                    functions: dict[str, FunctionInfo]) -> str | None:
    """Resolve a recorded call to a fully-qualified known function, or None."""
    module = info.module
    if "." in callee:
        obj, attr = callee.split(".", 1)
        if obj == "self" and "." in info.qname:
            cls = info.qname.rsplit(".", 1)[0]
            cand = f"{module}.{cls}.{attr}" if module else f"{cls}.{attr}"
            return cand if cand in functions else None
        target_mod = imports.get(obj)
        if target_mod:
            cand = f"{target_mod}.{attr}"
            return cand if cand in functions else None
        return None
    fqn = imports.get(callee)
    if fqn:
        return fqn if fqn in functions else None
    cand = f"{module}.{callee}" if module else callee
    return cand if cand in functions else None


def _sql_taint(fqn: str, functions: dict[str, FunctionInfo],
               all_imports: dict[str, dict], depth: int = 2,
               seen: frozenset = frozenset()):
    """Does a parameter of `fqn` reach a raw-SQL interpolation within
    `depth` call hops? Returns (param, witness_fqn, witness_line) or None.

    Uses the module-qualified call graph (3.4): a param is tainted if it is
    interpolated directly, or forwarded into a callee argument position that
    is itself a taint sink. Precision-first — positional args only, resolved
    callees only; nothing is guessed."""
    info = functions.get(fqn)
    if info is None or fqn in seen or depth < 0:
        return None
    if info.sql_sink_params:                       # direct interpolation
        return (info.sql_sink_params[0], fqn, info.raw_sql_interp[0][0])
    if depth == 0:
        return None
    seen = seen | {fqn}
    imports = all_imports.get(info.module, {})
    for fwd in info.forwards:
        target = _resolve_callee(fwd["callee"], info, imports, functions)
        if not target:
            continue
        sub = _sql_taint(target, functions, all_imports, depth - 1, seen)
        if sub is None:
            continue
        sink_param, wfqn, wline = sub
        try:
            idx = functions[target].params.index(sink_param)
        except ValueError:
            continue
        if idx < len(fwd["arg_params"]) and fwd["arg_params"][idx]:
            return (fwd["arg_params"][idx], wfqn, wline)
    return None


def _resolve_model_fields(name: str, models: dict[str, dict],
                          seen: frozenset = frozenset()) -> list[str]:
    """Own fields plus inherited ones from other collected models."""
    if name not in models or name in seen:
        return []
    info = models[name]
    fields = list(info["fields"])
    for base in info["bases"]:
        for f in _resolve_model_fields(base, models, seen | {name}):
            if f not in fields:
                fields.append(f)
    return fields


def _resolve_required_fields(name: str, models: dict[str, dict],
                             seen: frozenset = frozenset()) -> list[str]:
    """Required (no-default, non-Optional) fields, own + inherited."""
    if name not in models or name in seen:
        return []
    info = models[name]
    req = list(info.get("required", []))
    for base in info["bases"]:
        for f in _resolve_required_fields(base, models, seen | {name}):
            if f not in req:
                req.append(f)
    return req


def extract_backend(root: str, graph: Graph,
                    cache: FileCache | None = None) -> dict:
    """Scan a Python source tree, populate graph, return summary stats."""
    all_functions: dict[str, FunctionInfo] = {}
    all_routes: list[RouteInfo] = []
    all_models: list[tuple[str, str, int]] = []
    all_pydantic: dict[str, dict] = {}
    all_imports: dict[str, dict] = {}       # module -> {local name: fqn}
    all_url_entries: dict[str, list] = {}   # module -> django urlpatterns
    all_drf: dict[str, list] = {}           # module -> DRF registrations
    all_gql: list[dict] = []                # strawberry/graphene root fields
    files_parsed = files_cached = 0
    seen_files: set[str] = set()

    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in
                       (".git", "node_modules", "__pycache__", ".venv", "venv", "migrations")]
        for fn in filenames:
            if not fn.endswith(".py"):
                continue
            full = os.path.join(dirpath, fn)
            # forward slashes everywhere: rel feeds node ids, evidence
            # strings, and cache keys, which must match across OSes
            rel = os.path.relpath(full, root).replace(os.sep, "/")
            try:
                with open(full, "rb") as f:
                    raw = f.read()
            except OSError:
                continue    # unreadable (permissions, >MAX_PATH on Windows)
            seen_files.add(rel)
            sha = content_hash(raw)
            data = cache.get("backend", rel, sha) if cache else None
            if data is None:
                data = _parse_source(raw.decode("utf-8", errors="replace"), rel)
                files_parsed += 1
                if cache:
                    cache.put("backend", rel, sha, data)
            else:
                files_cached += 1
            all_routes.extend(RouteInfo(**r) for r in data["routes"])
            all_functions.update(
                {q: FunctionInfo(**fi) for q, fi in data["functions"].items()})
            all_models.extend((m, rel, ln) for m, ln in data["models"])
            all_pydantic.update(data.get("pydantic_models", {}))
            all_imports[data["module"]] = data["imports"]
            if data.get("url_entries"):
                all_url_entries[data["module"]] = data["url_entries"]
            if data.get("drf"):
                all_drf[data["module"]] = data["drf"]
            all_gql.extend(data.get("gql", []))

    if cache:
        cache.prune("backend", seen_files)

    all_routes.extend(_django_routes(all_url_entries, all_drf,
                                     all_imports, all_functions))

    # graphql resolvers -> pseudo-path routes; the standard endpoint loop
    # then gives them static flags and call-graph walking for free
    for res in all_gql:
        info = all_functions.get(res["handler"])
        all_routes.append(RouteInfo(
            method=res["kind"], path=f"/graphql#{res['field']}",
            handler=res["handler"],
            file=info.file if info else "", line=res["line"],
            framework="graphql",
            has_auth_dep=info.auth_hint if info else False))

    # --- nodes: ORM models ---
    for mname, mfile, mline in all_models:
        graph.add_node(Node(
            id=f"db:{mname}", type=NodeType.DB_MODEL, label=mname,
            file=mfile, line=mline,
        ))

    # --- nodes: endpoints + reachable functions ---
    for r in all_routes:
        full_path = (r.router_prefix.rstrip("/") + "/" + r.path.lstrip("/")).rstrip("/") or "/"
        ep_id = f"ep:{r.method} {full_path}"
        info = all_functions.get(r.handler)
        meta = {"handler": r.handler, "framework": r.framework,
                "has_auth": r.has_auth_dep, "raw_path": full_path,
                "handler_end_line": info.end_line if info else 0}
        if r.response_model and r.response_model in all_pydantic:
            meta["response_model"] = r.response_model
            meta["response_fields"] = sorted(
                _resolve_model_fields(r.response_model, all_pydantic))
        if r.request_model and r.request_model in all_pydantic:
            meta["request_model"] = r.request_model
            meta["request_fields"] = sorted(
                _resolve_model_fields(r.request_model, all_pydantic))
            meta["request_required"] = sorted(
                _resolve_required_fields(r.request_model, all_pydantic))
        graph.add_node(Node(
            id=ep_id, type=NodeType.ENDPOINT, label=f"{r.method} {full_path}",
            file=r.file, line=r.line, meta=meta,
        ))
        _walk_calls(graph, ep_id, r.handler, all_functions, all_imports,
                    depth=0, seen=set())

        # --- static risk signals on the handler ---
        if info:
            if info.io_calls_outside_try and not info.has_try:
                first = info.io_calls_outside_try[0]
                graph.flag_node(ep_id, RiskFlag(
                    code="no_error_handling", severity="high", category="quality",
                    message="Handler performs I/O with no try/except",
                    evidence=f"{info.file}:{first[0]} calls `{first[1]}` outside any try block",
                    suggestion="Wrap I/O in try/except and return a controlled error response",
                ))
            if info.complexity > 10:
                graph.flag_node(ep_id, RiskFlag(
                    code="high_complexity", severity="medium", category="quality",
                    message=f"Cyclomatic complexity {info.complexity} (>10)",
                    evidence=f"{info.file}:{info.line} `{info.qname}`",
                    suggestion="Split the handler into smaller service functions",
                ))
            for line, snippet in info.raw_sql_interp:
                graph.flag_node(ep_id, RiskFlag(
                    code="sql_injection_risk", severity="critical", category="security",
                    message="Raw SQL built with string interpolation",
                    evidence=f"{info.file}:{line} `{snippet}`",
                    suggestion="Use parameterized queries or the ORM",
                ))
            # cross-function taint (6.1): request param flows into SQL built
            # in another function, up to 2 hops through the qualified graph
            taint = _sql_taint(r.handler, all_functions, all_imports)
            if taint and taint[1] != r.handler:
                param, wfqn, wline = taint
                wfile = all_functions[wfqn].file
                graph.flag_node(ep_id, RiskFlag(
                    code="sql_injection_risk", severity="critical",
                    category="security",
                    message="Request data flows into raw SQL built in "
                            f"`{wfqn}`",
                    evidence=f"{r.file}:{r.line} passes `{param}` -> "
                             f"{wfile}:{wline} string-interpolated execute()",
                    suggestion="Parameterize the query in the downstream "
                               "function, or validate/escape before passing",
                ))
        mutating = r.method in ("POST", "PUT", "DELETE", "PATCH")
        if mutating and not r.has_auth_dep:
            graph.flag_node(ep_id, RiskFlag(
                code="missing_auth", severity="high", category="security",
                message=f"{r.method} endpoint has no detectable auth dependency",
                evidence=f"{r.file}:{r.line} handler `{r.handler}`",
                suggestion="Add an auth dependency (e.g. Depends(get_current_user)) "
                           "or mark it public intentionally",
            ))

    return {"routes": len(all_routes), "functions": len(all_functions),
            "models": len(all_models),
            "files_parsed": files_parsed, "files_cached": files_cached}


def _walk_calls(graph: Graph, parent_id: str, fqn: str,
                functions: dict[str, FunctionInfo],
                all_imports: dict[str, dict], depth: int, seen: set):
    """Follow project-local calls from a handler, max 4 levels deep.
    Callees resolve through the calling module's import map."""
    if depth > 4 or fqn in seen:
        return
    seen.add(fqn)
    info = functions.get(fqn)
    if not info:
        return
    # ORM edges
    for model in info.orm_models:
        mid = f"db:{model}"
        if mid in graph.nodes:
            graph.add_edge(Edge(
                id=f"{parent_id}->q:{model}", source=parent_id, target=mid,
                type=EdgeType.QUERIES,
            ))
    imports = all_imports.get(info.module, {})
    for callee in info.calls:
        target = _resolve_callee(callee, info, imports, functions)
        if target and target != fqn:
            t = functions[target]
            fid = f"fn:{target}"
            graph.add_node(Node(
                id=fid, type=NodeType.FUNCTION, label=t.qname,
                file=t.file, line=t.line,
                meta={"complexity": t.complexity, "end_line": t.end_line,
                      "module": t.module},
            ))
            graph.add_edge(Edge(
                id=f"{parent_id}->{fid}", source=parent_id, target=fid,
                type=EdgeType.CALLS,
            ))
            _walk_calls(graph, fid, target, functions, all_imports,
                        depth + 1, seen)
