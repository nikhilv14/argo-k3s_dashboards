#!/usr/bin/env bash
# prepare-k8s-iscsi-nodes.sh
# Idempotent node-level prerequisites for democratic-csi (TrueNAS iSCSI) on k3s.
# Run on EVERY node in the cluster: k3s-node1, k3s-server-nuc, k3s-server-hp-g9,
# k3s-worker-z690 (and any future node), as root (or with sudo).
#
# What it does:
#   1. Installs open-iscsi (iscsiadm), multipath-tools, lsscsi, and file-system tools.
#   2. Loads and persists the iscsi_tcp / dm_multipath kernel modules.
#   3. Enables + starts the iscsid daemon (required for in-node iSCSI sessions —
#      unlike Longhorn's embedded iscsid, democratic-csi uses the host's open-iscsi).
#   4. Ensures the kubelet directory is in a "rshared" mount propagation so the
#      CSI node plugin can bind-mount block devices from the host into containers.
#   5. Prints the node's unique Initiator IQN (needed for TrueNAS initiator group).
#   6. Restarts the k3s agent to pick up the new mount propagation.
#
# Usage:
#   sudo bash prepare-k8s-iscsi-nodes.sh [--restart-k3s]
set -euo pipefail

RESTART_K3S=false
if [[ "${1:-}" == "--restart-k3s" ]]; then
  RESTART_K3S=true
fi

log() { echo -e "[iscsi-prep] $*"; }

# --- 1. Packages -------------------------------------------------------------
log "installing packages..."
if command -v apt-get >/dev/null 2>&1; then
  apt-get update -qq
  DEBIAN_FRONTEND=noninteractive apt-get install -y -qq \
    open-iscsi multipath-tools lsscsi xfsprogs e2fsprogs jq >/dev/null
elif command -v dnf >/dev/null 2>&1; then
  dnf install -y -q iscsi-initiator-utils device-mapper-multipath lsscsi xfsprogs e2fsprogs jq
else
  log "ERROR: unsupported package manager (need apt-get or dnf)"; exit 1
fi

# --- 2. Kernel modules ------------------------------------------------------
log "loading kernel modules..."
for mod in iscsi_tcp dm_multipath; do
  if ! lsmod | grep -q "^${mod} "; then
    modprobe "$mod" 2>/dev/null || log "WARN: could not load $mod (kernel may have it built-in)"
  fi
done
# Persist across reboots.
MODULES_FILE="/etc/modules-load.d/iscsi-csi.conf"
if [[ ! -f "$MODULES_FILE" ]]; then
  cat > "$MODULES_FILE" <<EOF
# Required by democratic-csi (TrueNAS iSCSI) node plugin
iscsi_tcp
dm_multipath
EOF
  log "wrote $MODULES_FILE"
fi

# --- 3. iscsid daemon -------------------------------------------------------
log "enabling iscsid..."
systemctl enable --now iscsid 2>/dev/null || \
  systemctl enable --now iscsid.socket || log "WARN: iscsid not enabled (may already be running)"
systemctl is-active --quiet iscsid 2>/dev/null && log "iscsid is active" || \
  log "WARN: iscsid not active — check 'systemctl status iscsid'"

# --- 4. Kubelet rshared mount propagation -----------------------------------
KUBELET_DIR="/var/lib/kubelet"
if [[ -d /var/lib/rancher/k3s/agent/kubelet ]]; then
  KUBELET_DIR="/var/lib/rancher/k3s/agent/kubelet"
fi
log "ensuring $KUBELET_DIR is in rshared propagation..."
if [[ -d "$KUBELET_DIR" ]]; then
  CURRENT_PROPAGATION=$(findmnt -n -o PROPAGATION "$KUBELET_DIR" 2>/dev/null || echo "unknown")
  if [[ "$CURRENT_PROPAGATION" != *shared* ]]; then
    mount --make-rshared "$KUBELET_DIR" || log "WARN: could not set rshared on $KUBELET_DIR"
  fi
  # Persist across reboots via a systemd one-shot (works for K3s and vanilla kubelet).
  UNIT="/etc/systemd/system/kubelet-rshared.service"
  if [[ ! -f "$UNIT" ]]; then
    cat > "$UNIT" <<EOF
[Unit]
Description=Make kubelet dir rshared for CSI node plugins
After=local-fs.target
Before=kubelet.service k3s.service

[Service]
Type=oneshot
ExecStart=/bin/mount --make-rshared ${KUBELET_DIR}

[Install]
WantedBy=multi-user.target
EOF
    systemctl daemon-reload
    systemctl enable kubelet-rshared.service >/dev/null 2>&1 || true
    log "wrote and enabled $UNIT"
  fi
else
  log "WARN: $KUBELET_DIR does not exist yet — will be created on first pod"
fi

# --- 5. Restart k3s agent (optional, to re-load mount propagation) ----------
if [[ "$RESTART_K3S" == true ]]; then
  log "restarting k3s agent..."
  systemctl restart k3s 2>/dev/null || true
  log "k3s restarted (this will temporarily interrupt local workloads)"
fi

# --- 6. Gather and print the node IQN --------------------------------------
log "=== NODE SUMMARY ==="
IQN=$(iscsiadm -m node --show 2>/dev/null | head -1 || true)
if [[ -f /etc/iscsi/initiatorname.iscsi ]]; then
  IQN=$(grep -E '^InitiatorName=' /etc/iscsi/initiatorname.iscsi | cut -d'=' -f2)
fi
if [[ -z "$IQN" ]]; then
  log "WARN: no IQN found — generate with 'iscsi-iname > /etc/iscsi/initiatorname.iscsi'"
else
  log "Initiator IQN (add this to TrueNAS initiator group 'k8s-nodes'):"
  echo "  $IQN"
fi
log "iscsiadm version: $(iscsiadm --version 2>/dev/null | head -1 || echo 'unknown')"
log "multipath: $(command -v multipath >/dev/null && echo 'installed' || echo 'missing')"
log "lsscsi: $(lsscsi 2>/dev/null | head -1 | grep -q . && echo 'working' || echo 'no devices found')"
log "done."
