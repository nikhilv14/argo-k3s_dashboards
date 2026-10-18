# Longhorn Graceful Drain & Complete Decommissioning Runbook

> **Execute ONLY after** the 7-day data-migration soak period AND after
> verifying **zero** Longhorn-backed PVCs remain in the cluster.
> This runbook assumes all data has been migrated to TrueNAS iSCSI
> (StorageClass `truenas-iscsi`) and `nas-rwx` (NFS) is the only other
> storage backend in use.

## Phase 3.0 — Preconditions

1. Verify no Longhorn PVCs remain in the cluster:
   ```bash
   kubectl get pvc -A -o json | \
     jq -r '.items[] | select(.spec.storageClassName | test("longhorn|ssd-cache")) | "\(.metadata.namespace)/\(.metadata.name)"'
   ```
   This must output **nothing**. If anything is listed, migrate it first
   (see Phase 4 of the migration plan).

2. Verify no Longhorn-backed PVs remain:
   ```bash
   kubectl get pv -o json | \
     jq -r '.items[] | select(.spec.csi.driver == "driver.longhorn.io") | .metadata.name'
   ```
   Also must output nothing.

3. Verify `democratic-csi` is healthy and the `truenas-iscsi` StorageClass
   has served at least one volume successfully (Phase 5 validation).

4. Take a final Longhorn backup (belt-and-braces):
   ```bash
   kubectl -n longhorn-system get volumes.longhorn.io -o name | xargs -I{} \
     kubectl -n longhorn-system exec deployment/longhorn-ui -- \
       longhorn backup create {} # manual step, or use the Longhorn UI
   ```

## Phase 3.1 — Drain & stop Longhorn workloads

```bash
# Scale down the Longhorn UI and driver deployer (they block driver removal).
kubectl -n longhorn-system scale deployment/longhorn-driver-deployer --replicas=0
kubectl -n longhorn-system scale deployment/longhorn-ui --replicas=0

# Wait for all CSI-attach sessions to drain (may take a few minutes).
kubectl -n longhorn-system get pods
```

## Phase 3.2 — Remove the ArgoCD apps (git-first)

This is the **critical step** — ArgoCD has `prune: true` + `selfHeal: true`,
so if you just `kubectl delete` without removing from git, ArgoCD will
re-create everything on the next sync.

1. Remove the `longhorn` and `longhorn-dashboard` Application entries from
   `argocd/apps-infra.yaml` (and any app-of-apps entry pointing at them).
2. Commit and push to `main`. Wait for ArgoCD to sync the `root` app — the
   `longhorn` and `longhorn-dashboard` Applications will be **pruned**.
3. Once ArgoCD has pruned them, the Helm release will be uninstalled but the
   CRDs and webhook leftovers will remain. Clean those up in Phase 3.3.

## Phase 3.3 — Strip finalizers and delete leftovers

Longhorn CRs often hang on finalizers after the manager is gone.

```bash
# Scale down the longhorn-manager daemonset (if it still exists).
kubectl -n longhorn-system scale daemonset/longhorn-manager --replicas=0 2>/dev/null || true

# Remove finalizers from all custom resources.
for cr in \
  volumes.longhorn.io \
  nodes.longhorn.io \
  instancemanagers.longhorn.io \
  engines.longhorn.io \
  replicas.longhorn.io \
  settings.longhorn.io \
  backups.longhorn.io \
  backingimages.longhorn.io \
  recurringjobs.longhorn.io \
  backups.longhorn.io; do
  kubectl -n longhorn-system get "$cr" -o name 2>/dev/null | xargs -r -I{} \
    kubectl -n longhorn-system patch {} --type=merge \
      -p '{"metadata":{"finalizers":null}}' 2>/dev/null || true
done

# Delete the namespace.
kubectl delete namespace longhorn-system --grace-period=120 --force 2>/dev/null || true

# Delete Longhorn CRDs (force-remove stuck ones).
for crd in $(kubectl get crd -o name 2>/dev/null | grep longhorn.io); do
  kubectl delete "$crd" --grace-period=0 --force 2>/dev/null || true
done

# Remove the Longhorn admission webhook.
kubectl get validatingwebhookconfigurations -o name 2>/dev/null | \
  grep -i longhorn | xargs -r kubectl delete
kubectl get mutatingwebhookconfigurations -o name 2>/dev/null | \
  grep -i longhorn | xargs -r kubectl delete

# Delete the StorageClasses.
kubectl delete storageclass longhorn longhorn-static ssd-cache 2>/dev/null || true
```

## Phase 3.4 — Final verification

```bash
# No Longhorn CRDs left.
kubectl get crd | grep -c longhorn   # must output 0

# No Longhorn resources left.
kubectl get all -n longhorn-system   # must output "No resources found"

# No Longhorn PVs.
kubectl get pv | grep -c longhorn    # must output 0

# democratic-csi still healthy.
kubectl get pods -n democratic-csi
```

## Phase 3.5 — Clean up the repo

1. Remove `infra/longhorn/` and `longhorn-dashboard/` directories from the
   git repo (after ArgoCD has fully pruned the apps).
2. Remove `storageClassName: longhorn` / `ssd-cache` references from any
   remaining manifests (should be none if the migration was complete).
3. Commit and push. Done.

## Rollback

If anything goes wrong during decommissioning and you need to roll back:
- Longhorn data is still on disk (the Longhorn-managed zvol/dataset on the
  node local-storage is untouched until you wipe it).
- Re-install Longhorn via `helm install longhorn longhorn/longhorn -n
  longhorn-system --create-namespace` (or restore the ArgoCD app from git
  history with `git revert <commit>`).
- Restore any PVCs from the Longhorn backups you took in Phase 3.0.
