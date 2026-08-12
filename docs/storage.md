# Storage

This host has one physical NVMe, but the layout separates replaceable operating-system state,
application configuration, and large user data so their capacity and backup policies are clear.

| Proxmox storage | Virtual disk | Guest path | Purpose |
| --- | ---: | --- | --- |
| `local` | 32 GB | `/` | Ubuntu, k3s, container images, and etcd |
| `local` | 32 GB | `/mnt/config` | Every persistent application config/database/cache PVC |
| `local-lvm` | 600 GB | `/mnt/data` | Downloads, movies, TV, and Immich originals/uploads |

The remaining unallocated `local-lvm` capacity is deliberately reserved for future media/photo
growth. It is not consumed by application configuration.

## Persistent application state

Ansible identifies the config disk by the `HOMEFALLOUT_CONFIG` serial, formats it only if blank,
and mounts it at `/mnt/config`. K3s's local-path provisioner creates volumes under
`/mnt/config/k3s-storage`; every application PVC explicitly selects `config-local-retain`.

This includes:

- Radarr, Sonarr, Prowlarr, qBittorrent, Bazarr, Seerr, and Maintainerr configuration.
- Homarr's `/appdata` volume.
- Jellyfin configuration and cache.
- Immich Postgres data and machine-learning cache.

Gluetun and FlareSolverr are intentionally stateless. Immich Valkey/Redis is only a disposable
cache; authoritative Immich state is in Postgres and the photo library. Kubernetes Secret values
and the Sealed Secrets controller state live in etcd on the OS disk, while the sealing-key export
must also be copied to encrypted storage outside this server.

The PVC requests total less than the 32 GB config disk, but the local-path provisioner does not
enforce per-PVC quotas. Monitor `/mnt/config` and keep at least 8 GB free. A `Retain` reclaim policy
protects against automatic deletion; it is not a backup.

## Bulk media and photos

Ansible locates the 600 GB disk by its `HOMEFALLOUT_DATA` serial, formats it only when blank, and
mounts it at `/mnt/data`:

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

Use `/data/downloads` and `/data/library` in qBittorrent and the Arr applications. Downloads and
the library share one filesystem, allowing Radarr and Sonarr to hardlink instead of copying.
Jellyfin sees the library read-only at `/media/library`.

The media and photos PV capacities are binding metadata rather than separate quotas: both share
the same 600 GB filesystem. Keep the Proxmox thin pool below roughly 80-85% actual usage.

## Why Longhorn is not installed

Longhorn's availability comes from replicas placed on separate storage nodes. This cluster has
one node, one Proxmox host, and one physical NVMe, so additional replicas would share the same
failure domain while consuming extra capacity, memory, and I/O. Longhorn's own current best
practices recommend three nodes and dedicated disks for its V1 data engine.

Local ext4 volumes are simpler here. Add Longhorn only after adding multiple Kubernetes nodes with
independent disks. Even then, use an external NFS/S3 backup target: Longhorn snapshots remain on
cluster disks, while backups are what survive loss of the cluster.

## Recommended upgrade order

1. Add a conventional 8-16 TB SATA CMR hard drive for media and photos. Keep databases and
   configuration on NVMe.
2. Upgrade to 32 GB RAM so Immich ML and Jellyfin transcoding cannot starve Proxmox.
3. Add an external backup target for `/mnt/config`, Immich originals, and the sealing key.

Until an HDD is added, expect roughly 450-520 GB of practical media/photo capacity after download
staging and safety headroom. Configure qBittorrent to remove completed downloads after import and
use Maintainerr to cap library growth.
