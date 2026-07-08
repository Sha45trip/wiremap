"""wiremap CLI.

    wiremap scan <project_root> [--backend DIR] [--frontend DIR]
                 [--out DIR] [--no-cache] [--coverage FILE] [--serve]
"""
from __future__ import annotations

import argparse
import http.server
import json
import os
import sys
import webbrowser

from .cache import FileCache
from .coverage import apply_coverage, load_coverage
from .graph import Graph
from .extractors.python_backend import extract_backend
from .extractors.react_frontend import extract_frontend
from .matcher import match
from .risk import load_config, score


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


def scan(args) -> int:
    root = os.path.abspath(args.project_root)
    backend = os.path.abspath(args.backend) if args.backend else None
    frontend = os.path.abspath(args.frontend) if args.frontend else None
    if backend is None or frontend is None:
        gb, gf = _guess_dirs(root)
        backend = backend or gb
        frontend = frontend or gf

    print(f"wiremap · scanning\n  backend : {backend}\n  frontend: {frontend}")

    out_dir = os.path.abspath(args.out or os.path.join(root, ".wiremap"))
    os.makedirs(out_dir, exist_ok=True)

    cache = None
    if not args.no_cache:
        cache = FileCache(os.path.join(out_dir, "cache.json"))

    graph = Graph()
    b_stats = extract_backend(backend, graph, cache)
    f_stats = extract_frontend(frontend, graph, cache)
    m_stats = match(graph)

    cov_stats = None
    if args.coverage:
        try:
            cov = load_coverage(args.coverage)
        except (OSError, ValueError, json.JSONDecodeError) as e:
            print(f"error: cannot read coverage report: {e}", file=sys.stderr)
            return 2
        cov_stats = apply_coverage(graph, cov)

    config = load_config(root)
    r_stats = score(graph, config)

    if cache is not None:
        cache.save()

    parsed = b_stats["files_parsed"] + f_stats["files_parsed"]
    from_cache = b_stats["files_cached"] + f_stats["files_cached"]
    cache_note = "cache disabled" if args.no_cache else \
        f"{from_cache} unchanged, from cache"

    graph_path = os.path.join(out_dir, "graph.json")
    graph.save(graph_path)

    template_path = os.path.join(os.path.dirname(__file__), "viewer_template.html")
    with open(template_path) as f:
        html = f.read()
    html = html.replace("__GRAPH_JSON__",
                        json.dumps(graph.to_dict()).replace("</", "<\\/"))
    viewer_path = os.path.join(out_dir, "wiremap.html")
    with open(viewer_path, "w") as f:
        f.write(html)

    print(f"""
  files parsed      {parsed}  ({cache_note})
  routes found      {b_stats['routes']}
  api call sites    {f_stats['api_calls']}
  wires matched     {m_stats['matched']}
  orphan calls      {m_stats['orphan_calls']}   <- frontend calls with no backend route
  unused endpoints  {m_stats['unused_endpoints']}{f'''
  coverage mapped   {cov_stats['nodes_with_coverage']} nodes  ({cov_stats['untested_handlers']} under-tested handlers)''' if cov_stats else ''}
  risk flags        {r_stats['total_flags']}  ({r_stats['critical_flags']} critical)

  graph  -> {graph_path}
  viewer -> {viewer_path}""")

    if args.serve:
        os.chdir(out_dir)
        port = args.port
        print(f"\n  serving http://localhost:{port}/wiremap.html  (Ctrl-C to stop)")
        try:
            webbrowser.open(f"http://localhost:{port}/wiremap.html")
        except Exception:
            pass
        http.server.HTTPServer(
            ("", port), http.server.SimpleHTTPRequestHandler).serve_forever()
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
    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
