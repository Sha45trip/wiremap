"""wiremap CLI.

    wiremap scan <project_root> [--backend DIR] [--frontend DIR]
                 [--out DIR] [--no-cache] [--coverage FILE] [--serve]
    wiremap collect [project_root] [--port 4318] [--window 24]
    wiremap serve [project_root] [--port 8787] [--rescan-interval SECS]
    wiremap diff <old.json> <new.json> [--format text|md|json] [--fail-on SEV]
"""
from __future__ import annotations

import argparse
import http.server
import json
import os
import sys
import webbrowser

from .cache import FileCache
from .collector import merge_runtime, run_collector
from .coverage import apply_coverage, load_coverage
from .diff import SEVERITY_ORDER, run_diff
from .graph import Graph
from .extractors.express_backend import extract_express
from .extractors.nextjs_backend import extract_nextjs
from .gql import ingest_sdl
from .history import load_history, record_snapshot
from .extractors.python_backend import extract_backend
from .extractors.react_frontend import extract_frontend
from .matcher import match
from .openapi import ingest_endpoints, load_openapi, operation_map
from .risk import load_config, score
from .server import run_server


def _guess_dirs(root: str) -> tuple[str, str]:
    """Find backend/frontend dirs by convention if not specified."""
    backend = frontend = root
    for cand in ("backend", "server", "api", "app"):
        p = os.path.join(root, cand)
        if os.path.isdir(p):
            backend = p
            break
    for cand in ("frontend", "client", "web", "ui"):
        p = os.path.join(root, cand)
        if os.path.isdir(p):
            frontend = p
            break
    return backend, frontend


def perform_scan(project_root: str, backend: str | None = None,
                 frontend: str | None = None, out: str | None = None,
                 no_cache: bool = False, coverage: str | None = None) -> dict:
    """Run the full pipeline and write graph.json + wiremap.html.

    Silent (no printing) so the team server can re-scan in-process;
    `scan()` prints the summary. Raises on unreadable coverage reports.
    """
    root = os.path.abspath(project_root)
    if not os.path.isdir(root):
        # a silent empty graph here poisons CI diffs downstream
        raise FileNotFoundError(f"project root does not exist: {root}")
    b_dir = os.path.abspath(backend) if backend else None
    f_dir = os.path.abspath(frontend) if frontend else None
    if b_dir is None or f_dir is None:
        gb, gf = _guess_dirs(root)
        b_dir = b_dir or gb
        f_dir = f_dir or gf

    out_dir = os.path.abspath(out or os.path.join(root, ".wiremap"))
    os.makedirs(out_dir, exist_ok=True)

    cache = None if no_cache else FileCache(os.path.join(out_dir, "cache.json"))

    graph = Graph()
    b_stats = extract_backend(b_dir, graph, cache)
    e_stats = extract_express(b_dir, graph, cache)
    b_stats["routes"] += e_stats["routes"]
    b_stats["files_parsed"] += e_stats["files_parsed"]
    b_stats["files_cached"] += e_stats["files_cached"]

    g_stats = ingest_sdl(b_dir, graph)   # fills gaps; resolver-found wins

    # Next.js/tRPC routes live in the frontend tree (and sometimes backend);
    # scan both, de-dupe by node id
    nx_stats = extract_nextjs(f_dir, graph, cache)
    if os.path.abspath(b_dir) != os.path.abspath(f_dir):
        nx_b = extract_nextjs(b_dir, graph, cache)
        for k in ("routes", "files_parsed", "files_cached"):
            nx_stats[k] += nx_b[k]
    b_stats["routes"] += nx_stats["routes"]
    b_stats["files_parsed"] += nx_stats["files_parsed"]
    b_stats["files_cached"] += nx_stats["files_cached"]

    oa_stats = client_ops = None
    found = load_openapi(root, b_dir)
    if found:
        spec, spec_rel = found
        oa_stats = ingest_endpoints(spec, graph, spec_rel)
        client_ops = operation_map(spec)

    f_stats = extract_frontend(f_dir, graph, cache, client_ops=client_ops)
    m_stats = match(graph)

    cov_stats = None
    if coverage:
        cov_stats = apply_coverage(graph, load_coverage(coverage))

    config = load_config(root)
    rt_stats = merge_runtime(graph, os.path.join(out_dir, "runtime.json"), config)
    r_stats = score(graph, config)

    if cache is not None:
        cache.save()

    # scan history: append a snapshot (skipped if the graph is unchanged)
    snap = record_snapshot(out_dir, graph)
    history = load_history(out_dir)

    graph_path = os.path.join(out_dir, "graph.json")
    graph.save(graph_path)

    template_path = os.path.join(os.path.dirname(__file__), "viewer_template.html")
    with open(template_path) as f:
        html = f.read()
    html = html.replace("__GRAPH_JSON__",
                        json.dumps(graph.to_dict()).replace("</", "<\\/"))
    html = html.replace("__HISTORY_JSON__",
                        json.dumps(history).replace("</", "<\\/"))
    viewer_path = os.path.join(out_dir, "wiremap.html")
    with open(viewer_path, "w") as f:
        f.write(html)

    return {"backend": b_dir, "frontend": f_dir, "out_dir": out_dir,
            "graph_path": graph_path, "viewer_path": viewer_path,
            "b": b_stats, "f": f_stats, "m": m_stats, "oa": oa_stats,
            "nx": nx_stats, "gql": g_stats if g_stats["root_fields"] else None,
            "cov": cov_stats, "rt": rt_stats, "r": r_stats,
            "history": history, "snapshot": snap}


def scan(args) -> int:
    try:
        res = perform_scan(args.project_root, backend=args.backend,
                           frontend=args.frontend, out=args.out,
                           no_cache=args.no_cache, coverage=args.coverage)
    except (OSError, ValueError, json.JSONDecodeError) as e:
        if args.coverage:
            print(f"error: cannot read coverage report: {e}", file=sys.stderr)
            return 2
        raise

    b_stats, f_stats, m_stats = res["b"], res["f"], res["m"]
    cov_stats, rt_stats, r_stats = res["cov"], res["rt"], res["r"]
    parsed = b_stats["files_parsed"] + f_stats["files_parsed"]
    from_cache = b_stats["files_cached"] + f_stats["files_cached"]
    cache_note = "cache disabled" if args.no_cache else \
        f"{from_cache} unchanged, from cache"

    print(f"""wiremap · scanning
  backend : {res['backend']}
  frontend: {res['frontend']}

  files parsed      {parsed}  ({cache_note})
  routes found      {b_stats['routes']}{f'''
  next/trpc routes  {res['nx']['routes']}''' if res['nx'] and res['nx']['routes'] else ''}{f'''
  graphql schema    {res['gql']['root_fields']} root fields (SDL)''' if res['gql'] else ''}{f'''
  openapi ingested  {res['oa']['endpoints']} spec endpoints''' if res['oa'] else ''}
  api call sites    {f_stats['api_calls']}
  wires matched     {m_stats['matched']}
  orphan calls      {m_stats['orphan_calls']}   <- frontend calls with no backend route
  unused endpoints  {m_stats['unused_endpoints']}{f'''
  coverage mapped   {cov_stats['nodes_with_coverage']} nodes  ({cov_stats['untested_handlers']} under-tested handlers)''' if cov_stats else ''}{f'''
  runtime overlay   {rt_stats['endpoints_with_traffic']} endpoints with traffic  ({rt_stats['runtime_flags']} runtime flags)''' if rt_stats else ''}
  risk flags        {r_stats['total_flags']}  ({r_stats['critical_flags']} critical)

  graph  -> {res['graph_path']}
  viewer -> {res['viewer_path']}""")

    if args.serve:
        os.chdir(res["out_dir"])
        port = args.port
        print(f"\n  serving http://localhost:{port}/wiremap.html  (Ctrl-C to stop)")
        try:
            webbrowser.open(f"http://localhost:{port}/wiremap.html")
        except Exception:
            pass
        http.server.HTTPServer(
            ("", port), http.server.SimpleHTTPRequestHandler).serve_forever()
    return 0


def explain(args) -> int:
    """Print exactly how a node's or edge's risk score is computed."""
    root = os.path.abspath(args.project_root)
    graph_path = os.path.join(args.out or os.path.join(root, ".wiremap"),
                              "graph.json")
    try:
        with open(graph_path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        print(f"error: cannot read {graph_path}: {e} — run `wiremap scan` first",
              file=sys.stderr)
        return 2
    elems = {e["id"]: e for e in data["nodes"] + data["edges"]}
    elem = elems.get(args.node_id)
    if elem is None:
        hits = [i for i in elems if args.node_id.lower() in i.lower()]
        print(f"error: no element `{args.node_id}`"
              + (f"; close matches: {', '.join(hits[:5])}" if hits else ""),
              file=sys.stderr)
        return 2

    config = load_config(root)
    weights, sev = config["weights"], config["severity"]
    print(f"{elem['id']}\n  label: {elem.get('label', '')}\n"
          f"  score: {elem['risk_score']} / 100\n")
    total = 0.0
    for f in elem["risk_flags"]:
        pts = sev.get(f["severity"], 1) * weights.get(f["category"], 1.0)
        total += pts
        print(f"  {pts:>5.1f}  = severity {f['severity']} ({sev.get(f['severity'], 1)})"
              f" x weight {f['category']} ({weights.get(f['category'], 1.0)})"
              f"   [{f['code']}]")
    if elem["risk_flags"]:
        print(f"\n  raw total {total:g}; normalized = min(total/20*100, 100)"
              f" = {min(round(total / 20 * 100, 1), 100)}")
    else:
        print("  no flags on this element")
    if elem["id"] in {e["id"] for e in data["edges"]}:
        ends = [elems.get(elem["source"]), elems.get(elem["target"])]
        end_risk = max((n["risk_score"] for n in ends if n), default=0)
        print(f"  edge rule: max(own {min(round(total / 20 * 100, 1), 100)},"
              f" 0.8 x max endpoint risk {end_risk}) = {elem['risk_score']}")
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(prog="wiremap",
                                description="Full-stack wire mapping and risk flags")
    sub = p.add_subparsers(dest="cmd", required=True)
    ps = sub.add_parser("scan", help="scan a project and build the wire map")
    ps.add_argument("project_root")
    ps.add_argument("--backend", help="backend source dir (default: auto-detect)")
    ps.add_argument("--frontend", help="frontend source dir (default: auto-detect)")
    ps.add_argument("--out", help="output dir (default: <root>/.wiremap)")
    ps.add_argument("--no-cache", action="store_true",
                    help="force full re-parse, ignoring .wiremap/cache.json")
    ps.add_argument("--coverage",
                    help="coverage.py JSON report (`coverage json`) to map "
                         "onto handlers/functions")
    ps.add_argument("--serve", action="store_true", help="serve the viewer locally")
    ps.add_argument("--port", type=int, default=8787)
    ps.set_defaults(func=scan)

    pc = sub.add_parser("collect",
                        help="run the OTLP/JSON trace receiver "
                             "(writes .wiremap/runtime.json)")
    pc.add_argument("project_root", nargs="?", default=".")
    pc.add_argument("--port", type=int, default=4318)
    pc.add_argument("--window", type=float, default=24.0,
                    help="rolling window in hours (default 24)")
    pc.set_defaults(func=lambda a: run_collector(a.project_root, a.port, a.window))

    pv = sub.add_parser("serve",
                        help="team-mode server: viewer + OTLP collector + "
                             "webhook/scheduled re-scans (WIREMAP_TOKEN "
                             "guards mutating routes)")
    pv.add_argument("project_root", nargs="?", default=".")
    pv.add_argument("--backend", help="backend source dir (default: auto-detect)")
    pv.add_argument("--frontend", help="frontend source dir (default: auto-detect)")
    pv.add_argument("--out", help="output dir (default: <root>/.wiremap); "
                                  "use a writable volume when the repo mount "
                                  "is read-only")
    pv.add_argument("--port", type=int, default=8787)
    pv.add_argument("--rescan-interval", type=float, default=0,
                    help="seconds between automatic re-scans (0 = webhook only)")
    pv.set_defaults(func=lambda a: run_server(
        a.project_root, a.port, out=a.out, backend=a.backend,
        frontend=a.frontend, rescan_interval=a.rescan_interval))

    pdf = sub.add_parser("diff",
                         help="compare two graph.json files: wires "
                              "added/removed/changed, flags "
                              "introduced/resolved, risk delta")
    pdf.add_argument("old_graph")
    pdf.add_argument("new_graph")
    pdf.add_argument("--format", choices=("text", "md", "json"),
                     default="text",
                     help="md emits a PR-comment body")
    pdf.add_argument("--fail-on", choices=tuple(SEVERITY_ORDER),
                     help="exit 1 when an introduced flag is at or above "
                          "this severity (merge gate)")
    pdf.set_defaults(func=lambda a: run_diff(a.old_graph, a.new_graph,
                                             a.format, a.fail_on))

    pe = sub.add_parser("explain",
                        help="show exactly how an element's risk score "
                             "is computed from its flags")
    pe.add_argument("project_root")
    pe.add_argument("node_id", help="node or edge id from graph.json")
    pe.add_argument("--out", help="output dir (default: <root>/.wiremap)")
    pe.set_defaults(func=explain)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
