# Architecture

This repository uses the same ownership model as the reference homelab, reduced for one physical
server:

1. Terraform owns the Proxmox VM and produces the Ansible inventory.
2. Ansible owns the guest OS, data-disk mount, and k3s installation.
3. Argo CD owns Kubernetes resources after one root Application is applied.
4. MetalLB owns stable LAN service addresses; the router owns DHCP and DNS.

## Topology

| VM | VMID | Node IP | Pool | Workloads |
| --- | ---: | --- | --- | --- |
| `k3s-home-01` | 230 | `10.0.0.10` | converged | Kubernetes, GitOps, media, Jellyfin, and Immich |

This is intentionally one converged VM. Three VMs on a 16 GB host would provide no physical
redundancy while wasting memory on three operating systems. It is not highly available: the one
NVMe, Proxmox host, VM, and k3s node are all failure domains.

The VM gets 10 GB RAM and 12 of the i7-14700's 28 logical CPUs. Proxmox retains roughly 5 GB RAM.
The VM uses a 64 GB OS disk plus a 600 GB data disk. Both are thin volumes on the same physical
NVMe; the separation is operational, not redundant.

## VPN boundary

The `media-stack` Deployment creates one pod containing Gluetun, Radarr, Sonarr, Prowlarr,
qBittorrent, FlareSolverr, Bazarr, Seerr, Maintainerr, and Homarr. Kubernetes containers in a pod
share a network namespace, so Gluetun changes the routes and firewall for all of them.

Gluetun is defined as a restartable init container (a native Kubernetes sidecar). Kubernetes waits
for its startup probe before launching the other containers. This prevents applications from
briefly using the normal pod route while the VPN is still connecting. The tradeoff is deliberate:
one container/PVC change recreates the whole media pod.

Jellyfin is a separate pod at `10.0.0.230:8096`; LAN streaming should not cross the VPN. Immich is
separate at `10.0.0.240`; its database and machine-learning lifecycle are independent.

## LAN allocation

Configure the router so DHCP ends at or below `10.0.0.199`. The repository reserves:

| Range | Owner |
| --- | --- |
| `10.0.0.10-10.0.0.19` | Static k3s nodes |
| `10.0.0.200-10.0.0.229` | MetalLB platform services |
| `10.0.0.230-10.0.0.250` | MetalLB media and photo services |

Never add a router DHCP reservation inside either MetalLB range.
