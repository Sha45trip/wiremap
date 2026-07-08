# wiremap

Full-stack wire mapping for Python + React codebases. Scans your backend and
frontend, traces every wire — component → API call → endpoint → function →
database model — flags the weak ones with file:line evidence and a concrete
fix, and renders it all in an interactive local viewer. Overlay live traffic
from OpenTelemetry and test coverage from coverage.py to see which risky
wires actually matter in production.

No single existing tool sees the whole wire: static analyzers don't know
which flagged handler your checkout button calls or whether it takes
traffic; APM tools see latency but not the missing auth check behind it.
wiremap joins both sides of that gap — its `hot_fragile` flag means "this
endpoint takes top-decile traffic AND carries a high/critical static flag",
which is the first thing worth fixing in any codebase.

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
Re-scans are incremental: unchanged files are served from a content-hash
cache (`--no-cache` forces a full re-parse).

## Runtime overlay (OpenTelemetry)

Run the collector, point standard OTel auto-instrumentation at it, and
re-scan — no custom instrumentation, no code changes:

```bash
wiremap collect .                           # OTLP/JSON receiver on :4318

# in your backend's environment:
OTEL_EXPORTER_OTLP_ENDPOINT=http://localhost:4318 \
OTEL_EXPORTER_OTLP_PROTOCOL=http/json \
opentelemetry-instrument --traces_exporter otlp uvicorn app.main:app

wiremap scan .                              # merges .wiremap/runtime.json
```

Endpoints gain `req_count`, `p50_ms`, `p95_ms`, and `error_rate` over a
rolling window (default 24h, `--window`), shown in the viewer's evidence
panel. Spans are matched by `http.route`; spans with only a concrete URL
path are matched against route patterns and marked lower-confidence.

Try it on the bundled demo:

```bash
wiremap collect demo          # terminal 1
python demo/replay_spans.py   # terminal 2 — replays canned traffic
wiremap scan demo             # watch hot_fragile appear on POST /api/orders
```

## Coverage overlay (coverage.py)

```bash
coverage json                               # produces coverage.json
wiremap scan . --coverage coverage.json
```

Handlers and functions gain `coverage_pct` (measured over body lines —
import-time execution of `def` lines doesn't count as "tested"), and
untested endpoint handlers are flagged.

## What it detects

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
| `hot_fragile` | operational | top-decile traffic on an endpoint with high/critical static flags |
| `orphan_call` | contract | frontend calls a route that doesn't exist — will 404 |
| `unused_endpoint` | contract | no frontend caller — dead code or external consumer |
| `confirmed_dead` | contract | statically unreferenced AND zero traffic in window |
| `missing_auth` | security | mutating endpoint with no auth dependency |
| `sql_injection_risk` | security | raw SQL built via f-string/concatenation |
| `no_error_handling` | quality | I/O without try/except or promise without .catch |
| `untested_handler` | quality | endpoint handler with 0% (high) or <50% (medium) coverage |
| `high_latency` | operational | p95 above threshold (default 1000ms) |
| `high_error_rate` | operational | more than 2% of responses are 5xx |
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
runtime:
  p95_ms_threshold: 1000
  error_rate_threshold: 0.02
```

## Architecture

```
extractors/python_backend.py   ast-based route + call-graph + risk extraction
extractors/react_frontend.py   tree-sitter (js/jsx/ts/tsx) call-site extraction
matcher.py                     canonical route matching, orphan detection
risk.py                        weighted composite scoring, hub detection
cache.py                       sha256-keyed per-file cache for incremental scans
coverage.py                    coverage.py JSON -> coverage_pct + untested_handler
collector.py                   OTLP/JSON receiver, rolling-window store, runtime flags
graph.py                       unified node/edge model, JSON serialization
cli.py                         scan/collect commands, viewer generation, local server
viewer_template.html           zero-dependency interactive wire map
```

Everything runs locally; no code leaves your machine. Dependencies are
tree-sitter (+ language wheels) and pyyaml only — the collector is stdlib
`http.server`, accepting the OTLP JSON encoding so no protobuf is needed.

## Development

```bash
pip install -e .[dev]
python -m pytest tests/            # 109 tests
python tests/regen_golden.py       # regenerate the golden graph deliberately
```

Fixtures follow a planted-problem/near-miss pattern: every detector has a
case that must fire and a neighbor that must not. Precision beats recall.

## Roadmap

Done: test harness, incremental scans, coverage mapping, runtime telemetry
collector. Next, in order:

- **Contract checking** — Pydantic `response_model` field sets vs the fields
  the frontend actually reads from responses; `contract_mismatch` flag.
- **Viewer scalability** — pan/zoom, risk filter, search, group-by-file.
- **Team mode** — Docker self-hosted (collector + viewer service),
  `wiremap diff` + GitHub Action PR comments with `--fail-on critical`
  merge gating, Django/Flask-blueprint/React-Query/OpenAPI adapters,
  module-qualified call graph.

## Extending

Framework support is adapter-based: to add Django, implement route discovery
in a new function in `python_backend.py` following the FastAPI/Flask pattern
and emit `RouteInfo` objects — the matcher and everything downstream is
framework-agnostic.
