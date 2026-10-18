#!/usr/bin/env bash
# prepare-k8s-nfs-nodes.sh
# Phase 1: Node-level prerequisites for democratic-csi NFS driver
# (freenas-api-nfs). NFS needs NO kernel iSCSI sessions — only the kernel
# NFS client (always present) and proper kubelet dir propagation.
# Run on EVERY node: k3s-node1, k3s-server-nuc, k3s-server-hp-g9,
# k3s-worker-z690 (LXC — the whole point of the NFS driver).
set -euo pipefail

NODE_NAME=$(hostname)
KUBELET_DIR="/var/lib/kubelet"

log() { echo "[$(date +%H:%M:%S)] $*"; }

log "=== Phase 1 NFS prep: ${NODE_NAME} ==="

# 1. NFS client tools ( nfs-common for Debian/Ubuntu; kernel nfs client is
#    builtin but mount.nfs + rpcbind help older distros )
log "ensuring nfs-common..."
if command -v apt-get >/dev/null; then
  sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq nfs-common rpcbind \
    >/dev/null 2>&1 || true
elif command -v dnf >/dev/null; then
  sudo dnf install -y -q nfs-utils >/dev/null 2>&1 || true
fi

# 2. kubelet dir propagation (required by CSI node plugins — same as iSCSI)
log "ensuring ${KUBELET_DIR} rshared propagation..."
sudo mkdir -p "${KUBELET_DIR}"
sudo mount --bind "${KUBELET_DIR}" "${KUBELET_DIR}" 2>/dev/null || true
sudo mount --bind -o rshared "${KUBELET_DIR}" "${KUBELET_DIR}"
PROP=$(findmnt -n -o PROPAGATION "${KUBELET_DIR}" | head -1)
log "propagation: ${PROP}"
case "${PROP}" in
  *shared*) log "OK" ;;
  *) log "WARNING: propagation not shared — CSI mounts may fail" ;;
esac

# 3. Persistent bind-mount unit (idempotent one-shot, survives reboot)
log "installing k3s-csi-propagation.service..."
sudo tee /etc/systemd/system/k3s-csi-propagation.service >/dev/null <<EOF
[Unit]
Description=Ensure kubelet dir is rshared for CSI plugins (k3s)
After=k3s.service k3s-agent.service
Before=docker.service
[Service]
Type=oneshot
ExecStart=/bin/sh -c 'mkdir -p ${KUBELET_DIR} && mount --bind ${KUBELET_DIR} ${KUBELET_DIR} 2>/dev/null; mount --bind -o rshared ${KUBELET_DIR} ${KUBELET_DIR}'
RemainAfterExit=yes
[Install]
WantedBy=multi-user.target
EOF
sudo systemctl daemon-reload
sudo systemctl enable --now k3s-csi-propagation.service

# 4. Summary
log "NFS service client check: mount.nfs = $(command -v mount.nfs || echo missing)"
log "=== DONE: ${NODE_NAME} NFS prep complete ==="
