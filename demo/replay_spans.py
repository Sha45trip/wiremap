"""Replay canned OTLP/JSON spans at a running wiremap collector.

ROADMAP 2.3 acceptance script. Traffic story it plants:
- POST /api/orders        60 req, 2x 5xx, p95 > 1000ms  -> hot_fragile (it
                          already carries critical static flags), plus
                          high_latency and high_error_rate
- GET /api/users/{id}     40 fast clean requests         -> healthy runtime
- GET /api/orders/{id}    25 req sent WITHOUT http.route -> matched by
                          url.path fallback, confidence "probable"
- GET /api/reports/summary  no traffic                   -> confirmed_dead
- one CLIENT span and one unknown-route span, which must be ignored

Usage:
    wiremap collect demo              # terminal 1
    python demo/replay_spans.py       # terminal 2 (optional: port arg)
    wiremap scan demo                 # runtime overlay appears
"""
import json
import sys
import time
import urllib.request


def _span(method, dur_ms, status=200, route=None, path=None,
          offset_min=0, kind=2, legacy=False, now_ns=None):
    start = int(now_ns - offset_min * 60 * 1_000_000_000)
    end = int(start + dur_ms * 1_000_000)
    if legacy:
        attrs = [{"key": "http.method", "value": {"stringValue": method}},
                 {"key": "http.status_code", "value": {"intValue": str(status)}}]
        if route:
            attrs.append({"key": "http.route", "value": {"stringValue": route}})
        if path:
            attrs.append({"key": "http.target", "value": {"stringValue": path}})
    else:
        attrs = [{"key": "http.request.method", "value": {"stringValue": method}},
                 {"key": "http.response.status_code",
                  "value": {"intValue": str(status)}}]
        if route:
            attrs.append({"key": "http.route", "value": {"stringValue": route}})
        if path:
            attrs.append({"key": "url.path", "value": {"stringValue": path}})
    return {"name": f"{method} {route or path}", "kind": kind,
            "startTimeUnixNano": str(start), "endTimeUnixNano": str(end),
            "attributes": attrs}


def _payload(spans):
    return {"resourceSpans": [{
        "resource": {"attributes": [
            {"key": "service.name", "value": {"stringValue": "demo-backend"}}]},
        "scopeSpans": [{"scope": {"name": "demo-replay"}, "spans": spans}],
    }]}


def build_batches(now_ns=None):
    """Three OTLP payloads; importable by tests."""
    now_ns = now_ns or time.time_ns()
    orders = []
    for i in range(56):
        orders.append(_span("POST", dur_ms=200 + (i % 8) * 90,
                            status=500 if i in (7, 23) else 201,
                            route="/api/orders", offset_min=i, now_ns=now_ns))
    for i, slow in enumerate((1200, 1300, 1400, 1600)):
        orders.append(_span("POST", dur_ms=slow, status=201,
                            route="/api/orders", offset_min=57 + i, now_ns=now_ns))

    users = [_span("GET", dur_ms=30 + (i % 6) * 10, status=200,
                   route="/api/users/{user_id}", path=f"/api/users/{i}",
                   offset_min=i, legacy=(i % 2 == 0), now_ns=now_ns)
             for i in range(40)]

    order_detail = [_span("GET", dur_ms=80 + (i % 5) * 25, status=200,
                          path=f"/api/orders/{1000 + i}",
                          offset_min=i, now_ns=now_ns)
                    for i in range(25)]

    noise = [
        _span("GET", dur_ms=45, status=200, route="/api/nope",
              offset_min=1, now_ns=now_ns),
        _span("GET", dur_ms=310, status=200, path="/external/api",
              offset_min=2, kind=3, now_ns=now_ns),
    ]
    return [_payload(orders), _payload(users), _payload(order_detail + noise)]


def main() -> int:
    args = [a for a in sys.argv[1:] if a != "--protobuf"]
    protobuf = "--protobuf" in sys.argv
    port = int(args[0]) if args else 4318
    url = f"http://localhost:{port}/v1/traces"
    total = 0
    for batch in build_batches():
        n = sum(len(ss["spans"]) for rs in batch["resourceSpans"]
                for ss in rs["scopeSpans"])
        if protobuf:
            from google.protobuf.json_format import ParseDict
            from opentelemetry.proto.collector.trace.v1.trace_service_pb2 \
                import ExportTraceServiceRequest
            data = ParseDict(batch,
                             ExportTraceServiceRequest()).SerializeToString()
            ctype = "application/x-protobuf"
        else:
            data, ctype = json.dumps(batch).encode(), "application/json"
        req = urllib.request.Request(
            url, data=data, headers={"Content-Type": ctype}, method="POST")
        with urllib.request.urlopen(req) as resp:
            assert resp.status == 200, resp.status
        total += n
        print(f"  sent {n} spans ({ctype.split('/')[-1]}) -> {url}")
    print(f"replayed {total} spans; now run: wiremap scan demo")
    return 0


if __name__ == "__main__":
    sys.exit(main())
