# wiremap GitHub Action

Runs `wiremap scan` on the PR head and on the base branch, diffs the two
graphs (`wiremap diff`), posts the result as a PR comment and a job-summary
section, and fails the job when new flags at or above `fail-on` appear.

```yaml
name: wiremap
on: pull_request

permissions:
  contents: read
  pull-requests: write   # for the PR comment

jobs:
  wiremap:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with: { python-version: '3.12' }
      - uses: <owner>/wiremap/action@main
        with:
          fail-on: critical        # '' to disable the merge gate
          # backend: server        # optional, relative to repo root
          # frontend: web
```

The comment shows wires added/removed/changed, flags introduced/resolved
(matched by `(node_id, code)`), and the total risk delta. The gate step is
a plain `wiremap diff --fail-on <severity>`, so the same check runs
locally:

```bash
wiremap scan . --out /tmp/head
git worktree add /tmp/base origin/main && wiremap scan /tmp/base --out /tmp/base-out
wiremap diff /tmp/base-out/graph.json /tmp/head/graph.json --fail-on critical
```
