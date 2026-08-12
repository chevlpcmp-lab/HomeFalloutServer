# Architecture

This repository uses the same ownership model as the reference homelab, reduced for one physical
server:

1. Terraform owns the Proxmox template and three full-clone VMs and produces the Ansible inventory.
2. Ansible owns the guest OS, data-disk mount, and k3s installation.
3. Argo CD owns Kubernetes resources after one root Application is applied.
4. MetalLB owns stable LAN service addresses; the router owns DHCP and DNS.

## GitOps component layout

The platform chart follows the reference repository's component model. Each entry in
`platform/values/values-prod.yaml` can produce up to three Argo CD Applications:

| Phase | Directory/source | Relative sync wave |
| --- | --- | ---: |
| prerequisites | `pre-resources/` | tier - 1 |
| upstream Helm chart | chart plus `values/chart-prod.yaml` | tier |
| owned manifests | `resources/` | tier + 1 |

This lab currently needs chart-plus-resources for MetalLB, a chart for Sealed Secrets, and
resources-only components for the local storage and applications. Encrypted application secrets
are stored with their owning component. Plaintext inputs and the Sealed Secrets recovery key are
kept under the gitignored `platform/secrets/` directory.

## Topology

| VM | VMID | Node IP | Pool | Workloads |
| --- | ---: | --- | --- | --- |
| `k3s-cp-01` | 220 | `10.0.0.10` | control | k3s API, scheduler, controller, and etcd |
| `k3s-apps-01` | 240 | `10.0.0.11` | apps | Homarr and general workloads |
| `k3s-media-01` | 230 | `10.0.0.12` | media | VPN media pod, Jellyfin, and Immich |

Terraform first creates VMID 9000, `ubuntu-2404-cloudinit-template`, from Ubuntu's cloud image.
Every node is a full clone of that template. The nodes receive 4/8/16 vCPUs and 2/3/7 GB RAM,
respectively, using all 28 logical CPUs while leaving roughly 3 GB RAM for Proxmox. The control
plane is tainted so application pods run on the workers.

This is scheduling isolation, not high availability: all VMs, virtual disks, and Kubernetes nodes
still share one Proxmox host and one NVMe. The single control plane is acceptable for this resource
budget but is itself a cluster control-plane failure domain.

## VPN boundary

The `media-stack` Deployment creates one pod containing Gluetun, Radarr, Sonarr, Prowlarr,
qBittorrent, FlareSolverr, Bazarr, Seerr, and Maintainerr. Kubernetes containers in a pod
share a network namespace, so Gluetun changes the routes and firewall for all of them.

Gluetun is defined as a restartable init container (a native Kubernetes sidecar). Kubernetes waits
for its startup probe before launching the other containers. This prevents applications from
briefly using the normal pod route while the VPN is still connecting. The tradeoff is deliberate:
one container/PVC change recreates the whole media pod.

Homarr is a separate component and pod at `10.0.0.220`; its dashboard traffic does not need the
VPN. Jellyfin is separate at `10.0.0.230:8096`; LAN streaming should not cross the VPN. Immich is
separate at `10.0.0.240`; its database and machine-learning lifecycle are independent.

## LAN allocation

Configure the router so DHCP ends at or below `10.0.0.199`. The repository reserves:

| Range | Owner |
| --- | --- |
| `10.0.0.10-10.0.0.19` | Static k3s nodes |
| `10.0.0.200-10.0.0.229` | MetalLB platform services |
| `10.0.0.230-10.0.0.250` | MetalLB media and photo services |

Never add a router DHCP reservation inside either MetalLB range.
