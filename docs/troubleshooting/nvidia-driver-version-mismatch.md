# NVIDIA Driver/Library Version Mismatch

## Problem

The `nvidia-device-plugin` pod crash-loops with hundreds of restarts. The container fails to start
with:

```
failed to initialize NVML: Driver/library version mismatch
```

GPU workloads cannot be scheduled until the mismatch is resolved.

## Root Cause

Two driver management systems competing on the same host:

1. **GPU Operator** manages NVIDIA drivers as a containerized DaemonSet with its own upgrade
   controller state machine (cordon → evict GPU pods → restart driver pod → validate → uncordon).

2. **Unattended-upgrades** (Debian/Ubuntu `apt`) can automatically upgrade host-level NVIDIA
   packages (`nvidia-headless-*`, `libnvidia-*`, `cuda-*`) outside the operator's control.

When unattended-upgrades bumps the userspace library while the kernel module remains from the
previous version (or vice versa), NVML initialization fails with a version mismatch. The device
plugin crash-loops until a node reboot happens to realign both components.

This only manifests on GPU nodes where both the Ansible `nvidia-container-runtime` role installs
host NVIDIA packages **and** the GPU Operator deploys its own driver container.

## How to Diagnose

```bash
# Check device plugin restarts and last failure reason
kubectl --context=grigri describe pod -n gpu-operator -l app=nvidia-device-plugin-daemonset | grep -A5 "Last State"

# Check if driver/library versions differ on the host
ssh prusik "cat /proc/driver/nvidia/version"
ssh prusik "dpkg -l | grep nvidia | head -20"

# Check unattended-upgrades log for recent NVIDIA package upgrades
ssh prusik "grep -i nvidia /var/log/unattended-upgrades/unattended-upgrades.log"
```

## Fix

### 1. Blacklist NVIDIA packages from unattended-upgrades

The `50unattended-upgrades` apt config must include NVIDIA packages in the blacklist:

```
Unattended-Upgrade::Package-Blacklist {
    "nvidia-.*";
    "libnvidia-.*";
    "cuda-.*";
    "xserver-xorg-video-nvidia-.*";
};
```

Deploy the updated config via Ansible (note: the tag lives in the `prepare` role, not `cluster`):

```bash
cd metal && ANSIBLE_EXTRA_ARGS="-t unattended-upgrades" make prepare
```

### 2. Driver upgrades should only go through the GPU Operator

Use the operator's upgrade controller to roll out new driver versions:

```bash
kubectl patch clusterpolicies.nvidia.com/cluster-policy \
    --type='json' \
    -p='[{"op": "replace", "path": "/spec/driver/version", "value":"<new-version>"}]'
```

Monitor per-node progress:

```bash
kubectl get node -l nvidia.com/gpu.present \
    -ojsonpath='{range .items[*]}{.metadata.name}{"\t"}{.metadata.labels.nvidia\.com/gpu-driver-upgrade-state}{"\n"}{end}'
```

See [NVIDIA GPU Operator - GPU Driver Upgrades](https://docs.nvidia.com/datacenter/cloud-native/gpu-operator/latest/gpu-driver-upgrades.html)
for the full upgrade state machine and configuration options.
