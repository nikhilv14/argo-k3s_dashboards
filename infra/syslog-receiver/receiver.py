#!/usr/bin/env python3
"""Syslog -> Loki relay (UDP + TCP 514), stdlib only.

Runs as a DaemonSet with hostPort 514 on every node. A MetalLB LoadBalancer
Service fronts it with a single VIP, so a sender (OpenWrt logd, which accepts
exactly ONE remote target) always has somewhere to go; if the node holding the
VIP dies, MetalLB re-announces it from a node that still runs the relay.

Parsing is deliberately defensive: RFC3164 (OpenWrt/busybox logd) and RFC5424
both appear in the wild, and a line we cannot parse is still shipped - it just
carries fewer labels.
"""
import json
import os
import re
import socket
import socketserver
import threading
import time
import urllib.request

LOKI_URL = os.environ.get("LOKI_URL", "http://loki.monitoring.svc.cluster.local:3100/loki/api/v1/push")
LISTEN = os.environ.get("LISTEN", "0.0.0.0:514")
HEALTH_PORT = int(os.environ.get("HEALTH_PORT", "9094"))
BATCH_SECS = float(os.environ.get("BATCH_SECS", "1.0"))
MAX_LINE = int(os.environ.get("MAX_LINE", "8192"))
DEBUG = os.environ.get("DEBUG", "false").lower() == "true"

def dbg(*a):
    if DEBUG:
        print("[dbg]", *a, flush=True)

_queue = []
_lock = threading.Lock()

# <PRI> then optional TAG[pid]: / TAG:
PRI = re.compile(r"^<(\d{1,3})>")
RFC3164 = re.compile(
    r"^(?P<ts>[A-Z][a-z]{2}\s+\d{1,2}\s\d{2}:\d{2}:\d{2})\s+"
    r"(?P<host>\S+)\s+(?P<tag>[^\s:\[\]]+)(?:\[\d+\])?:\s?(?P<msg>.*)$"
)
RFC5424 = re.compile(
    r"^<(?P<pri>\d{1,3})>\d+\s+\S+\s+(?P<host>\S+)\s+(?P<tag>\S+)\s+.*$"
)
SEVERITY = ["emerg", "alert", "crit", "err", "warning", "notice", "info", "debug"]


def parse(line, peer):
    """-> (labels dict, text)"""
    labels = {"job": "syslog", "host": peer}
    m = PRI.match(line)
    pri = None
    if m:
        pri = int(m.group(1))
        line = line[m.end():]
    r = RFC3164.match(line)
    if r:
        labels["host"] = r.group("host")
        labels["app"] = r.group("tag")
        msg = r.group("msg")
    else:
        r2 = RFC5424.match(line)
        if r2:
            labels["host"] = r2.group("host")
            labels["app"] = r2.group("tag")
            msg = line
        else:
            msg = line
    if pri is not None:
        labels["level"] = SEVERITY[pri % 8] if pri % 8 < len(SEVERITY) else "info"
        labels["facility"] = str(pri // 8)
    return labels, msg[:MAX_LINE]


def enqueue(line, peer):
    labels, msg = parse(line, peer)
    ts = time.time_ns()
    with _lock:
        _queue.append((ts, labels, msg))


def pusher():
    # _queue is module state; without this declaration the rebind below makes it
    # a local and the thread dies on UnboundLocalError before pushing anything.
    global _queue
    while True:
        time.sleep(BATCH_SECS)
        with _lock:
            # rebind, never `batch, _queue[:] = _queue, []` - that assigns batch
            # to the SAME list object and then clears it, so every batch is empty
            batch, _queue = _queue, []
        if not batch:
            continue
        streams = {}
        for ts, labels, msg in batch:
            key = tuple(sorted(labels.items()))
            streams.setdefault(key, []).append([str(ts), msg])
        payload = {"streams": [
            {"stream": dict(k), "values": v} for k, v in streams.items()]}
        req = urllib.request.Request(
            LOKI_URL, data=json.dumps(payload).encode(),
            headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                if r.status not in (200, 204):
                    print(f"[loki] HTTP {r.status}", flush=True)
        except Exception as e:
            print(f"[loki] {type(e).__name__}: {e}", flush=True)


class UDPHandler(socketserver.BaseRequestHandler):
    def handle(self):
        data = self.request[0]
        dbg("udp packet", len(data), "bytes from", self.client_address)
        try:
            enqueue(data.decode("utf-8", "replace").rstrip("\n"), self.client_address[0])
        except Exception as e:
            print(f"[udp] {e!r}", flush=True)


class TCPHandler(socketserver.StreamRequestHandler):
    def handle(self):
        for raw in self.rfile:
            try:
                enqueue(raw.decode("utf-8", "replace").rstrip("\n"), self.client_address[0])
            except Exception as e:
                print(f"[tcp] {e!r}", flush=True)


class HealthHandler(socketserver.BaseRequestHandler):
    def handle(self):
        self.request.sendall(b"ok\n")


def serve(protocol):
    host, port = LISTEN.split(":")
    if protocol == "udp":
        socketserver.UDPServer.allow_reuse_address = True
        srv = socketserver.UDPServer((host, int(port)), UDPHandler)
    else:
        socketserver.TCPServer.allow_reuse_address = True
        srv = socketserver.ThreadingTCPServer((host, int(port)), TCPHandler)
    dbg("serving", protocol, "on", LISTEN)
    srv.serve_forever()


if __name__ == "__main__":
    threading.Thread(target=pusher, daemon=True).start()
    threading.Thread(target=serve, args=("udp",), daemon=True).start()
    threading.Thread(target=serve, args=("tcp",), daemon=True).start()
    socketserver.TCPServer.allow_reuse_address = True
    socketserver.TCPServer(("0.0.0.0", HEALTH_PORT), HealthHandler).serve_forever()
