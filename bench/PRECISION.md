# Precision report — first corpus pass (2026-07-11)

Method: `bench/run_bench.py` scans the corpus, samples flags per detector
(seeded), and the samples were labeled by opening the cited `file:line`.
This is an **initial pass** — 3 repos, ~25 flags inspected. It already
reshaped the tool (see fixes). Corpus:

| repo | stack | why chosen |
|---|---|---|
| fastapi/full-stack-fastapi-template | FastAPI + React TS | the canonical supported stack |
| getredash/redash | Flask + React | mature real-world Flask app |
| apache/superset | Flask (AppBuilder) + React TS | scale stress test (4.7k files) |

## Bugs found and fixed during this pass

1. **SyntaxError files crashed the whole scan** — the unparseable-file
   fallback was missing keys added after 2.1. Real repos contain broken/
   py2 files; fixtures didn't. Fixed + regression test.
2. **Windows MAX_PATH** — long checkout paths (superset docs/) broke the
   corpus clone; unreadable files crashed extraction. Extractors now skip
   unreadable files; bench clones with `core.longpaths` per-invocation.
3. **Computed route paths fabricated `ep:GET /`** — redash registers
   routes as `@routes.route(org_scoped_rule("/login"))`; the non-literal
   path became `""` → `/`, piling many handlers' flags onto one bogus
   endpoint. Now skipped entirely (precision rule) + near-miss fixture.
4. **Mass false orphans on unsupported stacks** — redash: 90/111 calls
   "orphaned" because Flask-RESTful `add_resource()` routes aren't
   discovered. New discovery guard: when ≥20 calls and <25% match,
   orphan_call downgrades to low severity with an explicit
   "route discovery may not cover this stack" note.

## Sample verdicts (initial labels)

| detector | inspected | verdict summary |
|---|---|---|
| high_complexity | 2 | measurement TP (redash `login` cc=13 is real); owner id was wrong pre-fix-3 |
| missing_auth | 5 | 1 FP (`verification_email` uses `current_user` — auth by context), 3 DEBATABLE (password-reset/setup routes are public **by design**; the flag's "mark it public intentionally" wording carries it), 1 TP-shaped (SAML endpoint warrants review) |
| no_error_handling (JS) | 5 | mostly DEBATABLE-to-FP: redash service modules **return** the promise (`delete: data => axios.delete(...)`) — the caller owns error handling. See "next fixes". |
| no_error_handling (PY) | 2 | TP-shaped (ldap handler does I/O outside try) |
| unused_endpoint | 4 (template) | all FP **as a set** — the template's frontend uses a generated `UsersService.*` client we don't recognize, so 0 calls were found |
| no_timeout | glance | technically-true noise at volume (134 across corpus); low severity is correct, but see "next fixes" |

## Honest read

Detector *logic* precision is decent when route/call discovery covers the
stack; the dominant real-world failure mode is **discovery coverage**, not
detector logic. On unsupported registration styles the contract flags
(orphan/unused) invert from signal to noise — hence the discovery guard,
which turns "90 high-severity false alarms" into "90 low-severity hints
plus one honest sentence about coverage".

## Discovery gaps confirmed by the corpus (next adapters, in impact order)

1. Flask-RESTful `api.add_resource(Resource, "/path")` (redash's entire API)
2. Flask-AppBuilder `@expose` class routes (superset's entire API)
3. Generated TS clients without a repo-committed `openapi.json`
   (fastapi-template's `UsersService.*` — heuristic: `*Service.method`)

## Next detector fixes (not yet implemented)

1. **no_error_handling: returned promises** — when the fluent chain is the
   `return` value (or an arrow expression body of an exported service fn),
   the caller owns error handling; firing at the definition site is noise.
   Needs care: the demo's planted flags use arrow-body fetches.
2. **no_timeout volume** — consider firing once per file or only on
   mutating calls; 100+ identical low flags bury the signal.
3. **missing_auth on auth-by-context** — a handler that *reads*
   `current_user`/`request.user` is not unauthenticated; suppress.

## Numbers after fixes (re-run)

See RESULTS.md — regenerated after the fixes above; the redash route
count drops (no fabricated `/`), orphan flags carry the guard note.
