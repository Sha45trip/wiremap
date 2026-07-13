"""Graph diffing (ROADMAP 3.2) — `wiremap diff <old.json> <new.json>`.

Semantics per the roadmap: nodes and edges match by id; a flag is
"introduced" when its (owner_id, code) pair newly appears, "resolved" when
it disappears. Total risk is the sum of node risk scores (edge scores
derive from their endpoints, so counting them would double-count).

Exit-code policy: `--fail-on <severity>` returns 1 when any introduced
flag is at or above that severity — usable as a CI merge gate.
"""
from __future__ import annotations

import json
import sys

SEVERITY_ORDER = {"low": 0, "medium": 1, "high": 2, "critical": 3}


def _flag_map(graph: dict) -> dict:
    """(owner_id, code) -> flag dict, across nodes and edges."""
    out = {}
    for elem in graph.get("nodes", []) + graph.get("edges", []):
        for f in elem.get("risk_flags", []):
            out.setdefault((elem["id"], f["code"]), f)
    return out


def diff_graphs(old: dict, new: dict) -> dict:
    old_nodes = {n["id"]: n for n in old.get("nodes", [])}
    new_nodes = {n["id"]: n for n in new.get("nodes", [])}
    old_edges = {e["id"]: e for e in old.get("edges", [])}
    new_edges = {e["id"]: e for e in new.get("edges", [])}

    edges_changed = []
    for eid in sorted(set(old_edges) & set(new_edges)):
        o, n = old_edges[eid], new_edges[eid]
        if o.get("confidence") != n.get("confidence") \
                or o.get("risk_score") != n.get("risk_score"):
            edges_changed.append({"id": eid,
                                  "old_risk": o.get("risk_score"),
                                  "new_risk": n.get("risk_score"),
                                  "old_confidence": o.get("confidence"),
                                  "new_confidence": n.get("confidence")})

    old_flags = _flag_map(old)
    new_flags = _flag_map(new)

    def flag_rows(keys, source):
        rows = [{"owner": owner, "code": code,
                 "severity": source[(owner, code)]["severity"],
                 "category": source[(owner, code)]["category"],
                 "message": source[(owner, code)]["message"]}
                for owner, code in keys]
        return sorted(rows, key=lambda r: (-SEVERITY_ORDER.get(r["severity"], 0),
                                           r["owner"], r["code"]))

    old_total = round(sum(n.get("risk_score", 0) for n in old_nodes.values()), 1)
    new_total = round(sum(n.get("risk_score", 0) for n in new_nodes.values()), 1)

    return {
        "nodes": {"added": sorted(set(new_nodes) - set(old_nodes)),
                  "removed": sorted(set(old_nodes) - set(new_nodes)),
                  "old_count": len(old_nodes), "new_count": len(new_nodes)},
        "wires": {"added": sorted(set(new_edges) - set(old_edges)),
                  "removed": sorted(set(old_edges) - set(new_edges)),
                  "changed": edges_changed,
                  "old_count": len(old_edges), "new_count": len(new_edges)},
        "flags": {"introduced": flag_rows(set(new_flags) - set(old_flags),
                                          new_flags),
                  "resolved": flag_rows(set(old_flags) - set(new_flags),
                                        old_flags)},
        "risk": {"old_total": old_total, "new_total": new_total,
                 "delta": round(new_total - old_total, 1)},
    }


def gate_failed(diff: dict, threshold: str) -> bool:
    limit = SEVERITY_ORDER[threshold]
    return any(SEVERITY_ORDER.get(f["severity"], 0) >= limit
               for f in diff["flags"]["introduced"])


def _sign(x: float) -> str:
    return f"+{x:g}" if x > 0 else f"{x:g}"


def format_text(d: dict) -> str:
    lines = [
        "wiremap diff",
        f"  nodes   {d['nodes']['old_count']} -> {d['nodes']['new_count']}"
        f"  (+{len(d['nodes']['added'])} / -{len(d['nodes']['removed'])})",
        f"  wires   {d['wires']['old_count']} -> {d['wires']['new_count']}"
        f"  (+{len(d['wires']['added'])} / -{len(d['wires']['removed'])}"
        f" / ~{len(d['wires']['changed'])})",
        f"  flags   +{len(d['flags']['introduced'])} introduced,"
        f" {len(d['flags']['resolved'])} resolved",
        f"  risk    {d['risk']['old_total']} -> {d['risk']['new_total']}"
        f"  ({_sign(d['risk']['delta'])})",
    ]
    if d["flags"]["introduced"]:
        lines.append("\nintroduced flags:")
        for f in d["flags"]["introduced"]:
            lines.append(f"  [{f['severity']}] {f['code']}  {f['owner']}"
                         f" — {f['message'][:90]}")
    if d["flags"]["resolved"]:
        lines.append("\nresolved flags:")
        for f in d["flags"]["resolved"]:
            lines.append(f"  [{f['severity']}] {f['code']}  {f['owner']}")
    for kind in ("added", "removed"):
        if d["wires"][kind]:
            lines.append(f"\nwires {kind}:")
            lines.extend(f"  {wid}" for wid in d["wires"][kind])
    return "\n".join(lines)


def format_markdown(d: dict) -> str:
    r = d["risk"]
    out = [
        "## wiremap diff",
        "",
        "| | old | new | Δ |",
        "|---|---|---|---|",
        f"| nodes | {d['nodes']['old_count']} | {d['nodes']['new_count']} "
        f"| {_sign(len(d['nodes']['added']) - len(d['nodes']['removed']))} |",
        f"| wires | {d['wires']['old_count']} | {d['wires']['new_count']} "
        f"| {_sign(len(d['wires']['added']) - len(d['wires']['removed']))} |",
        f"| total risk | {r['old_total']} | {r['new_total']} "
        f"| **{_sign(r['delta'])}** |",
        "",
    ]
    if d["flags"]["introduced"]:
        out += [f"**Introduced flags ({len(d['flags']['introduced'])})**", "",
                "| severity | flag | where | detail |", "|---|---|---|---|"]
        out += [f"| {f['severity']} | `{f['code']}` | `{f['owner']}` "
                f"| {f['message'][:100]} |" for f in d["flags"]["introduced"]]
        out.append("")
    if d["flags"]["resolved"]:
        out += [f"**Resolved flags ({len(d['flags']['resolved'])})**", "",
                "| severity | flag | where |", "|---|---|---|"]
        out += [f"| {f['severity']} | `{f['code']}` | `{f['owner']}` |"
                for f in d["flags"]["resolved"]]
        out.append("")
    wire_bits = []
    for kind in ("added", "removed"):
        wire_bits += [f"{kind}: `{wid}`" for wid in d["wires"][kind]]
    if d["wires"]["changed"]:
        wire_bits += [f"changed: `{c['id']}` risk {c['old_risk']} → "
                      f"{c['new_risk']}" for c in d["wires"]["changed"]]
    if wire_bits:
        out += ["<details><summary>wire changes "
                f"(+{len(d['wires']['added'])} / -{len(d['wires']['removed'])}"
                f" / ~{len(d['wires']['changed'])})</summary>", ""]
        out += [f"- {b}" for b in wire_bits]
        out += ["", "</details>", ""]
    if not (d["flags"]["introduced"] or d["flags"]["resolved"] or wire_bits):
        out.append("No wire or flag changes.")
    return "\n".join(out)


def run_diff(old_path: str, new_path: str, fmt: str = "text",
             fail_on: str | None = None) -> int:
    """CLI entry: load, print in the chosen format, apply the gate."""
    graphs = []
    for path in (old_path, new_path):
        try:
            with open(path, encoding="utf-8") as f:
                g = json.load(f)
        except (OSError, json.JSONDecodeError) as e:
            print(f"error: cannot read graph {path}: {e}", file=sys.stderr)
            return 2
        if not isinstance(g, dict) or "nodes" not in g:
            print(f"error: {path} is not a wiremap graph.json", file=sys.stderr)
            return 2
        graphs.append(g)

    d = diff_graphs(*graphs)
    if fmt == "json":
        print(json.dumps(d, indent=2))
    elif fmt == "md":
        print(format_markdown(d))
    else:
        print(format_text(d))

    if fail_on and gate_failed(d, fail_on):
        n = sum(1 for f in d["flags"]["introduced"]
                if SEVERITY_ORDER.get(f["severity"], 0)
                >= SEVERITY_ORDER[fail_on])
        print(f"\ngate: {n} introduced flag(s) at severity >= {fail_on}",
              file=sys.stderr)
        return 1
    return 0
