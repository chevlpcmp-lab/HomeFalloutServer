# Storage

Terraform attaches one 600 GB thin-provisioned virtual data disk to `k3s-home-01`. Ansible locates
it using the unique `HOMEFALLOUT_DATA` serial, formats it only when it is blank, and mounts it at
`/mnt/data`. It refuses to guess a `/dev/sdX` device.

| Guest path | Contents |
| --- | --- |
| `/mnt/data/media` | downloads and the Jellyfin library |
| `/mnt/data/photos` | Immich originals and uploads |

Verify the result after provisioning with `findmnt /mnt/data`. The data disk is not a backup: both
virtual disks reside on the same physical 1 TB NVMe.

Ansible creates this layout, owned by UID/GID 1000:

```text
/mnt/data/
  media/
    downloads/
      incomplete/
      complete/
    library/
      movies/
      tv/
  photos/
```

Use `/data/downloads` and `/data/library` in every media application. Because downloads and the
library share one mounted filesystem, Radarr and Sonarr can hardlink instead of copying files.
Jellyfin sees the same library read-only under `/media/library`.

The declared PV sizes are Kubernetes metadata rather than quotas. The physical limit is the NVMe
and Proxmox thin pool. Keep at least 15–20% free; a full thin pool can damage every volume on it.

Application databases/configuration use the `local-path-retain` StorageClass. Deleting a PVC does
not automatically delete its backing PV, but you must still back up those directories and the
Immich Postgres database; a `Retain` policy is not itself a backup. Ansible configures k3s's local
provisioner to use `/mnt/data/k3s-storage`, keeping these PVCs off the 64 GB OS disk.

## Recommended upgrade order

1. Add a conventional 8–16 TB SATA CMR hard drive for media. Keep Proxmox, VM OS, databases, and
   download staging on NVMe; pass the HDD to the VM or create a dedicated Proxmox data volume.
2. Upgrade to 32 GB RAM. This gives Immich ML and Jellyfin transcoding room without starving
   Proxmox. The i7-14700 has ample CPU; memory and storage are the constraints.
3. Add an external backup target for Immich originals, Postgres, and application configuration.
   Another internal disk, a USB disk used only for backups, or encrypted cloud backup is useful.

Until an HDD is added, expect roughly 450–520 GB of practical media/photo capacity after Proxmox,
the VM OS, application data, download staging, and safety headroom. Configure qBittorrent to remove
completed downloads after import and use Maintainerr to cap library growth.
