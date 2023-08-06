# Pando85's Homelab

[![document](https://img.shields.io/website?label=document&logo=gitbook&logoColor=white&style=flat-square&url=https%3A%2F%2Fhomelab.pando85.com)](https://pando85.github.io/homelab/)
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

## Overview

This section provides a high level overview of the project. For further information, please see the
[documentation](https://pando85.github.io/homelab/).

### 🔧 Hardware

| Device                              | Count | OS Disk Size | Data Disk Size                      | Ram  | Operating System | Purpose    |
| ----------------------------------- | ----- | ------------ | ----------------------------------- | ---- | ---------------- | ---------- |
| AMD E-450 APU                       | 1     | 60GB         | N/A                                 | 8GB  | Ubuntu 22.04     | k3s server |
| Supermicro Atom C2758 (A1SRi-2758F) | 1     | 250GB SSD    | 3\*4TB + 500GB (NVMe) RAIDZ + cache | 32GB | Centos 7         | K3s agent  |
| Rock64                              | 6     | N/A          | N/A                                 | 4GB  | Armbian          | K3s agent  |
| Odroid-c4                           | 2     | N/A          | N/A                                 | 4GB  | Armbian          | K3s agent  |
| Odroid-hc4                          | 3     | N/A          | 3TB + 240GB SSD                     | 4GB  | Armbian          | K3s agent  |
| PC Engines APU2e4                   | 1     | N/A          | N/A                                 | N/A  | N/A              | Router     |
| RTL8370N                            | 1     | N/A          | N/A                                 | N/A  | N/A              | Switch     |
| Netgear gs724t                      | 1     | N/A          | N/A                                 | N/A  | N/A              | Switch     |

### Features

- [x] Common applications: Gitea...
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
- [ ] Automated offsite backups 🚧

### 🌐 DNS

#### Load Balancer

[MetalLB](https://metallb.universe.tf/) is configured in BGP mode, both on my router and within the Kubernetes cluster.

#### Ingress Controllers

For external access, port forwarding is configured for ports `80` and `443`, directing traffic to
the load balancer IP of the Kubernetes ingress controller.

There are also another ingress controller for internal use.

#### Internal DNS

To handle internal requests, DNS configuration redirects all subdomains under `k8s.grigri` to the internal ingress controller.

#### External DNS

[ExternalDNS](https://github.com/kubernetes-sigs/external-dns) is deployed in the cluster and
configured to sync DNS records to [Cloudflare](https://www.cloudflare.com/).

## 🤝 Thanks

Thanks to all folks who donate their time to the [Kubernetes @Home](https://github.com/k8s-at-home/)
community. A lot of inspiration for my cluster came from those that have shared their clusters over
at [awesome-home-kubernetes](https://github.com/k8s-at-home/awesome-home-kubernetes).
