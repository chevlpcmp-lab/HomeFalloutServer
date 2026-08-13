# Operations runbook

This is the day-two guide: health checks, normal changes, backups, upgrades, and the first places to
look when something misbehaves.

## Fast health check

```powershell
.\scripts\k.ps1 get nodes -o wide
.\scripts\k.ps1 get applications -n argocd
.\scripts\k.ps1 get pods -A -o wide
.\scripts\k.ps1 get pvc -A
.\scripts\k.ps1 get svc -A
```

Healthy means:

- All three nodes are `Ready`.
- Argo CD child Applications are `Synced` and `Healthy`.
- Workload pods are `Running` or a short-lived job has `Completed`.
- PVCs are `Bound` to the expected worker.
- LoadBalancer Services have their requested `10.0.0.x` address.
- Argo CD's LAN Service answers at `10.0.0.200`.

For a compact resource view:

```powershell
.\scripts\k.ps1 top nodes
.\scripts\k.ps1 top pods -A --sort-by=memory
```

Metrics commands require Metrics Server; if it is not installed, use Proxmox charts and
`kubectl describe` resource data instead.

## Normal GitOps change

1. Edit the owning file under `platform/components/` or `platform/values/values-prod.yaml`.
2. Review `git diff` and make sure no plaintext Secret or local credential is staged.
3. Commit and push to `main`.
4. Watch Argo CD reconcile.
5. Verify the affected rollout, service, logs, and persistence.

```powershell
git diff --check
git status --short
git push
.\scripts\k.ps1 get applications -n argocd -w
```

Do not make a direct cluster edit and expect it to persist. Argo CD self-healing restores the Git
version. Emergency live edits should be followed immediately by the equivalent Git change.

## Workload inspection

```powershell
# Rollout state
.\scripts\k.ps1 rollout status -n media deploy/media-stack

# All containers and restart counts in the VPN pod
.\scripts\k.ps1 get pod -n media -l app=media-stack -o wide
.\scripts\k.ps1 describe pod -n media -l app=media-stack

# Per-container logs
.\scripts\k.ps1 logs -n media deploy/media-stack -c gluetun --tail=200
.\scripts\k.ps1 logs -n media deploy/media-stack -c qbittorrent --tail=200
.\scripts\k.ps1 logs -n media deploy/media-stack -c bootstrap --tail=200

# Previous crash logs
.\scripts\k.ps1 logs -n media deploy/media-stack -c gluetun --previous
```

Equivalent examples for standalone workloads:

```powershell
.\scripts\k.ps1 logs -n media deploy/jellyfin --tail=200
.\scripts\k.ps1 logs -n media deploy/homarr --tail=200
.\scripts\k.ps1 logs -n photos deploy/immich-server --tail=200
.\scripts\k.ps1 logs -n photos statefulset/immich-postgres --tail=200
```

## VPN safety check

Run this after deploying, after a VPN configuration change, and whenever download behavior is
suspicious:

```powershell
.\scripts\k.ps1 exec -n media deploy/media-stack -c gluetun -- wget -qO- https://ipinfo.io
.\scripts\k.ps1 exec -n media deploy/media-stack -c qbittorrent -- wget -qO- https://ipinfo.io
```

The public addresses must match and must differ from the home's WAN address. If they do not, pause
qBittorrent and inspect Gluetun. Also check the forwarded-port update in Gluetun/bootstrap logs
after reconnects.

## Intentional restarts

```powershell
.\scripts\k.ps1 rollout restart -n media deploy/media-stack
.\scripts\k.ps1 rollout restart -n media deploy/homarr
.\scripts\k.ps1 rollout restart -n media deploy/jellyfin
.\scripts\k.ps1 rollout restart -n photos deploy/immich-server
```

Restarting `media-stack` restarts every VPN-sharing application as one unit. Its retained PVCs and
bulk-data PV survive. Do not delete PVCs as a troubleshooting shortcut.

## Storage checks

Kubernetes capacity metadata does not report actual shared filesystem usage. Check both the PVC
state and guest/Proxmox filesystems:

```powershell
.\scripts\k.ps1 get pv,pvc -A -o wide
.\scripts\k.ps1 exec -n media deploy/media-stack -c radarr -- df -h /data
```

On `k3s-media-01`, inspect:

```bash
df -h /
df -h /mnt/data
du -sh /var/lib/rancher/k3s/storage/* 2>/dev/null | sort -h
du -sh /mnt/data/media /mnt/data/photos
```

Keep the shared data filesystem and Proxmox thin pool below roughly 80–85% actual use. Remember
that the two 500 Gi PV declarations share one 600 GB disk and are not independent quotas.

## Backup model

At present, backups must be arranged outside this repository. A useful minimum set is:

| Priority | Data | Why | Suggested method |
| ---: | --- | --- | --- |
| 1 | Immich originals and uploads | Irreplaceable personal data | Versioned copy to external disk/NAS/cloud |
| 1 | Immich Postgres | Required to reconstruct users, albums, faces, and metadata | Scheduled `pg_dump` plus off-host copy |
| 1 | Sealed Secrets controller key | Required to decrypt committed ciphertext after total cluster loss | Encrypted offline copy |
| 2 | Application config PVCs | Saves setup, databases, API keys, and history | Filesystem-level backup while apps are quiesced |
| 2 | Terraform state and gitignored inputs | Speeds exact infrastructure recovery | Encrypted off-host backup |
| 3 | Media library | Expensive to reacquire but often replaceable | External disk/NAS according to value |
| 4 | Download staging and caches | Disposable | Usually exclude |

Do not count Proxmox snapshots on the same NVMe as backups. They are convenient rollback points but
share the same failure domain. Test restoration, especially Postgres plus the matching photo tree.

### Sealing-key refresh

```powershell
.\scripts\backup-sealing-key.ps1
```

Move the output from `platform/secrets/` to encrypted off-host storage. Run it after first deploy
and periodically because the controller renews keys.

## Update strategy

- Review Renovate proposals; never auto-merge all application updates blindly.
- Prefer explicit image versions over `latest`/`release` for reliable rollback.
- Back up state before database, Immich, or k3s upgrades.
- Change one layer at a time: Proxmox, then VM OS, then k3s/platform, then applications.
- Read upstream release notes for schema migrations and breaking configuration changes.
- Verify VPN egress and application integration after any media pod image change.

For a routine manifest update:

```powershell
git checkout -b update/component-version
# edit the image or chart version
git diff --check
git commit -am "Update component version"
git push -u origin HEAD
```

After merge, watch Argo CD and the workload rollout. Revert the Git commit to roll back declarative
configuration; application data migrations may require the component's own recovery procedure.

## Capacity and performance

The 16 GB host is intentionally close to its useful ceiling. Watch for host swapping, guest OOM
kills, and Immich ML/Jellyfin concurrency. Practical tuning order:

1. Avoid running Immich bulk indexing while Jellyfin is CPU transcoding.
2. Limit background jobs in the application UIs if the media worker experiences memory pressure.
3. Add Intel iGPU passthrough for Jellyfin before buying CPU capacity.
4. Upgrade to 32 GB RAM before adding observability, more databases, or another large workload.
5. Add an 8–16 TB CMR disk and external backup target before the NVMe fills.

## Troubleshooting matrix

| Symptom | First checks | Likely boundary |
| --- | --- | --- |
| Proxmox says no guest agent | Package/service inside VM, Terraform agent channel, then VM reboot | Terraform + Ansible |
| Node `NotReady` | VM power/network, `systemctl status k3s` or `k3s-agent`, disk pressure | Proxmox / guest OS |
| Argo Application `OutOfSync` | Application conditions, Git revision, repository credential | Argo CD / Git |
| Pod `Pending` | Events, node selector, PVC binding, allocatable memory | Scheduling / storage |
| Pod `CrashLoopBackOff` | Current and `--previous` container logs, Secret keys, mounts | Application config |
| LoadBalancer address pending | MetalLB pods, IP pool, requested annotation | Networking |
| Argo CD LAN UI unavailable | `svc/argocd`, `argocd-server` readiness, address `.200` | GitOps / networking |
| Tailscale routes unavailable | Component enabled, pod logs, admin-console route approval | Optional remote access |
| Media pod never becomes ready | Gluetun logs, WireGuard key, provider/country, DNS | VPN secret/network |
| qBittorrent UI rejects login | `qbittorrent-auth` sealed value and bootstrap logs | Secret / bootstrap |
| Arr cannot import | Paths match `/data`, permissions are UID/GID 1000, free disk | Storage / app wiring |
| Jellyfin library empty | `/media/library` mount and Radarr/Sonarr output paths | Storage |
| Immich upload fails | server/Postgres logs, photos PVC, disk space | Immich / storage |
| Homarr tiles not created | Homarr API key and bootstrap logs | First-run wiring |

## Recovery order after a total rebuild

1. Restore Proxmox networking and storage definitions.
2. Restore the encrypted local Terraform inputs and SSH material, or generate replacements.
3. Run Terraform and Ansible to recreate the template, VMs, mounts, and k3s.
4. Restore the Sealed Secrets controller key before expecting committed ciphertext to decrypt.
5. Bootstrap Argo CD and let the platform reconcile.
6. Let the media bootstrap reconstruct its Git-owned application wiring; restore application config
   volumes and Immich Postgres when their history and user state must survive.
7. Restore the matching `/mnt/data/photos` and any protected media data.
8. Start applications, verify storage bindings, then verify the VPN boundary.

The exact restore tooling depends on the backup target eventually chosen. Git can recreate the
media integrations, libraries, retention policy, and a blank Jellyfin identity, but it cannot
recreate media, photos, watch/request history, or application databases. Until a tested off-host
backup exists, the lab is reproducible infrastructure with non-reproducible personal state, not a
complete disaster-recovery system.
