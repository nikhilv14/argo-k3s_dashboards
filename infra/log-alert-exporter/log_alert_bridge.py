#!/usr/bin/env python3
"""Log-alert bridge: evaluate LogQL against Loki and expose each result as a gauge.

Why this exists: the Loki ruler cannot run on this cluster. Loki is single-binary
on a filesystem (RWO) volume and the loki helm chart refuses ruler replicas
alongside single-binary, and single-binary exposes no query-frontend gRPC endpoint
for remote rule evaluation. So this bridge evaluates a git-managed list of LogQL
queries on an interval and publishes each result as `log_alert_value{rule="..."}`.

Alert semantics stay where every other alert lives: the threshold, `for` duration,
severity and routing are defined in PrometheusRule files, so a log alert is
evaluated, grouped and delivered by the same Prometheus -> Alertmanager -> Discord
pipeline as everything else.

Contracts:
  - a query returning no series publishes 0, so `log_alert_value >= 1` stays quiet
    when there is nothing to report;
  - a failed query publishes 0 and bumps log_alert_eval_errors_total rather than
    firing a false alert;
  - queries must be metric queries (wrapped in count_over_time/rate/sum) - a plain
    log query returns no number.

Rules file (JSON, mounted read-only):
  {"rules": [{"name": "...", "expr": "..."}, ...]}
"""
import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer

LOKI_URL = os.environ.get("LOKI_URL", "http://loki.monitoring.svc.cluster.local.:3100").rstrip("/")
RULES_FILE = os.environ.get("RULES_FILE", "/etc/log-alert/rules.json")
PORT = int(os.environ.get("METRICS_PORT", "9223"))
REFRESH = int(os.environ.get("REFRESH_SECS", "60"))
TIMEOUT = int(os.environ.get("LOKI_TIMEOUT_SECS", "30"))

_lock = threading.Lock()
STATE = {"values": {}, "errors": {}, "rules": [], "ts": 0, "started": int(time.time())}


def load_rules():
    with open(RULES_FILE, "r", encoding="utf-8") as fh:
        doc = json.load(fh)
    rules = doc.get("rules") or []
    seen = set()
    for r in rules:
        name = (r.get("name") or "").strip()
        expr = (r.get("expr") or "").strip()
        if not name or not expr:
            raise SystemExit(f"bad rule entry: {r!r}")
        if name in seen:
            raise SystemExit(f"duplicate rule name: {name}")
        seen.add(name)
    return rules


def loki_instant(expr):
    """-> list of float values from an instant LogQL query."""
    url = LOKI_URL + "/loki/api/v1/query?" + urllib.parse.urlencode(
        {"query": expr, "time": str(int(time.time()))}
    )
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        doc = json.loads(resp.read().decode("utf-8"))
    if doc.get("status") != "success":
        raise RuntimeError(f"loki status {doc.get('status')!r}")
    out = []
    for s in doc["data"]["result"]:
        if "value" in s:                      # vector
            out.append(float(s["value"][1]))
        elif "values" in s:                   # matrix - sum the last point of each
            for ts, val in s["values"]:
                out.append(float(val))
    return out


def collect():
    values = {}
    # Cumulative across cycles: a fresh dict here would reset the "counter" every
    # 60s, making rate()/increase() on log_alert_eval_errors_total meaningless and
    # firing LogAlertBridgeEvalErrors on a single transient Loki blip.
    with _lock:
        errors = dict(STATE.get("errors", {}))
    for rule in STATE["rules"]:
        name = rule["name"]
        try:
            got = loki_instant(rule["expr"])
            values[name] = sum(got) if got else 0.0
            errors.setdefault(name, 0)
        except Exception as exc:  # noqa: BLE001 - a broken query must not fire an alert
            print(f"[eval] {name}: {type(exc).__name__}: {exc}", flush=True)
            values[name] = 0.0
            errors[name] = errors.get(name, 0) + 1
    body = ["# HELP log_alert_value Last evaluated value of a LogQL alert query (0 = no matching lines).",
            "# TYPE log_alert_value gauge"]
    for name in sorted(values):
        body.append(f'log_alert_value{{rule="{name}"}} {values[name]}')
    body += ["# HELP log_alert_eval_errors_total Failed Loki evaluations since start.",
             "# TYPE log_alert_eval_errors_total counter"]
    for name in sorted(errors):
        body.append(f'log_alert_eval_errors_total{{rule="{name}"}} {errors[name]}')
    body += ["# HELP log_alert_scrape_age_seconds Seconds since the last successful evaluation pass.",
             "# TYPE log_alert_scrape_age_seconds gauge",
             f"log_alert_scrape_age_seconds {int(time.time()) - STATE['ts'] if STATE['ts'] else -1}"]
    with _lock:
        STATE["values"] = values
        STATE["errors"] = errors
        STATE["metrics"] = ("\n".join(body) + "\n").encode()
        STATE["ts"] = int(time.time())


class H(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.startswith("/metrics"):
            with _lock:
                body = STATE.get("metrics", b"")
            self.send_response(200 if body else 503)
            self.send_header("Content-Type", "text/plain; version=0.0.4")
            self.end_headers()
            self.wfile.write(body or b"")
            return
        with _lock:
            up = int(time.time()) - STATE["started"]
        self.send_response(200)
        self.send_header("Content-Type", "text/plain")
        self.end_headers()
        self.wfile.write(f"ok rules={len(STATE['rules'])} uptime_s={up}\n".encode())

    def log_message(self, format, *args):  # noqa: A002 - stdlib signature
        pass


def refresher():
    while True:
        try:
            collect()
        except Exception as exc:  # noqa: BLE001
            print(f"[refresh] {type(exc).__name__}: {exc}", flush=True)
        time.sleep(REFRESH)


if __name__ == "__main__":
    STATE["rules"] = load_rules()
    collect()
    threading.Thread(target=refresher, daemon=True).start()
    print(f"log-alert bridge on :{PORT}, rules={[r['name'] for r in STATE['rules']]}", flush=True)
    HTTPServer(("0.0.0.0", PORT), H).serve_forever()
