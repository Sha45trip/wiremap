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

Both OTLP encodings are accepted: JSON works out of the box; for
`http/protobuf` (most exporters' default) install the extra —
`pip install wiremap[otlp]`.

Try it on the bundled demo:

```bash
wiremap collect demo          # terminal 1
python demo/replay_spans.py   # terminal 2 — replays canned traffic
wiremap scan demo             # watch hot_fragile appear on POST /api/orders
```

## Team mode (Docker self-hosted)

One container serves the viewer, receives OTLP traces, and re-scans the
mounted repo every 15 minutes (or on demand):

```bash
WIREMAP_REPO=/path/to/your/project docker compose up -d
# viewer:   http://localhost:8787/wiremap.html
# traces:   OTEL_EXPORTER_OTLP_ENDPOINT=http://<host>:8787 (http/json)
# re-scan:  curl -X POST http://<host>:8787/rescan
```

Or without Docker: `wiremap serve . --rescan-interval 900`.

## CI merge gate (GitHub Action)

```yaml
- uses: <owner>/wiremap/action@main
  with:
    fail-on: critical
```

Scans the PR head and base branch, comments the diff on the PR (wires
added/removed/changed, flags introduced/resolved by `(node_id, code)`,
total risk delta), and fails the job when introduced flags reach the
`fail-on` severity. The action is a thin wrapper around `wiremap diff`:

```bash
wiremap diff base/graph.json head/graph.json --format md   # PR comment body
wiremap diff base/graph.json head/graph.json --fail-on critical  # exit 1 on new criticals
```

See [action/README.md](action/README.md) for the full workflow.

Security model (v1): intended for **trusted networks** — the viewer and
graph are served without auth. Set `WIREMAP_TOKEN` to require
`Authorization: Bearer <token>` on the mutating routes (`/v1/traces`,
`/rescan`); OTel exporters pass it via
`OTEL_EXPORTER_OTLP_HEADERS="Authorization=Bearer <token>"`. The repo is
mounted read-only; outputs live on a named volume.

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
- FastAPI routes (`@app.get`, `APIRouter` with prefixes), Flask routes and
  blueprints (`url_prefix`, multi-method), Django (`urls.py`
  `path()`/`re_path()`/`include()`, class-based views, DRF routers),
  Express (`app.get(...)`, routers with `app.use()` mounts incl.
  cross-file `require()`, auth-middleware detection)
- OpenAPI: an `openapi.json` in the repo is ingested as the endpoint source
  of truth; generated-client calls (`api.getPetById(...)`) match by
  operationId
- GraphQL: root fields become endpoints (`QUERY user`) from SDL files
  and/or Strawberry/Graphene resolver classes (with call-graph + static
  flags on resolvers); client `gql` documents wire to them — undeclared
  selections are orphans, unqueried fields are unused
- Next.js: file-convention API routes (`pages/api/**`, app-router
  `app/api/**/route.ts`, `[id]`/`[...slug]` params); tRPC: router
  procedures become endpoints and `trpc.user.byId.useQuery()` client
  calls wire to them by dotted path
- TypeScript response types: `axios.get<Item>` / `useQuery<Item>` generics
  resolve against local interfaces/type aliases — required fields become
  the expected contract (probable confidence); optional fields (`?`) are
  tolerated missing
- React call sites: `fetch()`, `axios.get/post/put/delete/patch`, generic
  `api.*`/`client.*` wrappers, with template-literal URL resolution;
  calls inside React Query hooks (`useQuery`/`useMutation`) are recognized
  and not flagged for missing error handling — the hook owns it
- Cross-stack matching: `` `/api/users/${id}` `` ↔ `/api/users/{user_id}`
- Backend call graph: handler → services → ORM models (SQLAlchemy),
  module-qualified via import tracking (`from .services import x` resolves
  to `app.services.x` — same-named functions in different modules stay apart)
- Confidence per wire: certain / probable / inferred (dashed in the viewer)

**Risk flags** (each with evidence + suggested fix)
| code | category | what it means |
|---|---|---|
| `hot_fragile` | operational | top-decile traffic on an endpoint with high/critical static flags |
| `contract_mismatch` | contract | frontend reads (or declares via TS generics) a field the response model doesn't declare |
| `orphan_call` | contract | frontend calls a route that doesn't exist — will 404 |
| `unused_endpoint` | contract | no frontend caller — dead code or external consumer |
| `confirmed_dead` | contract | statically unreferenced AND zero traffic in window |
| `missing_auth` | security | mutating endpoint with no auth dependency |
| `sql_injection_risk` | security | raw SQL built via f-string/concatenation — including request data flowing into SQL built in another function (up to 2 call hops) |
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
server.py                      team-mode daemon: viewer + OTLP + webhook/interval re-scans
diff.py                        graph diffing, PR-comment formatting, --fail-on gate
cli.py                         scan/collect/serve/diff commands, viewer generation
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
collector, contract checking (Pydantic `response_model` field sets vs the
fields the frontend actually reads — declared models only, exact field
names, so it never guesses), viewer scalability (wheel-zoom/drag-pan,
risk-threshold slider, search, collapse-by-file above 60 nodes per column —
still a single self-contained HTML file), Docker self-hosted team mode
(`wiremap serve` + Dockerfile/compose, WIREMAP_TOKEN), CI integration
(`wiremap diff` + GitHub Action with `--fail-on` merge gating), and a
module-qualified call graph (import-tracked resolution — no cross-module
name collisions), and framework adapters (Django incl. CBVs + DRF routers,
Flask blueprints, React Query, OpenAPI clients).

**The roadmap through v0.x is complete.** Candidates beyond it: Express/
Node backends, tRPC, protobuf OTLP, auth for team mode.

## Extending

Framework support is adapter-based: to add Django, implement route discovery
in a new function in `python_backend.py` following the FastAPI/Flask pattern
and emit `RouteInfo` objects — the matcher and everything downstream is
framework-agnostic.
