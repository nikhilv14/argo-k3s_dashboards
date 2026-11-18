#!/usr/bin/env python3
"""Ship docker container logs from CT104 (media-stack) to the homelab syslog relay.

Jellyfin and friends run under docker compose on CT104, outside k8s, so the
promtail DaemonSet never sees them. The cluster already runs a syslog relay on
the MetalLB VIP 192.168.12.62:514 that forwards into Loki, so this tails the
docker json log and re-emits RFC3164 datagrams at it - no new exposure, no
promtail binary to keep patched.

Resulting streams in Loki: {job="syslog", host="media-stack", app="<container>"}

Config (env, or /etc/default/media-log-ship):
  MEDIA_LOG_CONTAINERS   comma list, default "jellyfin" (add apps as they onboard)
  MEDIA_LOG_SYSLOG       ip:port, default 192.168.12.62:514
"""
import json
import os
import socket
import time
import subprocess

CONTAINERS = [c.strip() for c in os.environ.get(
    "MEDIA_LOG_CONTAINERS", "jellyfin").split(",") if c.strip()]
SYSLOG = os.environ.get("MEDIA_LOG_SYSLOG", "192.168.12.62:514")
HOSTNAME = os.environ.get("MEDIA_LOG_HOSTNAME", "media-stack")
DOCKER_ROOT = "/var/lib/docker/containers"
FACILITY = 3 * 8  # daemon
MAX_LINE = 4000


def log(msg):
    print(f"[ship] {msg}", flush=True)


def container_log_path(name):
    try:
        cid = subprocess.run(
            ["docker", "inspect", "-f", "{{.Id}}", name],
            capture_output=True, text=True, timeout=15).stdout.strip()
    except Exception as e:
        log(f"inspect {name} failed: {e}")
        return None
    if not cid:
        log(f"container {name} not found")
        return None
    return os.path.join(DOCKER_ROOT, cid, f"{cid}-json.log")


def severity(stream, msg):
    low = msg.lower()
    if stream == "stderr" or " error" in low or low.startswith("error") or "fail" in low:
        return 4  # warning
    return 6      # info


class Tailer:
    def __init__(self, name, path, sock, addr):
        self.name, self.path, self.sock, self.addr = name, path, sock, addr
        self.fh = None
        self.inode = None

    def open_at_end(self):
        try:
            self.fh = open(self.path, "r", errors="replace")
            self.fh.seek(0, os.SEEK_END)
            self.inode = os.stat(self.path).st_ino
        except FileNotFoundError:
            self.fh = None

    def reopen_if_rotated(self):
        try:
            st = os.stat(self.path)
        except FileNotFoundError:
            # docker recreated the file; open from the beginning
            self.fh = None
            return
        if self.inode != st.st_ino:
            self.inode = st.st_ino
            try:
                self.fh = open(self.path, "r", errors="replace")
            except FileNotFoundError:
                self.fh = None

    def pump(self):
        if self.fh is None:
            self.open_at_end()
            if self.fh is None:
                return
        self.reopen_if_rotated()
        if self.fh is None:
            return
        for _ in range(200):  # bounded per tick so one noisy container can't starve others
            line = self.fh.readline()
            if not line:
                break
            try:
                obj = json.loads(line)
            except ValueError:
                continue
            msg = (obj.get("log") or "").rstrip("\n")
            if not msg:
                continue
            for part in msg.split("\n"):
                if not part.strip():
                    continue
                pri = FACILITY + severity(obj.get("stream"), part)
                stamp = time.strftime("%b %d %H:%M:%S")
                payload = f"<{pri}>{stamp} {HOSTNAME} {self.name}[0]: {part[:MAX_LINE]}"
                try:
                    self.sock.sendto(payload.encode("utf-8", "replace"), self.addr)
                except Exception as e:
                    log(f"send failed: {e}")


def main():
    host, port = SYSLOG.rsplit(":", 1)
    addr = (host, int(port))
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 1 << 20)
    log(f"shipping {CONTAINERS} -> {SYSLOG} as host={HOSTNAME}")
    tailers = []
    paths = {}
    while True:
        for name in CONTAINERS:
            if name not in paths:
                p = container_log_path(name)
                if p:
                    paths[name] = p
                    tailers.append(Tailer(name, p, sock, addr))
                    log(f"tailing {name} -> {p}")
        for t in tailers:
            try:
                t.pump()
            except Exception as e:
                log(f"pump {t.name}: {e}")
        time.sleep(1.0)


if __name__ == "__main__":
    main()
