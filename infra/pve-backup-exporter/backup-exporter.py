#!/usr/bin/env python3
"""PVE backup-job exporter (stdlib only, python:3.12-alpine).

pve-exporter exposes `pve_not_backed_up_info` but no last-run timestamps, so a
missed or failed vzdump job is invisible. This polls the PVE API for the backup
job configs and the vzdump task history and exposes, per job and per guest:

  pve_backup_job_last_run_timestamp_seconds{id,schedule,storage,frequency,interval}
  pve_backup_job_last_status{id}              1 = every guest task OK, 0 = any not OK
  pve_backup_job_enabled{id}
  pve_backup_job_next_run_timestamp_seconds{id}
  pve_backup_guest_last_run_timestamp_seconds{vmid,interval}
  pve_backup_guest_last_status{vmid}          1 = OK, 0 = failed / job errors
                                              (lower bound: PVE skips per-guest
                                              tasks on some multi-guest runs)
  pve_backup_jobrun_status{node}              1 = newest vzdump run on the node
                                                had every task OK. A "run" is the
                                                newest task plus every task that
                                                started within RUN_WINDOW of it.
  pve_backup_jobrun_last_run_timestamp_seconds{node}
  pve_backup_jobrun_task_count{node}
  pve_backup_jobrun_expected_interval_seconds{node}
                                              shortest enabled job interval whose
                                              guests live on this node (0 = none)
  pve_backup_job_missed_run{id}               1 = enabled job's next-run slot is
                                                more than one interval in the past

Note on task ids: PVE writes an aggregate vzdump task with id='' for some runs
only. Relying on id='' freezes the gauge once a node stops producing those
(a job that lost guests leaves a single-guest run, which writes vzdump:<vmid>
instead) - that is what pinned PVEBackupJobRunErrors on pve-nuc10 for a day
after the real fault was already fixed. Per-guest status is therefore a lower
bound, and node status is derived from the newest run instead.

Auth reuses the pve-exporter token (secret pve-exporter, key PVE_TOKEN_VALUE).
"""
import json
import os
import re
import ssl
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer

PVE_HOST = os.environ.get("PVE_HOST", "192.168.12.201")
PVE_PORT = int(os.environ.get("PVE_PORT", "8006"))
PVE_USER = os.environ.get("PVE_USER", "nikhil@pve")
PVE_TOKEN_NAME = os.environ.get("PVE_TOKEN_NAME", "grafana")
PVE_TOKEN_VALUE = os.environ["PVE_TOKEN_VALUE"]
PVE_VERIFY_SSL = os.environ.get("PVE_VERIFY_SSL", "false").lower() == "true"
NODES = [n.strip() for n in os.environ.get("PVE_NODES", "pve,pve-nuc10,pve-hp-g9").split(",") if n.strip()]
PORT = int(os.environ.get("METRICS_PORT", "9222"))
REFRESH = int(os.environ.get("REFRESH_SECS", "60"))
# Tasks whose starttime is within this many seconds of the newest task on a node
# belong to the same vzdump run (PVE starts a job's guests together).
RUN_WINDOW = int(os.environ.get("RUN_WINDOW_SECS", "60"))

AUTH = f"PVEAPIToken={PVE_USER}!{PVE_TOKEN_NAME}={PVE_TOKEN_VALUE}"
_lock = threading.Lock()
STATE = {"metrics": b"", "ts": 0}


def http_json(path, timeout=15):
    ctx = ssl.create_default_context()
    if not PVE_VERIFY_SSL:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    req = urllib.request.Request(
        f"https://{PVE_HOST}:{PVE_PORT}/api2/json{path}",
        headers={"Authorization": AUTH, "Accept": "application/json"})
    with urllib.request.urlopen(req, context=ctx, timeout=timeout) as resp:
        data = resp.read()
        if data[:2] == b"\x1f\x8b":
            import gzip
            data = gzip.decompress(data)
        return json.loads(data)["data"]


def parse_schedule(schedule):
    """-> (frequency_label, interval_seconds)."""
    s = (schedule or "").strip()
    if re.match(r"^\*-\*-\d+", s):
        return "monthly", 30 * 86400
    m = re.match(r"^[a-z]{3}\s", s)
    if m:
        return "weekly", 7 * 86400
    if re.match(r"^\d{1,2}:\d{2}", s) or "*" in s:
        return "daily", 86400
    return "unknown", 86400


def ok(status):
    return 1 if str(status).upper() == "OK" else 0


def collect():
    jobs = http_json("/cluster/backup") or []
    guest_node = {}
    try:
        for r in http_json("/cluster/resources?type=vm") or []:
            if r.get("vmid") is not None and r.get("node"):
                guest_node[str(r["vmid"])] = r["node"]
    except Exception:
        pass
    tasks = []
    for node in NODES:
        try:
            for t in http_json(f"/nodes/{node}/tasks?limit=300") or []:
                if t.get("type") == "vzdump":
                    t["_node"] = node
                    tasks.append(t)
        except Exception:
            continue

    out = []
    for j in jobs:
        freq, interval = parse_schedule(j.get("schedule", ""))
        vmids = [v.strip() for v in str(j.get("vmid", "")).split(",") if v.strip()]
        id_ = j["id"]
        labels = (f'id="{id_}",schedule="{j.get("schedule", "")}",'
                  f'storage="{j.get("storage", "")}",frequency="{freq}",interval="{interval}"')
        out.append(f'pve_backup_job_enabled{{{labels}}} {1 if j.get("enabled") == 1 else 0}')
        out.append(f'pve_backup_job_next_run_timestamp_seconds{{{labels}}} '
                   f'{int(j.get("next-run") or 0)}')

        # Status comes from the per-guest tasks only. PVE does not write a
        # per-guest task for every multi-guest run, so this is a lower bound on
        # freshness (dashboards use it) -- job *freshness* is deliberately not
        # alerted here: the task model cannot attribute id='' aggregate tasks to
        # a specific job without the schedules colliding (01:30 vs 02:00).
        mine = [t for t in tasks if str(t.get("id")) in vmids]
        if mine:
            newest = max(mine, key=lambda t: t.get("starttime", 0))
            out.append(f'pve_backup_job_last_run_timestamp_seconds{{{labels}}} '
                       f'{int(newest.get("starttime", 0))}')
            out.append(f'pve_backup_job_last_status{{{labels}}} '
                       f'{1 if all(ok(t.get("status")) for t in mine) else 0}')
        else:
            out.append(f'pve_backup_job_last_run_timestamp_seconds{{{labels}}} 0')
            out.append(f'pve_backup_job_last_status{{{labels}}} 0')
        out.append(f'pve_backup_job_interval_seconds{{id="{id_}"}} {interval}')
        # A missed slot: PVE leaves next-run in the past when a scheduled run
        # does not happen. Referenced by PVEBackupJobFailed, which previously
        # could never fire because this series did not exist.
        nr = int(j.get("next-run") or 0)
        missed = 1 if (j.get("enabled") == 1 and nr
                       and (time.time() - nr) > interval) else 0
        out.append(f'pve_backup_job_missed_run{{id="{id_}"}} {missed}')

    # Node-level status from the newest vzdump RUN, not from id='' aggregates.
    for node in NODES:
        node_tasks = [t for t in tasks if t["_node"] == node]
        if not node_tasks:
            continue
        newest = max(node_tasks, key=lambda t: t.get("starttime", 0))
        t0 = newest.get("starttime", 0)
        run = [t for t in node_tasks if abs(t.get("starttime", 0) - t0) <= RUN_WINDOW]
        out.append(f'pve_backup_jobrun_status{{node="{node}"}} '
                   f'{1 if all(ok(t.get("status")) for t in run) else 0}')
        out.append(f'pve_backup_jobrun_last_run_timestamp_seconds{{node="{node}"}} {int(t0)}')
        out.append(f'pve_backup_jobrun_task_count{{node="{node}"}} {len(run)}')
        intervals = []
        for j in jobs:
            if j.get("enabled") != 1:
                continue
            _freq, iv = parse_schedule(j.get("schedule", ""))
            vmids = [v.strip() for v in str(j.get("vmid", "")).split(",") if v.strip()]
            if any(guest_node.get(v) == node for v in vmids):
                intervals.append(iv)
        out.append(f'pve_backup_jobrun_expected_interval_seconds{{node="{node}"}} '
                   f'{min(intervals) if intervals else 0}')

    body = ("\n".join(out) + "\n").encode()
    with _lock:
        STATE["metrics"] = body
        STATE["ts"] = int(time.time())


class H(BaseHTTPRequestHandler):
    def do_GET(self):
        with _lock:
            body = STATE["metrics"]
        self.send_response(200 if body else 503)
        self.send_header("Content-Type", "text/plain; version=0.0.4")
        self.end_headers()
        self.wfile.write(body or b"")

    def log_message(self, format, *args):  # noqa: A002 - stdlib signature
        pass


def refresher():
    while True:
        try:
            collect()
        except Exception as e:
            print(f"[refresh] {type(e).__name__}: {e}", flush=True)
        time.sleep(REFRESH)


if __name__ == "__main__":
    collect()
    threading.Thread(target=refresher, daemon=True).start()
    print(f"pve-backup-exporter on :{PORT}, nodes={NODES}", flush=True)
    HTTPServer(("0.0.0.0", PORT), H).serve_forever()
