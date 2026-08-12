# Proxmox one-time preparation

The repository creates reusable Ubuntu template VMID `9000` directly from Ubuntu's cloud image,
then creates VMIDs `220`, `240`, and `230` as full clones. It does not require SSH access to the
Proxmox host or a manually built template.

## 1. Confirm storage capacity

Open the Proxmox shell and run:

```bash
pvesm status
lvs
```

The default configuration requests 20 GB, 20 GB, and 28 GB OS/config disks plus a sparse 4 GB
template disk on directory-backed `local`. It requests a separate 600 GB thin-provisioned bulk-data
disk on `local-lvm`; the rest of that thin pool remains reserved for future Immich/media growth.

Your reported `local` storage has about 83 GB available. These directory-backed qcow2 disks are
sparse, but together can grow to 68 GB plus the downloaded cloud image. Monitor the Proxmox root
filesystem closely and keep at least 10-15 GB physically free. Sparse images and thin provisioning
do not create extra physical capacity.

## 2. Enable image imports and VM disks

In the Proxmox UI, open **Datacenter -> Storage -> local -> Edit** and ensure both **Disk image** and
**Import** are included in Content. Terraform stores the template and OS/config disks there and downloads the
Ubuntu image directly through Proxmox's API. Do this before applying; `local-lvm` already supports
disk images.

## 3. Create the Terraform API token

Run these commands in the Proxmox shell. This uses a dedicated automation identity rather than
your root account.

```bash
pveum user add terraform@pve
pveum role add TerraformHome -privs "VM.PowerMgmt VM.Audit VM.Allocate VM.Clone VM.Config.CDROM VM.Config.CPU VM.Config.Cloudinit VM.Config.Disk VM.Config.HWType VM.Config.Memory VM.Config.Network VM.Config.Options VM.GuestAgent.Audit Datastore.Audit Datastore.Allocate Datastore.AllocateSpace Datastore.AllocateTemplate Sys.Audit Sys.Modify SDN.Use"
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

Reserve `10.0.0.10-10.0.0.12` for the k3s VMs and exclude `10.0.0.200-250` from the router's DHCP pool. The
laptop is currently on the same `10.0.0.0/24` LAN, so no route or port forward is required for
`kubectl`.
