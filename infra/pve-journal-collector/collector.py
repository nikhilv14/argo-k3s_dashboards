#!/usr/bin/env python3
"""PVE journal + task-log collector.

Polls the Proxmox VE API for each node's systemd journal (and finished task
logs) and pushes them into Loki. Runs as a single Deployment in the cluster,
so no SSH access to the PVE hosts is needed. Auth reuses the same PVE API
token as pve-exporter (secret pve-exporter, key PVE_TOKEN_VALUE).

Journal entries are parsed from the alternating [cursor, line] list the API
returns; the precise timestamp comes from the cursor's `t=` field (epoch
microseconds). Task logs are pushed once per finished UPID.
"""

import gzip
import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer

LOKI_URL = os.environ.get(
    "LOKI_URL", "http://loki.monitoring.svc.cluster.local:3100/loki/api/v1/push"
)
TOKEN = os.environ["PVE_TOKEN_VALUE"]
AUTH = f"PVEAPIToken={os.environ.get('PVE_USER', 'nikhil@pve')}!{os.environ.get('PVE_TOKEN_NAME', 'grafana')}={TOKEN}"
NODES = [n.strip() for n in os.environ.get("PVE_NODES", "pve,pve-nuc10,pve-hp-g9").split(",") if n.strip()]
PORT = int(os.environ.get("PVE_PORT", "8006"))
POLL_SECS = float(os.environ.get("POLL_SECS", "10"))
TASK_POLL_SECS = float(os.environ.get("TASK_POLL_SECS", "60"))
# Loki rejects entries older than ~24h; start only 10 minutes back on boot.
START_BACK = int(os.environ.get("START_BACK_SECS", "600"))
CLUSTER = os.environ.get("PVE_CLUSTER", "beast-cluster")

MAX_LAG_US = int(os.environ.get("MAX_LAG_US", str(12 * 3600 * 1_000_000)))

JOURNAL_LINE = re.compile(
    r"^(?P<ts>\w{3} \d{2} \d{2}:\d{2}:\d{2}) (?P<host>\S+) (?P<tag>[^:\[\s]+)(?:\[(?P<pid>\d+)\])?: ?(?P<msg>.*)$"
)


def http_json(node_host, path, timeout=10):
    url = f"https://{node_host}:{PORT}{path}"
    req = urllib.request.Request(url, headers={"Authorization": AUTH, "Accept": "application/json"})
    # PVE nodes use self-signed / internal-CA certs (pve-exporter sets
    # PVE_VERIFY_SSL=false for the same reason).
    if os.environ.get("PVE_VERIFY_SSL", "true").lower() == "false":
        import ssl
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        opener = urllib.request.build_opener(urllib.request.HTTPSHandler(context=ctx))
        resp = opener.open(req, timeout=timeout)
    else:
        resp = urllib.request.urlopen(req, timeout=timeout)
    with resp:
        # PVE may return gzip regardless of Accept-Encoding.
        data = resp.read()
        if resp.headers.get("Content-Encoding") == "gzip" or data[:2] == b"\x1f\x8b":
            data = gzip.decompress(data)
        return json.loads(data)["data"]


def push_to_loki(streams):
    """streams: {labels_dict: [(ts_ns, line), ...]}"""
    payload = {"streams": [{"stream": labels, "values": vals} for labels, vals in streams.items()]}
    body = json.dumps(payload).encode()
    req = urllib.request.Request(
        LOKI_URL,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        if resp.status != 204:
            raise RuntimeError(f"Loki push returned {resp.status}")


class NodeCollector(threading.Thread):
    def __init__(self, node, host):
        super().__init__(daemon=True, name=f"col-{node}")
        self.node = node
        self.host = host
        self.since_us = int((time.time() - START_BACK) * 1_000_000)
        self.seen_cursors = set()
        self.tasks_seen = set()  # UPIDs already pushed
        self.lock = threading.Lock()
        self.out = {}  # labels -> [(ts_ns, line)] staged for the pusher

    def log(self, msg):
        print(f"[{self.node}] {msg}", flush=True)

    def collect_journal(self):
        # PVE's journal API takes `since` in epoch SECONDS; we track micro-
        # precision internally via the cursor t= field and convert on request.
        since_s = max(self.since_us // 1_000_000 - 1, int(time.time()) - MAX_LAG_US // 1_000_000)
        entries = http_json(self.host, f"/api2/json/nodes/{self.node}/journal?since={since_s}") or []
        # alternating [cursor, line, cursor, line, ...]
        pairs = [(entries[i], entries[i + 1]) for i in range(0, len(entries) - 1, 2)]
        batch = {}
        newest_us = None
        for cursor, line in pairs:
            if cursor in self.seen_cursors:
                continue
            m = re.search(r"[;^]t=([0-9a-f]+)", cursor)
            if not m:
                continue
            ts_us = int(m.group(1), 16)
            if ts_us < self.since_us:
                continue
            self.seen_cursors.add(cursor)
            if len(self.seen_cursors) > 5000:
                self.seen_cursors = set(list(self.seen_cursors)[-2000:])
            newest_us = ts_us if newest_us is None else max(newest_us, ts_us)

            tags = {"job": "pve-journal", "host": self.node, "cluster": CLUSTER}
            lm = JOURNAL_LINE.match(line)
            if lm:
                tags["unit"] = lm.group("tag")
                line = lm.group("msg")
            batch.setdefault(tuple(sorted(tags.items())), []).append((ts_us * 1000, line))
        if newest_us is not None:
            self.since_us = max(self.since_us, newest_us)  # strictly forward
        if batch:
            with self.lock:
                for labels, vals in batch.items():
                    self.out.setdefault(dict(labels), []).extend(vals)

    def collect_tasks(self):
        tasks = http_json(self.host, f"/api2/json/nodes/{self.node}/tasks?limit=50") or []
        batch = {}
        for t in tasks:
            upid = t.get("upid")
            status = t.get("status")
            if not upid or upid in self.tasks_seen or status is None:
                continue
            # only fetch logs for finished tasks
            if status not in ("OK", "unknown", "warning") and not status.startswith("ERR"):
                continue
            self.tasks_seen.add(upid)
            if len(self.tasks_seen) > 2000:
                self.tasks_seen = set(list(self.tasks_seen)[-500:])
            try:
                loglines = http_json(self.host, f"/api2/json/nodes/{self.node}/tasks/{urllib.parse.quote(upid, safe='')}/log?limit=500")
            except Exception as e:
                self.log(f"task log fetch failed {upid}: {e}")
                continue
            # loglines is [[n, text], ...]
            endtime = t.get("endtime") or t.get("starttime") or int(time.time())
            ts_ns = int(endtime) * 1_000_000_000
            tags = {
                "job": "pve-tasks",
                "host": self.node,
                "cluster": CLUSTER,
                "task_type": t.get("type", "unknown"),
                "user": (t.get("user") or "unknown").replace("@", "_"),
            }
            vals = [(ts_ns, ln[1]) for ln in loglines if len(ln) > 1]
            if vals:
                batch.setdefault(tuple(sorted(tags.items())), []).extend(vals)
        if batch:
            with self.lock:
                for labels, vals in batch.items():
                    self.out.setdefault(dict(labels), []).extend(vals)

    def run(self):
        task_polls = 0
        while True:
            try:
                self.collect_journal()
            except Exception as e:
                self.log(f"journal poll failed: {e}")
            task_polls += 1
            if task_polls * POLL_SECS >= TASK_POLL_SECS:
                task_polls = 0
                try:
                    self.collect_tasks()
                except Exception as e:
                    self.log(f"tasks poll failed: {e}")
            time.sleep(POLL_SECS)


def serve_health(port):
    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"ok")

        def log_message(self, *a):
            pass

    HTTPServer(("0.0.0.0", port), H).serve_forever()


def main():
    # PVE_NODES entries are "name=host" or plain "name"; name is the PVE
    # node name used as the Loki `host` label, host is the address to poll
    # (the API of ANY node works for the whole cluster, but we poll each
    # node directly so an outage shows as missing data for that node only).
    node_map = []
    for entry in NODES:
        name, _, host = entry.partition("=")
        node_map.append((name, host or name))
    def pusher():
        while True:
            time.sleep(5)
            batch = {}
            for c in collectors:
                with c.lock:
                    for labels, vals in c.out.items():
                        merged = batch.setdefault(labels, [])
                        merged.extend(vals)
                    c.out.clear()
            if batch:
                try:
                    push_to_loki(batch)
                    print(f"[pusher] sent {sum(len(v) for v in batch.values())} lines", flush=True)
                except Exception as e:
                    print(f"[pusher] push failed: {e}", flush=True)
                    # re-stage for retry
                    for c in collectors:
                        with c.lock:
                            for labels, vals in batch.items():
                                c.out.setdefault(labels, []).extend(vals)

    collectors = [NodeCollector(n, h) for n, h in node_map]
    threading.Thread(target=pusher, daemon=True).start()
    threading.Thread(target=serve_health, args=(int(os.environ.get("HEALTH_PORT", "9300")),), daemon=True).start()
    for c in collectors:
        c.start()
    print(f"collecting {NODES} -> {LOKI_URL}", flush=True)
    while True:
        time.sleep(3600)


if __name__ == "__main__":
    main()
