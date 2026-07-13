# Changelog

All notable changes to wiremap are documented here. Versions follow
[semantic versioning](https://semver.org/).

## [0.2.0] — 2026-07-13

The reach + depth + hardening release. Everything in ROADMAP-v2 Phases 4–7.

### Added
- **Framework adapters**: Express/Node, GraphQL (SDL + Strawberry/Graphene
  resolvers + `gql` client documents), Next.js API routes + tRPC.
- **TypeScript response types**: `axios.get<T>` / `useQuery<T>` generics
  resolve against local interfaces for probable-confidence contract checks.
- **Cross-function SQL-injection taint** (2 hops) over the qualified call graph.
- **Request-body contract checking**: `request_contract_mismatch` and
  `missing_request_field`, the mirror of the response-side check.
- **Router/middleware-scope auth modeling**: FastAPI `APIRouter(dependencies=)`,
  per-route `dependencies=`, `Annotated[T, Depends(auth)]` incl. cross-file
  type aliases, and Express `router.use(auth)` — kills the biggest
  `missing_auth` false-positive class.
- **Per-wire runtime attribution**: browser OTel spans joined to server spans
  by trace id; `hot_fragile` names the most-exposed caller component.
- **Scan history & trends**: `.wiremap/history.json` snapshots + a viewer
  trend strip (risk over time, flags introduced/resolved).
- **Protobuf OTLP ingestion** via the `wiremap[otlp]` extra (JSON stays
  dependency-free).
- **`wiremap explain <root> <node_id>`**: shows how a risk score is computed.
- **Precision benchmark** harness (`bench/`) against real OSS repos.
- **Per-user team-mode tokens** via `WIREMAP_TOKENS` (`label:token,...`).
- **`wiremap --version`**.

### Changed
- Graph node ids / evidence use forward slashes everywhere (portable CI diffs).
- Module-qualified call graph (no cross-module name collisions).
- Duplicate flags (same code + evidence) are de-duplicated.
- Runtime store caps observations per endpoint (`MAX_OBS_PER_KEY`) so a
  high-traffic endpoint can't grow it without bound.

### Fixed
- Scans no longer crash on unparseable or unreadable source files.
- Computed route paths no longer fabricate a phantom `GET /` endpoint.
- Mass false `orphan_call` on unsupported route styles is downgraded via a
  match-rate discovery guard.

## [0.1.0]

Initial release: Python (FastAPI/Flask) + React wire graph, static risk
flags, incremental scans, coverage + runtime overlays, self-contained
viewer, Docker team mode, and the `wiremap diff` CI merge gate.
