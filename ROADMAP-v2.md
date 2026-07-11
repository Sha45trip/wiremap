# ROADMAP v2 — from "roadmap complete" to "production credible"

The v1 roadmap (Phases 1–3) is fully implemented: static wire graph, five
framework adapters, incremental scans, coverage/runtime/contract overlays,
scalable viewer, Docker team mode, CI diff gate. 201 tests.

This roadmap addresses two things: the places mature tools genuinely beat
us (analysis depth, stack breadth, runtime fidelity, battle-testing) and
the known-limitations list. Ordering principle: **trust before reach before
depth** — nothing else matters if precision doesn't hold on real codebases,
and no depth work pays off on stacks we can't parse.

Work top-to-bottom within a phase. Same conventions as v1: evidence or it
didn't happen; precision beats recall; every detector ships with a fixture
that must fire and a near-miss that must not.

---

## Phase 4 — Trust (validation & correctness). Gate for everything else.

### 4.0 Cross-OS graph determinism  (S)
Node ids and evidence strings embed `\` on Windows; a diff between a
Windows dev machine and Linux CI misreports every wire as added+removed.
Normalize path separators at graph-write time (single choke point in
`graph.py`), regenerate golden, add a separator-invariance test.
Do this first — it's small and silently corrupts 3.2's merge gate.

### 4.1 Real-world precision benchmark  (L)
Every heuristic was tuned on fixtures. Assemble a corpus of 10–20 real
OSS full-stack repos (FastAPI+React, Django+DRF+React, Flask). For each:
scan, sample ≥30 flags per detector, hand-label true/false positive.
- Deliverable: `bench/` harness + a PRECISION.md table per detector.
- Acceptance: high/critical detectors ≥90% precision on the corpus, or
  the detector gets demoted (severity down, or confidence-gated) until
  it clears the bar. Publish the numbers — they are the sales pitch.
- Known suspects to re-examine: `_call_features` substring matching
  ("timeout" anywhere in the chain), auth hints by name smell,
  `_expected_fields` on unusual response shapes.

### 4.2 Dogfood the GitHub Action  (S)
The action has never run on a real PR. Set up CI on this repo itself:
pytest matrix (Linux + Windows) plus the wiremap action gating PRs with
`fail-on: high`. Fix what breaks. (Needs the repo pushed to GitHub —
coordinate with the owner before any git operations.)

### 4.3 Risk-model calibration  (M)
Replace magic numbers with defensible defaults: hot_fragile decile and
latency/error thresholds become percentile-based against the scanned
project's own distribution where sample size allows; document the math.
De-duplicate stacked flags (same root cause reported at call + endpoint).
Add `wiremap scan --explain <node_id>` printing exactly why a score is
what it is.

---

## Phase 5 — Reach (stack breadth). Ordered by market size.

### 5.1 Protobuf OTLP ingestion  (M)
Most OTel exporters default to `http/protobuf`; JSON-only is an adoption
blocker (today we 415 and ask users to reconfigure). Accept protobuf on
`/v1/traces` via `opentelemetry-proto` as an optional extra
(`pip install wiremap[otlp]`); JSON path stays dependency-free.
Acceptance: replay script gains a protobuf mode; both encodings produce
identical runtime.json.

### 5.2 Express/Node backend adapter  (L)
Biggest backend ecosystem we don't cover, and tree-sitter JS is already
a dependency. Detect `app.get/post/...`, `express.Router()` with mount
prefixes, middleware-chain auth smell (`requireAuth`-ish names in the
handler chain). Emit RouteInfo-equivalents; matcher unchanged. Fixtures
with planted/near-miss per convention.

### 5.3 GraphQL  (L)
Today a GraphQL app produces an empty map — worst single coverage hole.
- Schema side: parse SDL (or introspection JSON) → one endpoint node per
  root field (`query.user`, `mutation.createOrder`), CERTAIN.
- Client side: `gql` tagged templates → operation → wire to the root
  field. Apollo/urql hooks get the React Query error-handling treatment.
- Resolver mapping (Strawberry/Graphene/Ariadne class+decorator patterns)
  connects fields to backend functions so the call graph continues.

### 5.4 Next.js + tRPC  (M)
Next API routes and server actions are file-convention routes (cheap to
add on tree-sitter). tRPC: procedures defined in routers → endpoints;
typed client calls (`trpc.user.byId.useQuery`) → wires. Both are
high-signal because the community currently has zero wire tooling.

### 5.5 TypeScript type integration for contracts  (M)
When the frontend is TS, response types (`useQuery<User>` generics,
`axios.get<User>`) give exact expected-field sets — upgrade
`expected_fields` from INFERRED to PROBABLE and catch nullability
mismatches, not just missing fields.

---

## Phase 6 — Depth (analysis quality)

### 6.1 Cross-function taint for SQL injection  (L)
We already have the qualified call graph — use it. Track
request-derived params two hops through project functions into
`execute()` interpolation. Still heuristic, but catches the
"handler passes payload to a service that builds SQL" case CodeQL
catches and we currently miss. Precision rule: only flag when the
tainted variable name provably reaches the f-string.

### 6.2 Request-body contract checking  (M)
Mirror of 2.4: fields the frontend sends (`axios.post(url, {a, b})`)
vs the Pydantic request model — flags for silently-dropped fields and
missing required ones.

### 6.3 Middleware/dependency-scope auth modeling  (M)
Kill the biggest missing_auth false-positive class: router-level
`dependencies=[Depends(auth)]` (FastAPI), Django middleware +
`LOGIN_REQUIRED_URLS`-style config, Express `router.use(auth)`.
An endpoint guarded at the router level is not "missing auth".

### 6.4 Per-wire runtime attribution  (L)
Spans tell us endpoint traffic but not which frontend caller sent it.
Consume standard OTel *browser* spans (no custom instrumentation — the
v1 non-goal stands) and join client→server traces by traceparent to
weight individual wires, not just endpoints. hot_fragile then names the
component whose users are exposed.

### 6.5 History & trends  (M)
Keep per-scan snapshots (graph hash + stats) in `.wiremap/history/`;
viewer gets a trend strip (risk over time, flags introduced/resolved per
week). This is the retention feature APMs have and we don't — and it
makes the team server worth leaving running.

---

## Phase 7 — Team/production hardening

- Real authn for team mode: per-user tokens at minimum, OIDC if demand;
  TLS guidance (reverse-proxy docs, not homegrown TLS).
- Runtime store scale: rotation/compaction for the observation lists,
  sampling above a configurable req/s.
- Multi-repo workspaces: one viewer over N repos (monorepo teams ask
  for this first).
- PyPI release, versioning policy, CHANGELOG — v0.2.0 the moment 4.0–4.2
  land.

---

## SaaS? Recommendation: not a classic SaaS — open-core with an optional
hosted control plane, decided by a traction gate.

**Why not SaaS-first.** (1) The differentiator is *local-first*: "your
code never leaves your machine" is the one structural advantage over
Datadog Code Security — hosting people's source surrenders it and moves
the fight onto Datadog's turf (hosted, integrated, sales army).
(2) Source-access SaaS is the highest-trust sale in devtools; unproven
precision (Phase 4) makes that sale impossible today. (3) Multi-tenancy,
SOC2, SLAs are a company's worth of work that buys zero product truth.

**What to do instead.**
1. **Now:** ship the scanner as OSS (PyPI + GitHub), free forever,
   local-first as identity. Run Phase 4 in public — published precision
   numbers are marketing.
2. **If traction** (signals: external issues/PRs, action installs,
   unsolicited "does it do Express?"): add **wiremap cloud** — a hosted
   home for *graphs, not code*: history/trends, PR-diff artifact storage
   (the action currently re-scans base every run), team dashboards,
   managed OTLP ingest. graph.json is metadata (routes, ids, evidence
   strings), a categorically easier trust sale than source hosting, and
   the self-hosted Docker path stays free so the wedge survives.
3. **Pricing anchor:** teams pay for the merge gate + history + hosted
   ingest, never for the scanner. Sentry/Grafana open-core economics.

**Decision gate:** revisit after Phase 4 + a public v0.2. Building a
control plane before anyone runs the scanner is building a roof first.
