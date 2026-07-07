# wiremap

Full-stack wire mapping for Python + React codebases. Scans your backend and
frontend, traces every wire — component → API call → endpoint → function →
database model — flags the weak ones with file:line evidence and a concrete
fix, and renders it all in an interactive local viewer.

## Install & run

```bash
pip install -e .
wiremap scan /path/to/your/project          # auto-detects backend/ and frontend/
wiremap scan . --backend server --frontend client --serve
```

Output goes to `<project>/.wiremap/`:
- `graph.json` — the full graph (nodes, edges, flags, scores)
- `wiremap.html` — self-contained interactive viewer (open in any browser)

`--serve` starts a local server and opens the viewer automatically.

## What it detects today (v0.1)

**Wiring**
- FastAPI routes (`@app.get`, `APIRouter` with prefixes) and basic Flask routes
- React call sites: `fetch()`, `axios.get/post/put/delete/patch`, generic
  `api.*`/`client.*` wrappers, with template-literal URL resolution
- Cross-stack matching: `` `/api/users/${id}` `` ↔ `/api/users/{user_id}`
- Backend call graph: handler → services → ORM models (SQLAlchemy)
- Confidence per wire: certain / probable / inferred (dashed in the viewer)

**Risk flags** (each with evidence + suggested fix)
| code | category | what it means |
|---|---|---|
| `orphan_call` | contract | frontend calls a route that doesn't exist — will 404 |
| `unused_endpoint` | contract | no frontend caller — dead code or external consumer |
| `missing_auth` | security | mutating endpoint with no auth dependency |
| `sql_injection_risk` | security | raw SQL built via f-string/concatenation |
| `no_error_handling` | quality | I/O without try/except or promise without .catch |
| `no_timeout` | operational | frontend call with no abort/timeout |
| `high_complexity` | quality | handler cyclomatic complexity > 10 |
| `hub_function` | operational | single function many wires route through (SPOF) |
| `unresolvable_url` | contract | fully dynamic URL, wire untraceable |

## Configuration

Drop a `wiremap.yaml` in your project root to tune scoring:

```yaml
weights:
  security: 2.0
  contract: 1.5
  quality: 1.0
  operational: 1.0
severity: { low: 1, medium: 3, high: 6, critical: 10 }
hub_fanin_threshold: 3
```

## Architecture

```
extractors/python_backend.py   ast-based route + call-graph + risk extraction
extractors/react_frontend.py   tree-sitter (js/jsx/ts/tsx) call-site extraction
matcher.py                     canonical route matching, orphan detection
risk.py                        weighted composite scoring, hub detection
graph.py                       unified node/edge model, JSON serialization
cli.py                         scan command, viewer generation, local server
viewer_template.html           zero-dependency interactive wire map
```

Everything runs locally; no code leaves your machine.

## Roadmap

- **Phase 2 — runtime layer.** OpenTelemetry collector that matches live spans
  to graph edges: traffic, p95 latency, error rate per wire; dead-wire
  confirmation. Coverage.py mapping onto handler nodes. Incremental re-scans
  (hash-based, changed files only).
- **Phase 3 — team mode.** Docker self-hosted deployment, GitHub Action that
  comments on PRs with affected wires and risk-score delta, Pydantic-vs-
  TypeScript response contract checking, Django/Express adapters.

## Extending

Framework support is adapter-based: to add Django, implement route discovery
in a new function in `python_backend.py` following the FastAPI/Flask pattern
and emit `RouteInfo` objects — the matcher and everything downstream is
framework-agnostic.
