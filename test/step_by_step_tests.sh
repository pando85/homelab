#!/bin/bash
# This script is just for testing step by step

k3d cluster create --config test/cluster.yaml

cat <<EOF | kubectl apply -f -
apiVersion: storage.k8s.io/v1
kind: StorageClass
metadata:
  name: longhorn
provisioner: rancher.io/local-path
reclaimPolicy: Delete
volumeBindingMode: WaitForFirstConsumer
EOF

scripts/deploy-dir.rh system/monitoring
scripts/deploy-dir.rh system/monitoring
scripts/deploy-dir.rh platform/vault
scripts/deploy-dir.rh platform/external-secrets
scripts/deploy-dir.rh platform/vault

echo "waiting for external-secrets webhook ready"
kubectl -n external-secrets wait --for=condition=ready pod \
  -l app.kubernetes.io/name=external-secrets-webhook \
  --timeout=300s

scripts/deploy-dir.rh platform/external-secrets
scripts/deploy-dir.rh system/monitoring

kubectl -n monitoring delete pod/monitoring-grafana-test

scripts/deploy-dir.rh system/cert-manager
scripts/deploy-dir.rh platform/gitea

scripts/deploy-dir.rh system/ingress-nginx

echo "waiting for cert-manager webhook ready"
kubectl -n cert-manager wait --for=condition=ready pod \
  -l app.kubernetes.io/instance=cert-manager \
  -l app.kubernetes.io/name=webhook \
  --timeout=300s

scripts/deploy-dir.rh system/cert-manager
# secrets to create:
#   - alertmanager-telegram-forwarder
#   - kubernetes-internal-ca-key-pair
k3d cluster delete homelab-dev
