# Pando85's Homelab

[![document](https://img.shields.io/website?label=document&logo=gitbook&logoColor=white&style=flat-square&url=https%3A%2F%2Fpando85.github.io%2Fhomelab%2F)](https://pando85.github.io/homelab/)
[![license](https://img.shields.io/github/license/pando85/homelab?style=flat-square&logo=gnu&logoColor=white)](https://www.gnu.org/licenses/gpl-3.0.html)

This project utilizes [Infrastructure as Code](https://en.wikipedia.org/wiki/Infrastructure_as_code)
and [GitOps](https://www.weave.works/technologies/gitops) to automate provisioning, operating, and
updating self-hosted services in my homelab. Based in [K3s](https://k3s.io/),
[ArgoCD](https://argo-cd.readthedocs.io/en/stable/),
[Renovate](https://github.com/renovatebot/renovate) and ZFS. It can be used as a highly customizable
framework to build your own homelab.

> **What is a homelab?**
>
> Homelab is a laboratory at home where you can self-host, experiment with new technologies,
> practice for certifications, and so on. For more information about homelab in general, see the
> [r/homelab introduction](https://www.reddit.com/r/homelab/wiki/introduction).

## 📖 Overview

This section provides a high level overview of the project. For further information, please see the
[documentation](https://pando85.github.io/homelab/).

## ⛵ Kubernetes

This repo is focused in maintain in a GitOps practical way my home infrastructure.
[Ansible](https://www.ansible.com/) is used to deploy a simple [K3s](https://k3s.io/) cluster.
Managed by [ArgoCD](https://argo-cd.readthedocs.io/en/stable/).

### Installation

The cluster is running on [Debian](https://www.debian.org/) based distributions, deployed on
bare-metal. We use custom Ansible playbooks and roles to setup the Kubernetes cluster.

### Core components

- [external-secrets](https://github.com/external-secrets/external-secrets): External Secrets
  Operator reads information from a Vault and automatically injects the values as Kubernetes
  Secrets.
- [hashicorp/vault](https://www.vaultproject.io): A tool for secrets management, encryption as a
  service, and privileged access management.
- [kubernetes-sigs/external-dns](https://github.com/kubernetes-sigs/external-dns): Automatically
  manages DNS records from my cluster in a cloud DNS provider.
- [jetstack/cert-manager](https://cert-manager.io/docs/): Creates SSL certificates for services in
  my Kubernetes cluster.
- [kubernetes/ingress-nginx](https://github.com/kubernetes/ingress-nginx/): Ingress controller to
  expose HTTP traffic to pods over DNS.
- [democratic-csi](https://github.com/democratic-csi/democratic-csi): implements the CSI (container
  storage interface) spec providing storage via zfs-based storage systems.
- [kanidm](https://kanidm.com/): A simple, secure and fast identity management platform.

## 🔧 Hardware

| Hostname         | Device                              | Count | OS Disk Size | Data Disk Size                      | Ram  | Operating System | Purpose    |
| ---------------- | ----------------------------------- | ----- | ------------ | ----------------------------------- | ---- | ---------------- | ---------- |
| grigri           | Supermicro Atom C2758 (A1SRi-2758F) | 1     | 250GB SSD    | 3\*4TB + 500GB (NVMe) RAIDZ + cache | 32GB | Ubuntu 22.04     | K3s server |
| k8s-amd64-1\*    | AMD E-450 APU                       | 1     | 60GB         | N/A                                 | 8GB  | Ubuntu 22.04     | k3s agent  |
| k8s-rock64-i     | Rock64                              | 6     | N/A          | N/A                                 | 4GB  | Armbian          | K3s agent  |
| k8s-odroid-c4-i  | Odroid-c4                           | 2     | N/A          | N/A                                 | 4GB  | Armbian          | K3s agent  |
| k8s-odroid-hc4-i | Odroid-hc4                          | 1     | N/A          | 3TB + 240GB SSD                     | 4GB  | Armbian          | K3s agent  |
| pfsense          | PC Engines APU2e4                   | 1     | 60GB         | N/A                                 | 4GB  | pfSense/FreeBSD  | Router     |
| gs724t           | Netgear gs724t                      | 1     | N/A          | N/A                                 | N/A  | N/A              | Switch     |
| cerezo           | Unifi UAP                           | 1     | N/A          | N/A                                 | N/A  | N/A              | AP         |
| manzano          | Unifi UAP                           | 1     | N/A          | N/A                                 | N/A  | N/A              | AP         |

\* with graphic card connected: Nvidia GeForce GTX 1060 3GB

### Images

<img src="https://raw.githubusercontent.com/pando85/homelab/master/docs/images/rack.jpg" width="50%" height="auto" />
<img src="https://raw.githubusercontent.com/pando85/homelab/master/docs/images/k8s-amd64-1.jpg" width="50%" height="auto" />

## ⭐ Features

- [x] Common applications: Jellyfin, Gitea, arr, Nextcloud...
- [x] Automated Kubernetes installation and management
- [x] Installing and managing applications using GitOps
- [x] Automatic rolling upgrade for OS and Kubernetes
- [x] Automatically update apps (with approval if needed)
- [x] Modular architecture, easy to add or remove features/components
- [x] Automated certificate management
- [x] Automatically update DNS records for exposed services
- [x] Monitoring and alerting
- [x] Single sign-on
- [x] Automated backups

## 🌐 DNS

[ExternalDNS](https://github.com/kubernetes-sigs/external-dns) is deployed in the cluster and
configured to sync DNS records to [Cloudflare](https://www.cloudflare.com/).

All connections outside the cluster are handled with TLS using
[cert-manager](https://cert-manager.io/) with [Let's Encrypt](https://letsencrypt.org/).

### Load Balancer

[MetalLB](https://metallb.universe.tf/) is configured in BGP mode, both on my router and within the
Kubernetes cluster.

### Ingress Controllers

For external access, port forwarding is configured for ports `80` and `443`, directing traffic to
the load balancer IP of the Kubernetes ingress controller.

There are also another ingress controller for internal use.

### Internal DNS

`internal.grigri.cloud` domain is used. Configured as:

```yaml
annotations:
  cert-manager.io/cluster-issuer: letsencrypt-prod-dns
  external-dns.alpha.kubernetes.io/enabled: "true"
```

### External DNS

`grigri.cloud` domain is used. Configured as:

```yaml
annotations:
  cert-manager.io/cluster-issuer: letsencrypt-prod-dns
  external-dns.alpha.kubernetes.io/enabled: "true"
  external-dns.alpha.kubernetes.io/target: grigri.cloud
```

## 🤝 Thanks

Thanks to all folks who donate their time to the [Kubernetes @Home](https://github.com/k8s-at-home/)
community. A lot of inspiration for my cluster came from those that have shared their clusters over
at [awesome-home-kubernetes](https://github.com/k8s-at-home/awesome-home-kubernetes).
