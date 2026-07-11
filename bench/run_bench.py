"""Real-world precision benchmark harness (ROADMAP-v2 4.1).

Clones a corpus of public OSS full-stack repos (shallow), scans each with
wiremap, and writes:
- bench/RESULTS.md   — per-repo counts: routes, calls, wires, flags by code
- bench/samples/<repo>.md — seeded random sample of flags per detector,
  with evidence, for manual true/false-positive labeling

Usage:  python bench/run_bench.py [--corpus-dir DIR] [--sample 8]
The corpus dir defaults to WIREMAP_BENCH_DIR or ./bench/.corpus (gitignored).
"""
from __future__ import annotations

import argparse
import collections
import json
import os
import random
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from wiremap.cli import perform_scan  # noqa: E402

CORPUS = [
    # (name, git url, backend subdir or None=auto, frontend subdir or None)
    ("fastapi-template", "https://github.com/fastapi/full-stack-fastapi-template",
     "backend", "frontend"),
    ("redash", "https://github.com/getredash/redash",
     "redash", "client"),
    ("superset", "https://github.com/apache/superset",
     "superset", "superset-frontend"),
]

BENCH_DIR = os.path.dirname(os.path.abspath(__file__))


def clone(url: str, dest: str) -> None:
    if os.path.exists(dest):
        print(f"  (cached) {dest}")
        return
    # core.longpaths per-invocation: some repos ship >260-char paths that
    # break Windows checkouts (found with apache/superset docs/)
    subprocess.run(["git", "-c", "core.longpaths=true", "clone", "--depth",
                    "1", "--quiet", url, dest], check=True)


def scan(name: str, root: str, backend: str | None, frontend: str | None,
         out_base: str) -> dict:
    t0 = time.time()
    res = perform_scan(
        root,
        backend=os.path.join(root, backend) if backend else None,
        frontend=os.path.join(root, frontend) if frontend else None,
        out=os.path.join(out_base, name),
        no_cache=True,
    )
    elapsed = time.time() - t0
    with open(res["graph_path"], encoding="utf-8") as f:
        graph = json.load(f)
    return {"res": res, "graph": graph, "seconds": round(elapsed, 1)}


def flag_rows(graph: dict) -> list[dict]:
    rows = []
    for node in graph["nodes"]:
        for fl in node["risk_flags"]:
            rows.append({"owner": node["id"], **fl})
    return rows


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--corpus-dir",
                    default=os.environ.get("WIREMAP_BENCH_DIR",
                                           os.path.join(BENCH_DIR, ".corpus")))
    ap.add_argument("--sample", type=int, default=8,
                    help="flags sampled per detector per repo for labeling")
    args = ap.parse_args()
    os.makedirs(args.corpus_dir, exist_ok=True)
    samples_dir = os.path.join(BENCH_DIR, "samples")
    os.makedirs(samples_dir, exist_ok=True)
    out_base = os.path.join(args.corpus_dir, ".scans")
    rng = random.Random(45)

    results_md = ["# Benchmark results", "",
                  "| repo | files | routes | calls | wires | flags | scan s |",
                  "|---|---|---|---|---|---|---|"]
    totals: collections.Counter = collections.Counter()

    for name, url, backend, frontend in CORPUS:
        print(f"== {name}")
        root = os.path.join(args.corpus_dir, name)
        clone(url, root)
        r = scan(name, root, backend, frontend, out_base)
        graph, res = r["graph"], r["res"]
        rows = flag_rows(graph)
        by_code = collections.Counter(row["code"] for row in rows)
        totals.update(by_code)
        files = res["b"]["files_parsed"] + res["f"]["files_parsed"]
        results_md.append(
            f"| {name} | {files} | {res['b']['routes']} "
            f"| {res['f']['api_calls']} | {res['m']['matched']} "
            f"| {len(rows)} | {r['seconds']} |")
        results_md.append(
            "|  | " + ", ".join(f"{c}: {n}" for c, n in
                                sorted(by_code.items())) + " | | | | | |")

        lines = [f"# {name} — flag samples for labeling", "",
                 "Verdict legend: TP (real issue), FP (wrong), "
                 "DEBATABLE (defensible but low-value).", ""]
        for code in sorted(by_code):
            pool = [row for row in rows if row["code"] == code]
            picks = rng.sample(pool, min(args.sample, len(pool)))
            lines.append(f"## {code} ({by_code[code]} total, "
                         f"{len(picks)} sampled)")
            for row in picks:
                lines += [f"- [ ] `{row['owner']}`",
                          f"  - evidence: {row['evidence']}",
                          f"  - message: {row['message']}",
                          "  - verdict: "]
            lines.append("")
        with open(os.path.join(samples_dir, f"{name}.md"), "w",
                  encoding="utf-8") as f:
            f.write("\n".join(lines))

    results_md += ["", "## Flag totals across corpus", ""]
    results_md += [f"- {code}: {n}" for code, n in totals.most_common()]
    with open(os.path.join(BENCH_DIR, "RESULTS.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(results_md) + "\n")
    print(f"wrote bench/RESULTS.md and bench/samples/*.md")
    return 0


if __name__ == "__main__":
    sys.exit(main())
