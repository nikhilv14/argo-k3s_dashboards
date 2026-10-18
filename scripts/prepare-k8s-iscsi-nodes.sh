#!/usr/bin/env bash
# idempotent node iSCSI preparation for democratic-csi (TrueNAS backend)
set -euo pipefail

NODE_NAME=$(hostname)
echo "=== preparing iSCSI prerequisites on ${NODE_NAME} ==="

# 1. Packages
export DEBIAN_FRONTEND=noninteractive
sudo apt-get update -qq
sudo apt-get install -y -qq open-iscsi multipath-tools lsscsi util-linux xfsprogs e2fsprogs >/dev/null

# 2. Kernel modules (persistent)
sudo tee /etc/modules-load.d/iscsi.conf >/dev/null <<'EOF'
iscsi_tcp
dm_multipath
dm_snapshot
EOF
for mod in iscsi_tcp dm_multipath dm_snapshot; do
  sudo modprobe "$mod" 2>/dev/null || echo "WARN: modprobe ${mod} failed"
done

# 3. Services
sudo systemctl unmask iscsid.service iscsid.socket multipathd.service 2>/dev/null || true
sudo systemctl enable --now iscsid.service iscsid.socket multipathd.service 2>/dev/null || true

# 4. Mount propagation: /var/lib/kubelet must be rshared (or / itself)
if ! sudo findmnt -o PROPAGATION --target /var/lib/kubelet 2>/dev/null | grep -q shared; then
  sudo mkdir -p /var/lib/kubelet
  sudo mount --bind /var/lib/kubelet /var/lib/kubelet
  sudo mount --make-rshared /var/lib/kubelet
fi

# persistent one-shot unit for propagation across reboots
sudo tee /etc/systemd/system/kubelet-rshared.service >/dev/null <<'EOF'
[Unit]
Description=Make /var/lib/kubelet rshared for CSI mount propagation
DefaultDependencies=no
Before=kubelet.service

[Service]
Type=oneshot
ExecStartPre=/bin/mkdir -p /var/lib/kubelet
ExecStart=/bin/mount --bind /var/lib/kubelet /var/lib/kubelet
ExecStart=/bin/mount --make-rshared /var/lib/kubelet
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
EOF
sudo systemctl daemon-reload
sudo systemctl enable kubelet-rshared.service >/dev/null 2>&1 || true
sudo systemctl start kubelet-rshared.service

# 5. IQN audit
IQN=$(cat /etc/iscsi/initiatorname.iscsi | grep -v '^#' | awk '{print $NF}')
echo "=== ${NODE_NAME} IQN: ${IQN} ==="
echo "=== DONE: ${NODE_NAME} iSCSI prep complete ==="
