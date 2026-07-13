"""Team-mode server (ROADMAP 3.1) — `wiremap serve`.

One process serving, on a single port:
  GET  /wiremap.html, /graph.json   read-only viewer + graph (no auth)
  GET  /healthz                     liveness + last-scan info (no auth)
  POST /v1/traces                   OTLP/JSON span ingestion   (token-guarded)
  POST /rescan                      webhook-triggered re-scan  (token-guarded)

plus an optional interval-based re-scan loop (`--rescan-interval`).

Auth model per the roadmap: v1 is for trusted networks — read routes are
open. If the WIREMAP_TOKEN env var is set, the mutating routes require
`Authorization: Bearer <token>` (OTel exporters send it via
OTEL_EXPORTER_OTLP_HEADERS). Designed for Docker: mount the repo read-only
at /repo, write outputs to a volume via --out.
"""
from __future__ import annotations

import json
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .collector import RuntimeStore, ingest_http_body


class TeamServer(ThreadingHTTPServer):
    def __init__(self, addr, root: str, out_dir: str,
                 backend: str | None = None, frontend: str | None = None,
                 token: str | None = None, tokens: dict | None = None,
                 rescan_interval: float = 0):
        super().__init__(addr, _TeamHandler)
        self.root = os.path.abspath(root)
        self.out_dir = os.path.abspath(out_dir)
        self.backend, self.frontend = backend, frontend
        # per-user tokens: {token: label}. A single `token=` is accepted for
        # back-compat and stored under the label "default".
        self.tokens: dict[str, str] = dict(tokens or {})
        if token:
            self.tokens.setdefault(token, "default")
        self.rescan_interval = rescan_interval
        self.store = RuntimeStore(os.path.join(self.out_dir, "runtime.json"))
        self.scan_lock = threading.Lock()
        self.last_scan: float | None = None
        self.spans_seen = 0
        self._stop = threading.Event()

    def rescan(self) -> dict:
        from .cli import perform_scan   # deferred: cli imports this module
        with self.scan_lock:
            res = perform_scan(self.root, backend=self.backend,
                               frontend=self.frontend, out=self.out_dir)
            self.last_scan = time.time()
            return res

    def start_rescan_loop(self) -> None:
        if self.rescan_interval <= 0:
            return

        def loop():
            while not self._stop.wait(self.rescan_interval):
                try:
                    res = self.rescan()
                    print(f"  re-scan: {res['b']['routes']} routes, "
                          f"{res['r']['total_flags']} flags")
                except Exception as e:
                    print(f"  re-scan failed: {e}")

        threading.Thread(target=loop, daemon=True).start()

    def shutdown(self):
        self._stop.set()
        super().shutdown()


class _TeamHandler(BaseHTTPRequestHandler):
    def _reply(self, code: int, body: dict):
        data = json.dumps(body).encode()
        self.send_response(code)
        if code == 401:
            self.send_header("WWW-Authenticate", "Bearer")
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _file(self, name: str, ctype: str):
        path = os.path.join(self.server.out_dir, name)
        if not os.path.exists(path):
            self._reply(404, {"error": f"{name} not generated yet — "
                                       "POST /rescan or wait for the loop"})
            return
        with open(path, "rb") as f:
            data = f.read()
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _authorized(self) -> bool:
        toks = self.server.tokens
        if not toks:
            return True                       # open (trusted networks)
        auth = self.headers.get("Authorization", "")
        presented = auth[7:] if auth.startswith("Bearer ") else ""
        self._user = toks.get(presented)      # label, for logging
        return presented in toks

    def do_GET(self):
        p = self.path.split("?")[0]
        if p in ("/", "/index.html"):
            self.send_response(302)
            self.send_header("Location", "/wiremap.html")
            self.send_header("Content-Length", "0")
            self.end_headers()
        elif p == "/wiremap.html":
            self._file("wiremap.html", "text/html; charset=utf-8")
        elif p == "/graph.json":
            self._file("graph.json", "application/json")
        elif p == "/healthz":
            self._reply(200, {"status": "ok", "last_scan": self.server.last_scan,
                              "spans_seen": self.server.spans_seen})
        else:
            self._reply(404, {"error": "unknown path"})

    def do_POST(self):
        p = self.path.split("?")[0]
        if p not in ("/v1/traces", "/rescan"):
            self._reply(404, {"error": "unknown path"})
            return
        if not self._authorized():
            self._reply(401, {"error": "missing or invalid bearer token "
                                       "(server has WIREMAP_TOKEN set)"})
            return
        if p == "/v1/traces":
            length = int(self.headers.get("Content-Length", 0))
            status, body, ctype, n = ingest_http_body(
                self.server.store, self.rfile.read(length),
                self.headers.get("Content-Type", ""))
            self.server.spans_seen += n
            data = body
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        else:
            try:
                res = self.server.rescan()
            except Exception as e:
                self._reply(500, {"error": f"re-scan failed: {e}"})
                return
            self._reply(200, {
                "rescanned": True,
                "routes": res["b"]["routes"],
                "api_calls": res["f"]["api_calls"],
                "wires": res["m"]["matched"],
                "risk_flags": res["r"]["total_flags"],
                "critical": res["r"]["critical_flags"],
            })

    def log_message(self, fmt, *args):
        pass


def parse_tokens(tokens_env: str | None,
                 token_env: str | None = None) -> dict[str, str]:
    """WIREMAP_TOKENS='alice:tok1,bob:tok2' or bare 'tok1,tok2' -> {token:
    label}. A bare token gets a generated label. Single WIREMAP_TOKEN is
    folded in for back-compat."""
    out: dict[str, str] = {}
    for i, part in enumerate((tokens_env or "").split(",")):
        part = part.strip()
        if not part:
            continue
        if ":" in part:
            label, tok = part.split(":", 1)
            out[tok.strip()] = label.strip() or f"user{i + 1}"
        else:
            out[part] = f"user{i + 1}"
    if token_env:
        out.setdefault(token_env, "default")
    return out


def run_server(root: str, port: int, out: str | None = None,
               backend: str | None = None, frontend: str | None = None,
               rescan_interval: float = 0) -> int:
    out_dir = os.path.abspath(out or os.path.join(os.path.abspath(root),
                                                  ".wiremap"))
    tokens = parse_tokens(os.environ.get("WIREMAP_TOKENS"),
                          os.environ.get("WIREMAP_TOKEN"))
    server = TeamServer(("", port), root=root, out_dir=out_dir,
                        backend=backend, frontend=frontend, tokens=tokens,
                        rescan_interval=rescan_interval)
    res = server.rescan()   # initial scan so the viewer exists immediately
    server.start_rescan_loop()
    auth_line = (f"{len(tokens)} bearer token(s) on mutating routes "
                 f"({', '.join(sorted(set(tokens.values())))})" if tokens
                 else "OPEN (trusted networks only — set WIREMAP_TOKENS)")
    print(f"wiremap · team server\n"
          f"  viewer    : http://localhost:{port}/wiremap.html\n"
          f"  traces    : POST http://localhost:{port}/v1/traces (OTLP/JSON)\n"
          f"  re-scan   : POST http://localhost:{port}/rescan"
          + (f" · every {rescan_interval:g}s" if rescan_interval > 0 else "")
          + f"\n  outputs   : {out_dir}\n"
          f"  auth      : {auth_line}\n"
          f"  initial scan: {res['b']['routes']} routes, "
          f"{res['r']['total_flags']} flags\n\n  Ctrl-C to stop")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.store.save()
    return 0
