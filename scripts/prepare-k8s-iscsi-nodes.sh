#!/usr/bin/env bash
# prepare-k8s-iscsi-nodes.sh
# Idempotent per-node iSCSI/multipath prep for democratic-csi (TrueNAS iSCSI).
# Run on every k3s node: k3s-node1, k3s-server-nuc, k3s-server-hp-g9, k3s-worker-z690.
# Usage: sudo bash prepare-k8s-iscsi-nodes.sh
set -euo pipefail

# ---------------------------------------------------------------- packages ---
PKGS=(open-iscsi multipath-tools lsscsi util-linux xfsprogs e2fsprogs)
for p in "${PKGS[@]}"; do
  if ! dpkg -s "$p" >/dev/null 2>&1; then
    echo "[prep] installing $p"
    DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends "$p"
  else
    echo "[prep] $p already installed"
  fi
done

# ----------------------------------------------------------------- modules ---
# Load and persist the iSCSI/multipath kernel modules so they are present
# after every reboot.
MODFILE=/etc/modules-load.d/iscsi-csi.conf
mkdir -p /etc/modules-load.d
cat > "$MODFILE" <<'EOF'
iscsi_tcp
dm_multipath
dm_snapshot
EOF
for m in iscsi_tcp dm_multipath dm_snapshot; do
  modprobe "$m" || echo "[prep] WARNING: modprobe $m failed"
done

# ------------------------------------------------------------ iscsi daemon ---
systemctl unmask iscsid.service iscsid.socket multipathd.service 2>/dev/null || true
systemctl daemon-reload
systemctl enable --now iscsid.service 2>/dev/null || systemctl enable --now iscsid.socket
systemctl enable --now multipathd.service

# Ensure an initiator name exists (open-iscsi normally creates one at install).
if ! grep -q '^InitiatorName=' /etc/iscsi/initiatorname.iscsi 2>/dev/null; then
  echo "InitiatorName=$(iscsi-iname)" > /etc/iscsi/initiatorname.iscsi
  systemctl restart iscsid.service 2>/dev/null || systemctl restart iscsid.socket
fi

# ---------------------------------------------------- kubelet propagation ---
# CSI node plugins need the kubelet rootfs tree to be in a shared mount
# propagation so that bind-mounted block devices propagate into plugin
# containers. K3s kubelet lives at /var/lib/rancher/k3s/agent/kubelet.
KUBELET_DIR=/var/lib/rancher/k3s/agent/kubelet
mount --make-rshared /
mount --make-rshared "$KUBELET_DIR" 2>/dev/null || true

# Persist the propagation as a one-shot unit so it survives reboots (applied
# after local filesystems are up, before k3s starts).
cat > /etc/systemd/system/k3s-csi-propagation.service <<'EOF'
[Unit]
Description=Ensure rshared mount propagation for k3s kubelet (CSI)
DefaultDependencies=no
After=local-fs.target
Before=k3s.service
ConditionPathExists=/var/lib/rancher/k3s/agent/kubelet

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/bin/mount --make-rshared /

[Install]
WantedBy=multi-user.target
EOF
systemctl daemon-reload
systemctl enable k3s-csi-propagation.service

# ------------------------------------------------------------ verification ---
echo
echo "=== node: $(hostname) ==="
echo "initiator IQN: $(awk -F= '/^InitiatorName=/{print $2}' /etc/iscsi/initiatorname.iscsi)"
echo "iscsi service: $(systemctl is-active iscsid.service 2>/dev/null || systemctl is-active iscsid.socket)"
echo "multipathd:    $(systemctl is-active multipathd.service)"
echo "root mount:    $(findmnt -n -o PROPAGATION /)"
echo "sessions:      $(iscsiadm -m session 2>/dev/null | grep -c session || true)"
echo "[prep] done"
