#!/usr/bin/env python3
"""Syslog -> Loki relay (UDP + TCP 514), stdlib only.

Runs as a DaemonSet with hostPort 514 on every node. A MetalLB LoadBalancer
Service fronts it with a single VIP, so a sender (OpenWrt logd, which accepts
exactly ONE remote target) always has somewhere to go; if the node holding the
VIP dies, MetalLB re-announces it from a node that still runs the relay.

Parsing is deliberately defensive: RFC3164 (OpenWrt/busybox logd) and RFC5424
both appear in the wild, and a line we cannot parse is still shipped - it just
carries fewer labels.

Noise is dropped HERE, at ingestion. Filtering in the dashboard is not enough:
Loki stores what it is pushed, and the router emits tens of thousands of
multicast/broadcast firewall rejects a day.
"""
import json
import os
import re
import socketserver
import threading
import time
import urllib.parse
import urllib.request

LOKI_URL = os.environ.get("LOKI_URL", "http://loki.monitoring.svc.cluster.local:3100/loki/api/v1/push")
LISTEN = os.environ.get("LISTEN", "0.0.0.0:514")
HEALTH_PORT = int(os.environ.get("HEALTH_PORT", "9094"))
BATCH_SECS = float(os.environ.get("BATCH_SECS", "1.0"))
MAX_LINE = int(os.environ.get("MAX_LINE", "8192"))
DEBUG = os.environ.get("DEBUG", "false").lower() == "true"

# --------------------------------------------------------------------------
# Ingestion filters
#   DROP_REJECTS=noise|all|none   firewall "reject ... in:" lines
#   DROP_DNS=0|1                  dnsmasq "query[...] ... from ..." lines
#   DROP_PATTERNS="re1,re2"       extra regexes matched against the message
# --------------------------------------------------------------------------
REJECT_NOISE = re.compile(
    r"reject \S+ in:.*DST=(?:255\.255\.255\.255|ff02:|fe80:|192\.168\.12\.255)")
REJECT_ANY = re.compile(r"reject \S+ in:")
DNS_QUERY = re.compile(r"query\[[A-Z]+\] .* from ")


def _build_drops():
    mode = os.environ.get("DROP_REJECTS", "noise").strip().lower()
    pats = []
    if mode == "all":
        pats.append(REJECT_ANY)
    elif mode == "noise":
        pats.append(REJECT_NOISE)
    if os.environ.get("DROP_DNS", "0").strip() == "1":
        pats.append(DNS_QUERY)
    for extra in os.environ.get("DROP_PATTERNS", "").split(","):
        extra = extra.strip()
        if extra:
            pats.append(re.compile(extra))
    return pats


DROPS = _build_drops()
_dropped = {}

_queue = []
_lock = threading.Lock()

# --------------------------------------------------------------------------
# IP -> hostname map, so panels can show "beast12" instead of "192.168.12.181".
#
# dnsmasq logs DHCPACK(<iface>) <ip> <mac> <hostname>; the relay collects those
# from Loki (not from its own socket) so every pod converges on the same map
# instead of only knowing the leases that happened to land on its VIP hash.
#   DHCP_STREAM   selector to scan for DHCPACK lines
#   DHCP_LOOKBACK seconds of history to scan on each refresh
#   HOSTMAP_REFRESH  seconds between refreshes
#   HOSTMAP_EXTRA    "ip=name,ip=name" static entries
# --------------------------------------------------------------------------
DHCP_STREAM = os.environ.get(
    "DHCP_STREAM", '{job="syslog", host="OpenWrt", app="dnsmasq-dhcp"}')
DHCP_LOOKBACK = int(os.environ.get("DHCP_LOOKBACK", "86400"))
HOSTMAP_REFRESH = float(os.environ.get("HOSTMAP_REFRESH", "300"))
DHCPACK = re.compile(
    r"DHCPACK\([^)]*\)\s+(?P<ip>\S+)\s+(?P<mac>\S+)(?:\s+(?P<name>\S+))?")
SRC_RE = re.compile(r"(?:^|\s)SRC=(?P<ip>[^ ]+)")
DST_RE = re.compile(r"(?:^|\s)DST=(?P<ip>[^ ]+)")
_hosts = {}


def _refresh_hosts():
    global _hosts
    base = LOKI_URL.split("/loki/api/v1/push")[0]
    end = int(time.time())
    url = base + "/loki/api/v1/query_range?" + urllib.parse.urlencode({
        "query": DHCP_STREAM + ' |= "DHCPACK"',
        "start": str(end - DHCP_LOOKBACK), "end": str(end), "limit": "5000"})
    try:
        with urllib.request.urlopen(url, timeout=20) as r:
            data = json.load(r)
    except Exception as e:
        print(f"[hosts] refresh failed: {type(e).__name__}: {e}", flush=True)
        return
    m = {}
    for s in data.get("data", {}).get("result", []):
        for _ts, line in s.get("values", []):
            d = DHCPACK.search(line)
            if d and d.group("name"):
                m[d.group("ip")] = d.group("name")
    for pair in os.environ.get("HOSTMAP_EXTRA", "").split(","):
        if "=" in pair:
            ip, name = pair.split("=", 1)
            m[ip.strip()] = name.strip()
    with _lock:
        _hosts = m
    print(f"[hosts] {len(m)} ip->hostname entries", flush=True)


def _hostmap_loop():
    while True:
        _refresh_hosts()
        time.sleep(HOSTMAP_REFRESH)


def dbg(*a):
    if DEBUG:
        print("[dbg]", *a, flush=True)


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
    for rx in DROPS:
        if rx.search(msg):
            with _lock:
                _dropped[rx.pattern] = _dropped.get(rx.pattern, 0) + 1
            dbg("dropped", rx.pattern, msg[:80])
            return
    with _lock:
        hosts = _hosts
    ms = SRC_RE.search(msg)
    md = DST_RE.search(msg)
    # hostname when known, otherwise the address itself, so a legend reads
    # "beast12" for known hosts and "8.8.8.8" for everything else.
    if ms:
        labels["srchost"] = hosts.get(ms.group("ip"), ms.group("ip"))
    if md:
        labels["dsthost"] = hosts.get(md.group("ip"), md.group("ip"))
    if labels.get("app") == "dnsmasq":
        # "query[A] example.com from 192.168.12.42" -> clienthost
        mc = re.search(r"\sfrom\s(?P<ip>[^ ]+)\s*$", msg)
        if mc:
            labels["clienthost"] = hosts.get(mc.group("ip"), mc.group("ip"))
    ts = time.time_ns()
    with _lock:
        _queue.append((ts, labels, msg))


def pusher():
    # _queue is module state; without this declaration the rebind below makes it
    # a local and the thread dies on UnboundLocalError before pushing anything.
    global _queue
    last_stats = 0.0
    while True:
        time.sleep(BATCH_SECS)
        now = time.time()
        if now - last_stats > 60:
            last_stats = now
            if _dropped:
                print("[drops]", json.dumps(_dropped, sort_keys=True), flush=True)
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
        with _lock:
            dropped = sum(_dropped.values())
        self.request.sendall(f"ok dropped={dropped}\n".encode())


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
    if DROPS:
        print("[filters]", [r.pattern for r in DROPS], flush=True)
    threading.Thread(target=_hostmap_loop, daemon=True).start()
    threading.Thread(target=pusher, daemon=True).start()
    threading.Thread(target=serve, args=("udp",), daemon=True).start()
    threading.Thread(target=serve, args=("tcp",), daemon=True).start()
    socketserver.TCPServer.allow_reuse_address = True
    socketserver.TCPServer(("0.0.0.0", HEALTH_PORT), HealthHandler).serve_forever()
