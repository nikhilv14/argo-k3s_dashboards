#!/usr/bin/env bash
# Re-seal the PVE exporter API token into infra/pve-exporter/sealed-secret.yaml.
#
# Usage:  ./scripts/reseal-pve-token.sh <NEW_PVE_TOKEN_VALUE>
#
# The token comes from Proxmox: Datacenter -> Permissions -> API Tokens ->
# user nikhil@pve, token grafana-config, token separation ON, no expiration
# date. Rotate it whenever Proxmox rejects the exporter with 401.
set -euo pipefail
cd "$(dirname "$0")/.."

TOKEN=${1:?usage: $0 <NEW_PVE_TOKEN_VALUE>}
FILE=infra/pve-exporter/sealed-secret.yaml

KUBESEAL=${KUBESEAL:-$(command -v kubeseal || true)}
if [ -z "$KUBESEAL" ] && [ -x "$HOME/.local/bin/kubeseal" ]; then
  KUBESEAL=$HOME/.local/bin/kubeseal
fi
if [ -z "$KUBESEAL" ] || [ ! -x "$KUBESEAL" ]; then
  echo "kubeseal not found; install to ~/.local/bin or set KUBESEAL=/path/to/kubeseal" >&2
  exit 1
fi

TMP=$(mktemp)
trap 'rm -f "$TMP"' EXIT
kubectl create secret generic pve-exporter -n monitoring \
  --from-literal=PVE_TOKEN_VALUE="$TOKEN" \
  --dry-run=client -o yaml > "$TMP"

$KUBESEAL --controller-name sealed-secrets --controller-namespace kube-system \
  --format yaml < "$TMP" > "$FILE"

echo "Sealed $FILE. Push to git; ArgoCD will sync and the exporter picks up"
echo "the new token on its next /pve scrape (no pod restart needed)."
