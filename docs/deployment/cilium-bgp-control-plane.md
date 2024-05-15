# Cilium BPG control plane

Cilium replaces MetalLB for creating K8s LBs based on BGP.

## Deployment

Ansible set up the configuration and ArgoCD deploys Cilium.

## Router config

In order to use Cilium BGP control plane mode we must configure Pfsense as router to be able of share
BGP route table and route all network to that load balancer IPs. We use
[this tutorial](https://www.danmanners.com/posts/pfsense-bgp-kubernetes/)

- install package ffr

- configure `Services->FRR->Global Settings`:

  ```conf
  [general options]
  enable=x
  default_router_id=192.168.192.1
  ```

- `Services->FRR->Global Settings->Route Maps`:

  ```yaml
  - name: allow-all
    description: Match any route
    action: permit
    Sequence: 100
  ```

- `Services->FRR->BGP->BGP`:

  ```conf
  [bgp router options]
  enable=x
  local_as=64512

  [graceful restart/shutdown]
  enable_bgp_graceful_shutdown=true
  ```

- in `Services->FRR->BGP->Neighbors`:

  ```yaml
  - name: 192.168.192.2
    descr: grigri
    remote_as: 64513
    next_hop_self: true
    route_map_filters:
      inbound_router_map_filter: allow-all
      outbound_router_map_filter: allow-all
    allow_as_inbound: enabled
  - name: 192.168.192.3
    descr: prusik
    remote_as: 64513
    next_hop_self: true
    route_map_filters:
      inbound_router_map_filter: allow-all
      outbound_router_map_filter: allow-all
    allow_as_inbound: enabled
  - name: 192.168.192.23
    descr: k8s-odroid-hc4-3
    remote_as: 64513
    next_hop_self: true
    route_map_filters:
      inbound_router_map_filter: allow-all
      outbound_router_map_filter: allow-all
    allow_as_inbound: enabled
  ```

**Important:** to access Cilium IP pools network from kubernetes subnet you need to add your host to bgp
Some issues could be experimented if not added as `docker push` not working correctly.
