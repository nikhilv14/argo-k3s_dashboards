#!/usr/bin/env bash
# prepare-k8s-iscsi-nodes.sh
# Phase 1: Idempotent node preparation for democratic-csi (TrueNAS iSCSI).
# Run on every k3s node: k3s-node1, k3s-server-nuc, k3s-server-hp-g9, k3s-worker-z690
# Requires root.
set -euo pipefail

echo "=== Node prep for iSCSI (democratic-csi) on $(hostname) ==="

# 1. Install required packages
if command -v apt-get >/dev/null 2>&1; then
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -qq
  apt-get install -y -qq open-iscsi multipath-tools lsscsi
elif command -v dnf >/dev/null 2>&1; then
  dnf install -y iscsi-initiator-utils device-mapper-multipath lsscsi
else
  echo "Unsupported package manager (apt/dnf expected)." >&2; exit 1
fi

# 2. Enable and start iSCSI + multipath services
systemctl enable --now iscsid.socket multipathd.socket 2>/dev/null || true
systemctl enable --now iscsid.service multipathd.service 2>/dev/null || true

# 3. Load kernel modules (persistent across reboot)
cat > /etc/modules-load.d/iscsi.conf << 'EOF'
iscsi_tcp
scsi_transport_iscsi
dm_multipath
EOF
for mod in iscsi_tcp scsi_transport_iscsi dm_multipath; do
  modprobe "$mod" 2>/dev/null || echo "WARN: could not load module ${mod}"
done

# 4. Configure iscsid to auto-start sessions
sed -i 's/^node.startup = manual/node.startup = automatic/' /etc/iscsi/iscsid.conf || true

# 5. Multipath config (enables passthrough for non-multipath disks)
cat > /etc/multipath.conf << 'EOF'
defaults {
    user_friendly_names yes
    find_multipaths yes
}
blacklist {
    # local system disks (nvme/sda/sdb etc are usually blacklisted by find_multipaths)
}
EOF
systemctl restart multipathd 2>/dev/null || true

# 6. Prepare kubelet mount propagation (required for CSI Node plugin)
# k3s usually runs with default mount propagation, but for LXC (z690) we must
# ensure the host mount is shared.
if [ -d /var/lib/rancher/k3s ]; then
    echo "K3s detected: ensuring /var/lib/rancher/k3s/agent/kubelet has rshared propagation"
    mount --bind /var/lib/rancher/k3s/agent/kubelet /var/lib/rancher/k3s/agent/kubelet || true
    mount --make-rshared /var/lib/rancher/k3s/agent/kubelet || true
fi

# 7. Print node IQN for TrueNAS initiator group
IQN=$(cat /etc/iscsi/initiatorname.iscsi 2>/dev/null | grep -v '^#' | awk '{print $NF}')
echo "=== ${HOSTNAME} IQN: ${IQN} ==="
echo "=== DONE. Node ready for democratic-csi. ==="
