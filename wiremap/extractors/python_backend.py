"""Static extractor for Python backends (FastAPI first, Flask basic).

Walks every .py file, finds route registrations, then walks the call graph
from each handler down through project-local functions to ORM usage.
Also attaches per-handler static risk signals with file:line evidence.
"""
from __future__ import annotations

import ast
import os
from dataclasses import dataclass, field, asdict

from ..cache import FileCache, content_hash
from ..graph import Graph, Node, Edge, NodeType, EdgeType, Confidence, RiskFlag

HTTP_METHODS = {"get", "post", "put", "delete", "patch", "head", "options"}
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


@dataclass
class FunctionInfo:
    qname: str
    file: str
    line: int
    end_line: int
    calls: list = field(default_factory=list)      # names called inside
    has_try: bool = False
    io_calls_outside_try: list = field(default_factory=list)
    raw_sql_interp: list = field(default_factory=list)   # (line, snippet)
    complexity: int = 1
    orm_models: list = field(default_factory=list)


class _ModuleVisitor(ast.NodeVisitor):
    """Collects routes, functions, router prefixes, and ORM models in one file."""

    def __init__(self, filepath: str, rel: str):
        self.filepath = filepath
        self.rel = rel
        self.routes: list[RouteInfo] = []
        self.functions: dict[str, FunctionInfo] = {}
        self.router_prefixes: dict[str, str] = {}   # var name -> prefix
        self.orm_models: list[tuple[str, int]] = []
        self.pydantic_models: dict[str, dict] = {}  # name -> {fields, bases}
        self._class_stack: list[str] = []

    # --- router prefix detection: APIRouter(prefix="/api/users") ---
    def visit_Assign(self, node: ast.Assign):
        if isinstance(node.value, ast.Call):
            fn = node.value.func
            name = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", "")
            if name == "APIRouter":
                prefix = ""
                for kw in node.value.keywords:
                    if kw.arg == "prefix" and isinstance(kw.value, ast.Constant):
                        prefix = kw.value.value
                for tgt in node.targets:
                    if isinstance(tgt, ast.Name):
                        self.router_prefixes[tgt.id] = prefix
        self.generic_visit(node)

    def visit_ClassDef(self, node: ast.ClassDef):
        bases = []
        for b in node.bases:
            bases.append(b.attr if isinstance(b, ast.Attribute) else getattr(b, "id", ""))
        if any(b in ("Base", "Model", "DeclarativeBase") for b in bases):
            self.orm_models.append((node.name, node.lineno))
        if "BaseModel" in bases or any(b in self.pydantic_models for b in bases):
            fields = [s.target.id for s in node.body
                      if isinstance(s, ast.AnnAssign) and isinstance(s.target, ast.Name)]
            self.pydantic_models[node.name] = {"fields": fields, "bases": bases}
        self._class_stack.append(node.name)
        self.generic_visit(node)
        self._class_stack.pop()

    def visit_FunctionDef(self, node):
        self._handle_function(node)

    def visit_AsyncFunctionDef(self, node):
        self._handle_function(node)

    def _handle_function(self, node):
        qname = ".".join(self._class_stack + [node.name]) if self._class_stack else node.name
        info = FunctionInfo(
            qname=qname, file=self.rel, line=node.lineno,
            end_line=getattr(node, "end_lineno", node.lineno),
        )
        analyzer = _FunctionBodyAnalyzer()
        for stmt in node.body:
            analyzer.visit(stmt)
        info.calls = analyzer.calls
        info.has_try = analyzer.has_try
        info.io_calls_outside_try = analyzer.io_outside_try
        info.raw_sql_interp = analyzer.raw_sql
        info.complexity = analyzer.complexity
        info.orm_models = analyzer.orm_models
        self.functions[qname] = info

        # route decorators
        for dec in node.decorator_list:
            route = self._route_from_decorator(dec, qname, node)
            if route:
                self.routes.append(route)
        self.generic_visit(node)

    def _route_from_decorator(self, dec, qname, fn_node) -> RouteInfo | None:
        if not isinstance(dec, ast.Call):
            return None
        f = dec.func
        # FastAPI: @app.get("/x") / @router.post("/y")
        if isinstance(f, ast.Attribute) and f.attr in HTTP_METHODS:
            owner = getattr(f.value, "id", "")
            path = ""
            if dec.args and isinstance(dec.args[0], ast.Constant):
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
            return RouteInfo(
                method=f.attr.upper(), path=path, handler=qname,
                file=self.rel, line=fn_node.lineno, framework="fastapi",
                has_auth_dep=self._has_auth_dependency(fn_node),
                router_prefix=prefix, response_model=response_model,
            )
        # Flask: @app.route("/x", methods=["POST"])
        if isinstance(f, ast.Attribute) and f.attr == "route":
            path = ""
            if dec.args and isinstance(dec.args[0], ast.Constant):
                path = str(dec.args[0].value)
            methods = ["GET"]
            for kw in dec.keywords:
                if kw.arg == "methods" and isinstance(kw.value, (ast.List, ast.Tuple)):
                    methods = [e.value for e in kw.value.elts if isinstance(e, ast.Constant)]
            return RouteInfo(
                method=methods[0].upper(), path=path, handler=qname,
                file=self.rel, line=fn_node.lineno, framework="flask",
                has_auth_dep=self._has_auth_dependency(fn_node),
            )
        return None

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


class _FunctionBodyAnalyzer(ast.NodeVisitor):
    def __init__(self):
        self.calls: list[str] = []
        self.has_try = False
        self._try_depth = 0
        self.io_outside_try: list[tuple[int, str]] = []
        self.raw_sql: list[tuple[int, str]] = []
        self.complexity = 1
        self.orm_models: list[str] = []

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
        self.generic_visit(node)


def _parse_source(source: str, rel: str) -> dict:
    """Parse one file into a JSON-serializable extraction record (cacheable)."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return {"routes": [], "functions": {}, "models": []}
    v = _ModuleVisitor(rel, rel)
    v.visit(tree)
    return {
        "routes": [asdict(r) for r in v.routes],
        "functions": {q: asdict(fi) for q, fi in v.functions.items()},
        "models": [[m, ln] for m, ln in v.orm_models],
        "pydantic_models": v.pydantic_models,
    }


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


def extract_backend(root: str, graph: Graph,
                    cache: FileCache | None = None) -> dict:
    """Scan a Python source tree, populate graph, return summary stats."""
    all_functions: dict[str, FunctionInfo] = {}
    all_routes: list[RouteInfo] = []
    all_models: list[tuple[str, str, int]] = []
    all_pydantic: dict[str, dict] = {}
    files_parsed = files_cached = 0
    seen_files: set[str] = set()

    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in
                       (".git", "node_modules", "__pycache__", ".venv", "venv", "migrations")]
        for fn in filenames:
            if not fn.endswith(".py"):
                continue
            full = os.path.join(dirpath, fn)
            rel = os.path.relpath(full, root)
            with open(full, "rb") as f:
                raw = f.read()
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

    if cache:
        cache.prune("backend", seen_files)

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
        graph.add_node(Node(
            id=ep_id, type=NodeType.ENDPOINT, label=f"{r.method} {full_path}",
            file=r.file, line=r.line, meta=meta,
        ))
        _walk_calls(graph, ep_id, r.handler, all_functions, depth=0, seen=set())

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


def _walk_calls(graph: Graph, parent_id: str, fname: str,
                functions: dict[str, FunctionInfo], depth: int, seen: set):
    """Follow project-local calls from a handler, max 4 levels deep."""
    if depth > 4 or fname in seen:
        return
    seen.add(fname)
    info = functions.get(fname)
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
    for callee in info.calls:
        if callee in functions and callee != fname:
            fid = f"fn:{callee}"
            graph.add_node(Node(
                id=fid, type=NodeType.FUNCTION, label=callee,
                file=functions[callee].file, line=functions[callee].line,
                meta={"complexity": functions[callee].complexity,
                      "end_line": functions[callee].end_line},
            ))
            graph.add_edge(Edge(
                id=f"{parent_id}->{fid}", source=parent_id, target=fid,
                type=EdgeType.CALLS,
            ))
            _walk_calls(graph, fid, callee, functions, depth + 1, seen)
