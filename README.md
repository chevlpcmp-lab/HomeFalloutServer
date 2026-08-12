# HomeFalloutServer

Infrastructure-as-code for a Proxmox/k3s media homelab on `10.0.0.0/24`. Terraform creates the VM,
Ansible prepares its data disk and installs k3s, and Argo CD continuously reconciles Immich,
Sonarr, Radarr, Maintainerr, Homarr, qBittorrent, Gluetun, Prowlarr, FlareSolverr, Bazarr, Seerr,
and Jellyfin.

The GitOps layout follows the reference repository's component contract:

```text
platform/components/<name>/
  pre-resources/   # optional prerequisites, applied before an upstream chart
  values/          # versioned Helm values
  resources/       # manifests applied after the chart
```

The root platform chart derives Argo CD sync waves from tiers instead of assigning a wave to every
manifest by hand.

## Hardware-specific design

This host has an i7-14700, 16 GB RAM, and one 1 TB NVMe. The configuration creates one 10 GB k3s
VM instead of three pretend-HA VMs. It attaches a 64 GB OS disk and a 600 GB data disk. See
[storage](docs/storage.md) for realistic capacity and upgrade advice.

## Service addresses

| Address | Service |
| --- | --- |
| `http://10.0.0.220` | Homarr |
| `http://10.0.0.230:8096` | Jellyfin |
| `http://10.0.0.231` | Radarr |
| `http://10.0.0.232` | Sonarr |
| `http://10.0.0.233` | Prowlarr |
| `http://10.0.0.234` | qBittorrent |
| `http://10.0.0.235` | Bazarr |
| `http://10.0.0.236` | Seerr |
| `http://10.0.0.237` | Maintainerr |
| `http://10.0.0.240` | Immich |

FlareSolverr is cluster-internal only. Exclude `10.0.0.200-10.0.0.250` from DHCP before deploying
MetalLB.

## Before the first apply

1. Complete [the Proxmox prerequisites](docs/proxmox-prerequisites.md), including checking the real
   `local-lvm` capacity and creating the API token.
2. Install Ubuntu 24.04 WSL once; Ansible runs there while Terraform and kubectl run on Windows.

## Deploy from this laptop

Prepare the laptop. The script installs Terraform, creates a dedicated SSH key, generates local
secrets, and configures a read-only Argo CD deploy key for the private GitHub repository:

```powershell
.\scripts\prepare-workstation.ps1
```

Then deploy the VM, k3s, Argo CD, Sealed Secrets, and applications:

```powershell
.\scripts\deploy.ps1
```

On its first run, the deployment seals the gitignored plaintext inputs with this cluster's public
key, applies the encrypted resources, and backs up the controller key to a gitignored file. Commit
and push the generated `sealed-secret-*.yaml` files afterward; copy the controller-key backup to
encrypted storage outside this server. See [secret management](docs/secrets.md).

The deployment fetches the admin kubeconfig to `provisioning/ansible/kubeconfig`. Use it from this
laptop with:

```powershell
kubectl --kubeconfig provisioning/ansible/kubeconfig get nodes -o wide
kubectl --kubeconfig provisioning/ansible/kubeconfig get pods -A
```

Or use the included wrapper without changing any existing Docker Desktop kubeconfig:

```powershell
.\scripts\k.ps1 get nodes -o wide
.\scripts\k.ps1 get pods -A
```

To make it the default for the current PowerShell session:

```powershell
$env:KUBECONFIG = "$PWD\provisioning\ansible\kubeconfig"
kubectl get nodes
```

## Manual route

```bash
terraform -chdir=provisioning/terraform init
terraform -chdir=provisioning/terraform plan -out homefallout.tfplan
terraform -chdir=provisioning/terraform apply homefallout.tfplan

cd provisioning/ansible
ansible-playbook -i inventory.generated.yml playbooks/cluster.yml
cd ../..
```

Then apply the root app, wait for the Sealed Secrets controller, and create the encrypted manifests:

```powershell
.\scripts\bootstrap.ps1
kubectl --kubeconfig provisioning/ansible/kubeconfig rollout status deployment/sealed-secrets-controller -n secrets --timeout=5m
.\scripts\seal-secrets.ps1
kubectl --kubeconfig provisioning/ansible/kubeconfig apply -f platform/components/media-stack/resources/sealed-secret-gluetun-vpn.yaml
kubectl --kubeconfig provisioning/ansible/kubeconfig apply -f platform/components/media-stack/resources/sealed-secret-homarr-secrets.yaml
kubectl --kubeconfig provisioning/ansible/kubeconfig apply -f platform/components/immich/resources/sealed-secret-immich-database.yaml
.\scripts\backup-sealing-key.ps1
```

Read [architecture](docs/architecture.md), [storage](docs/storage.md),
[secret management](docs/secrets.md), and [first-run wiring](docs/configuration.md) before adding
downloads or importing photos.
