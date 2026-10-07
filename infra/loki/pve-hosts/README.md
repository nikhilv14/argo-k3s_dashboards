# Promtail on the Proxmox VE hosts

The k3s logs reach Loki through promtail inside the cluster, but the PVE
hosts (pve, pve-nuc10, pve-hp-g9) are outside it. This directory contains a
small standalone promtail deployment for those hosts: journald (pveproxy,
pvedaemon, pvestatd, kernel, sshd), PVE task history (/var/log/pve/tasks) and
/var/log/syslog are shipped into the same Loki the k3s logs live in.

One-time per host, on each PVE node (repeat for pve-nuc10 and pve-hp-g9):

    # 1. Loki ingress (done once from the k3s side, see below)
    # 2. Fetch the binary
    wget -qO /usr/local/bin/promtail https://github.com/grafana/loki/releases/download/v3.5.1/promtail-linux-amd64.zip
    unzip -o /usr/local/bin/promtail -d /tmp && mv /tmp/promtail-linux-amd64 /usr/local/bin/promtail && chmod +x /usr/local/bin/promtail
    rm /usr/local/bin/promtail.zip 2>/dev/null || true

    # 3. Config + systemd unit (copy from this repo)
    mkdir -p /etc/promtail /var/lib/promtail
    scp <workstation>:/mnt/workspace/repos/k3s-apps/argo-k3s_dashboards/infra/loki/pve-hosts/{promtail-config.yaml,promtail.service} root@192.168.12.201:/tmp/
    mv /tmp/promtail-config.yaml /etc/promtail/promtail.yaml
    mv /tmp/promtail.service /etc/systemd/system/promtail.service
    systemctl daemon-reload && systemctl enable --now promtail

Verify on the host:

    systemctl status promtail
    curl -s localhost:3101/metrics | grep promtail_send

Then look for `job="systemd-journal"` / `job="pve-tasks"` streams in Grafana
Explore (Loki datasource).

## Loki NodePort (k3s side, once)

The PVE hosts push through NodePort 30100, created by
`infra/loki/manifests/loki-nodeport.yaml` (Git-managed via ArgoCD):

    kubectl apply -f infra/loki/manifests/loki-nodeport.yaml

The client URL in promtail-config.yaml targets k3s-node1 (192.168.12.51);
that node is a plain agent (not a control-plane node) so it keeps receiving
traffic even during control-plane maintenance. If you retire that IP, point
the URL at another k3s node IP (192.168.12.110 / 192.168.12.132).
