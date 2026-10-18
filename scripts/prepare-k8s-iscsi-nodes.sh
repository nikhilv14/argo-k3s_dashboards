#!/usr/bin/env bash
# prepare-k8s-iscsi-nodes.sh
# Phase 1: Idempotent node preparation for democratic-csi (TrueNAS iSCSI).
# Run on every k3s node: k3s-node1, k3s-server-nuc, k3s-server-hp-g9, k3s-worker-z690
# Requires root.
#
# NOTE ON MULTIPATH: this is a single-path cluster (one iSCSI portal on the
# TrueNAS appliance). multipathd MUST NOT run: it claims unmounted block
# devices O_EXCL, which wedges Longhorn/democratic-csi NodeStage with
# "mount: already mounted or mount point busy" (verified incident
# 2026-10-18: mpatha/mpathb grabbed Longhorn volumes on k3s-node1).
set -euo pipefail

echo "=== Node prep for iSCSI (democratic-csi) on $(hostname) ==="

# ---------------------------------------------------------------
# 1. Packages
# ---------------------------------------------------------------
if command -v apt-get >/dev/null 2>&1; then
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -qq
  apt-get install -y -qq open-iscsi multipath-tools lsscsi xfsprogs e2fsprogs
elif command -v dnf >/dev/null 2>&1; then
  dnf install -y iscsi-initiator-utils device-mapper-multipath lsscsi xfsprogs e2fsprogs
else
  echo "Unsupported package manager (apt/dnf expected)." >&2; exit 1
fi

# ---------------------------------------------------------------
# 2. Kernel modules (persistent across reboot)
#    On the z690 LXC modprobe may fail for dm modules if /lib/modules
#    for the host kernel is not mounted — warn, do not fail.
# ---------------------------------------------------------------
cat > /etc/modules-load.d/iscsi.conf << 'EOF'
iscsi_tcp
scsi_transport_iscsi
dm_multipath
dm_snapshot
EOF
for mod in iscsi_tcp scsi_transport_iscsi dm_multipath dm_snapshot; do
  modprobe "$mod" 2>/dev/null || echo "WARN: could not load module ${mod} (ok on LXC if host provides it)"
done

# ---------------------------------------------------------------
# 3. iscsid: enable + auto-login sessions
# ---------------------------------------------------------------
sed -i 's/^node.startup = manual/node.startup = automatic/' /etc/iscsi/iscsid.conf 2>/dev/null || true
systemctl unmask iscsid.service iscsid.socket 2>/dev/null || true
systemctl enable --now iscsid.socket 2>/dev/null || true
systemctl enable --now iscsid.service 2>/dev/null || true

# ---------------------------------------------------------------
# 4. multipathd: hard-disable + full blacklist (single-path cluster)
# ---------------------------------------------------------------
systemctl disable --now multipathd.service multipathd.socket 2>/dev/null || true
systemctl mask multipathd.service multipathd.socket 2>/dev/null || true
cat > /etc/multipath.conf << 'EOF'
# Single-path cluster (one iSCSI portal on TrueNAS). multipathd must never
# claim volumes: it holds unmounted devices O_EXCL, which breaks Longhorn
# and democratic-csi NodeStage ("already mounted or mount point busy").
# Verified incident 2026-10-18 on k3s-node1. Blacklist everything.
blacklist {
    devnode "^(sd|vd|nvme|hd|xvd)"
}
EOF

# ---------------------------------------------------------------
# 5. Kubelet mount propagation (rshared) — REQUIRED for CSI node plugins.
#    On k3s, /var/lib/kubelet is a symlink to /var/lib/rancher/k3s/agent/
#    kubelet; the propagation must be set on the REAL directory and must
#    persist across reboots via a systemd one-shot unit.
# ---------------------------------------------------------------
REAL_KUBELET="$(readlink -f /var/lib/kubelet 2>/dev/null || echo /var/lib/kubelet)"
[ -d "$REAL_KUBELET" ] || mkdir -p "$REAL_KUBELET"
echo "Kubelet dir (resolved): $REAL_KUBELET"

make_rshared() {
  local dir="$1"
  # Ensure it is a real mountpoint, then set rshared
  if ! findmnt -n -o TARGET "$dir" >/dev/null 2>&1; then
    mount --bind "$dir" "$dir" || { echo "WARN: bind-mount failed for $dir"; return 1; }
  fi
  local prop
  prop="$(findmnt -n -o PROPAGATION "$dir" | head -1)"
  echo "  current propagation: $prop"
  case "$prop" in
    *shared*) echo "  already rshared" ;;
    *) mount --make-rshared "$dir" && echo "  set rshared" ;;
  esac
}
make_rshared "$REAL_KUBELET" || true

# Persist across reboots (runs before k3s starts)
install_propagation_unit() {
  local unit="/etc/systemd/system/k3s-csi-propagation.service"
  if [ -f "$unit" ] && grep -q "$REAL_KUBELET" "$unit"; then
    echo "Propagation unit already present"
  else
    cat > "$unit" << EOF
[Unit]
Description=Make kubelet dir rshared for CSI mount propagation
DefaultDependencies=no
After=local-fs.target
Before=k3s.service k3s-agent.service
[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/bin/sh -c 'mount --bind ${REAL_KUBELET} ${REAL_KUBELET} 2>/dev/null; mount --make-rshared ${REAL_KUBELET}'
[Install]
WantedBy=multi-user.target
EOF
    systemctl daemon-reload
    systemctl enable k3s-csi-propagation.service >/dev/null 2>&1 || true
    echo "Installed + enabled $unit"
  fi
}
install_propagation_unit

# ---------------------------------------------------------------
# 6. Initiator IQN audit (for TrueNAS initiator group)
# ---------------------------------------------------------------
IQN="$(grep -v '^#' /etc/iscsi/initiatorname.iscsi 2>/dev/null | awk '{print $NF}' | head -1)"
echo "=== $(hostname) IQN: ${IQN} ==="

# ---------------------------------------------------------------
# 7. Summary
# ---------------------------------------------------------------
echo "--- iscsid:    $(systemctl is-active iscsid.service 2>/dev/null || systemctl is-active iscsid.socket 2>/dev/null || echo inactive)"
echo "--- multipathd: $(systemctl is-active multipathd.service 2>/dev/null || echo 'inactive (correct)')"
echo "--- kubelet dir propagation: $(findmnt -n -o PROPAGATION "$REAL_KUBELET" | head -1)"
echo "=== DONE. Node ready for democratic-csi. ==="
