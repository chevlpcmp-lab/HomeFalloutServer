# Proxmox one-time preparation

The repository creates VM `230` directly from Ubuntu's cloud image. It does not require a manually
built template or SSH access to the Proxmox host.

## 1. Confirm storage capacity

Open the Proxmox shell and run:

```bash
pvesm status
lvs
```

The default configuration requests a 32 GB OS disk and a 32 GB application-config disk on the
directory-backed `local` storage. It requests a separate 600 GB thin-provisioned bulk-data disk on
`local-lvm`; the rest of that thin pool remains reserved for future Immich/media growth.

Your reported `local` storage has about 83 GB available. The two directory-backed VM disks are
sparse, but together can grow to 64 GB, so monitor the Proxmox root filesystem and keep at least
15 GB physically free. Reduce `config_disk_gb` before deployment if other files are added to
`local`. Sparse images and thin provisioning do not create extra physical capacity.

## 2. Enable image imports and VM disks

In the Proxmox UI, open **Datacenter -> Storage -> local -> Edit** and ensure both **Disk image** and
**Import** are included in Content. Terraform stores the OS/config disks there and downloads the
Ubuntu image directly through Proxmox's API. Do this before applying; `local-lvm` already supports
disk images.

## 3. Create the Terraform API token

Run these commands in the Proxmox shell. This uses a dedicated automation identity rather than
your root account.

```bash
pveum user add terraform@pve
pveum role add TerraformHome -privs "VM.PowerMgmt VM.Audit VM.Allocate VM.Clone VM.Config.CPU VM.Config.Cloudinit VM.Config.Disk VM.Config.HWType VM.Config.Memory VM.Config.Network VM.Config.Options Datastore.Audit Datastore.Allocate Datastore.AllocateSpace Datastore.AllocateTemplate Sys.Audit Sys.Modify SDN.Use"
pveum aclmod / -user terraform@pve -role TerraformHome
pveum user token add terraform@pve homefallout --privsep=0
```

Copy the token secret immediately; Proxmox shows it only once. Its final form is:

```text
terraform@pve!homefallout=THE_RETURNED_SECRET
```

Pass that value only to `scripts/prepare-workstation.ps1`. It writes it to the gitignored
`terraform.tfvars` file.

The first Terraform run deliberately leaves the QEMU guest-agent channel disabled because the
stock cloud image does not contain the agent yet. Ansible installs the package. You may later add
`qemu_guest_agent_enabled = true` to `terraform.tfvars`, apply once more, and reboot the VM to gain
agent-backed IP/filesystem reporting in Proxmox; Kubernetes does not require it.

## 4. Check address reservations

Reserve `10.0.0.10` for the k3s VM and exclude `10.0.0.200-250` from the router's DHCP pool. The
laptop is currently on the same `10.0.0.0/24` LAN, so no route or port forward is required for
`kubectl`.
