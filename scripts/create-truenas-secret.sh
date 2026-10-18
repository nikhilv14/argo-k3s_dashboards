#!/usr/bin/env bash
# create-truenas-secret.sh
#
# Builds the democratic-csi driver config (freenas-api-iscsi) from a TrueNAS
# API key supplied interactively (or via env var TRUENAS_API_KEY) and creates
# the Kubernetes secret that the helm chart consumes via existingConfigSecret.
#
# The API key never touches disk in the git repo. For the GitOps path, seal
# the output with kubeseal (see infra/democratic-csi/manifests/sealed-secret.yaml):
#   kubeseal --scope namespace-wide < secret.yaml
#
# Usage:
#   TRUENAS_API_KEY=1-abc... ./create-truenas-secret.sh
#   # or run bare and paste when prompted
set -euo pipefail

NAMESPACE="democratic-csi"
SECRET_NAME="democratic-csi"          # must match existingConfigSecret in values-iscsi.yaml
SECRET_KEY="driver-config-file.yaml"  # must match the chart's expected key
TRUENAS_HOST="192.168.12.50"
TRUENAS_PORT="80"
TRUENAS_PROTO="http"
PARENT_DATASET="Generic_1TB/k8s-iscsi"
TARGET_PREFIX="k8s-iscsi-vol-"

if [[ -z "${TRUENAS_API_KEY:-}" ]]; then
  read -rsp "Enter TrueNAS API key: " TRUENAS_API_KEY
  echo
fi

[[ -n "$TRUENAS_API_KEY" ]] || { echo "ERROR: empty API key"; exit 1; }

kubectl create namespace "$NAMESPACE" --dry-run=client -o yaml | kubectl apply -f -

TMP_CONFIG="$(mktemp)"
trap 'rm -f "$TMP_CONFIG"' EXIT

cat > "$TMP_CONFIG" <<EOF
driver: freenas-api-iscsi

httpConnection:
  protocol: ${TRUENAS_PROTO}
  host: ${TRUENAS_HOST}
  port: ${TRUENAS_PORT}
  apiKey: ${TRUENAS_API_KEY}
  allowInsecure: false
  apiVersion: 2

zfs:
  datasetParentName: ${PARENT_DATASET}
  datasetEnableQuotas: true
  datasetEnableReservation: false
  datasetPermissionsMode: "0770"
  datasetPermissionsUser: "0"
  datasetPermissionsGroup: "0"

  targetPrefix: ${TARGET_PREFIX}
  targetGroupName: k8s-targets
  initiatorGroupName: k8s-initiators
  zvolEnableSparse: true
  zvolBlockSize: "16k"
EOF

# validate YAML parses
if command -v python3 >/dev/null; then
  python3 -c "import yaml,sys; yaml.safe_load(open('$TMP_CONFIG'))" 2>/dev/null \
    || { echo "ERROR: generated config is not valid YAML"; exit 1; }
fi

kubectl create secret generic "$SECRET_NAME" \
  -n "$NAMESPACE" \
  --from-file="$SECRET_KEY"="$TMP_CONFIG" \
  --dry-run=client -o yaml | kubectl apply -f -

echo "Secret '${SECRET_NAME}' (key ${SECRET_KEY}) created/updated in namespace '${NAMESPACE}'."
echo "NOTE: the deployed secret is a SealedSecret (infra/democratic-csi/manifests/)."
echo "      For GitOps, seal this:  kubeseal --scope namespace-wide < secret.yaml"
