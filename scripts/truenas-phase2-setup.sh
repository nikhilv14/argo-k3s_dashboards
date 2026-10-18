#!/usr/bin/env bash
# truenas-phase2-setup.sh
# Idempotent TrueNAS SCALE (25.10) API configuration for democratic-csi.
# Run from the workstation holding the API key in ~/.truenas_key.
set -euo pipefail

TN="https://192.168.12.50/api/v2.0"
KEY=$(cat ~/.truenas_key)
CURL="curl -sk --max-time 15 -H \"Authorization: Bearer $KEY\" -H 'Content-Type: application/json'"

req() { # method path body
  eval "$CURL -X $1 \"$TN$2\" ${3:+--data '$3'}"
}

echo "=== 1. iSCSI Global (basename) ==="
req POST /iscsi/global '{"basename":"iqn.2026-10.lab.truenas:storage"}' || \
  echo "(may already be set — non-fatal)"

echo "=== 2. Portal 0.0.0.0:3260 ==="
req GET /iscsi/portal
# SCALE 25.10 requires listen as a list of ip dicts:
req POST /iscsi/portal '{"listen":[{"ip":"0.0.0.0"}],"comment":"k8s-iscsi"}'

echo "=== 3. Initiator group with node IQNs ==="
req POST /iscsi/initiator '{
  "initiators": [
    "iqn.2004-10.com.ubuntu:01:52c55542e014",
    "iqn.1993-08.org.debian:01:b72c457c6e94",
    "iqn.1993-08.org.debian:01:3c75a32e6e42",
    "iqn.1993-08.org.debian:01:a28566c8a824"
  ],
  "comment": "k8s-nodes"
}'

echo "=== 4. Enable + start iscsitarget service ==="
req POST /service/update '{"service":"iscsitarget","enable":true}'
req POST /service/start '{"service":"iscsitarget"}'

echo "=== 5. Verify service state ==="
req POST /service/query '[["service","=","iscsitarget"]]'

echo "=== 6. Verify port 3260 listening ==="
nc -zv 192.168.12.50 3260

echo "done."
