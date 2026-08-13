# First-run application wiring

A `bootstrap` sidecar in the media-stack pod (`bootstrap-configmap.yaml`) wires the
applications together automatically. It reads each app's API key straight from its config
volume, waits for the APIs to come up, and idempotently re-asserts the configuration on
every pod restart. The sidecar also watches the ConfigMap digest, so an Argo-applied change to the
Git-owned bootstrap is loaded automatically without a manual rollout. It configures:

| From | To | URL |
| --- | --- | --- |
| Radarr / Sonarr | qBittorrent | `http://localhost:8080` |
| Prowlarr | Radarr / Sonarr | full-sync Applications using `http://localhost:7878` / `:8989` |
| Prowlarr | FlareSolverr | `http://localhost:8191` |
| Bazarr | Radarr | `http://localhost:7878` |
| Bazarr | Sonarr | `http://localhost:8989` |
| Bazarr | Jellyfin | library refresh after subtitle changes, over Service DNS |
| Radarr / Sonarr | Jellyfin | Emby/Jellyfin library-update notification over Service DNS |
| Bootstrap | Jellyfin | Movies and TV Shows libraries from `/media/library/movies` and `/media/library/tv` |
| Seerr | Jellyfin, Radarr, Sonarr | first-run admin, libraries, profiles, and default instances |
| Maintainerr | Jellyfin, Radarr, Sonarr, Seerr, qBittorrent | cleanup-engine service configuration |
| Homarr | everything | Service DNS, for example `http://radarr.media.svc.cluster.local` |

It also creates the shared paths and sets them in qBittorrent and the Arr applications:

- qBittorrent incomplete: `/data/downloads/incomplete`
- qBittorrent complete: `/data/downloads/complete`
- Radarr root: `/data/library/movies`
- Sonarr root: `/data/library/tv`

## Git-owned application desired state

The reproducible application configuration lives in
`platform/components/media-stack/resources/bootstrap-configmap.yaml`; the Kubernetes workload that
executes and watches it lives beside that file in `media-stack.yaml`. Together they own the shared
login, download paths and categories, Arr roots and clients, Prowlarr applications and proxy,
credential-free indexers, the optional Arr `Ultra-HD` profiles, Jellyfin libraries and refresh
notifications, Bazarr policy, Seerr services, Maintainerr rules, and Homarr's initial dashboard
layout.

Do not commit raw `/config` directories, SQLite databases, or API responses. They contain generated
credentials, history, and mutable state. On a blank installation, the applications generate their
local databases and API keys, then the bootstrap discovers those keys and rebuilds the Git-owned
wiring. To force and verify the same reconciliation at any time:

```powershell
.\scripts\reconcile-media-config.ps1
```

Git recreates desired settings; backups recreate personal state. Jellyfin watch history, Homarr
layout changes made after bootstrap, Seerr requests, application history, Immich's database, photos,
and downloaded media still require the off-host backup set described in
[Operations](operations.md#recovery-order-after-a-total-rebuild).

## One shared admin account

The `apps-admin` secret (`platform/secrets/media-secrets.yaml`) is the single source of
truth for the admin login, and the bootstrap enforces it on every pod restart:

| App | Coverage |
| --- | --- |
| qBittorrent | WebUI credentials synced |
| Radarr / Sonarr / Prowlarr | forms login enforced with the shared credentials |
| Bazarr | form login enforced with the shared credentials |
| Jellyfin | fresh-install wizard, shared admin user, and password handled automatically |
| Seerr | signs in with Jellyfin, so the shared credentials work automatically |
| Maintainerr | has no login system |
| Homarr | set the admin password to match once by hand (its API cannot reset the account that owns the API key) |

To rotate the password: edit `apps-admin` in the plaintext file, run
`.\scripts\seal-secrets.ps1`, commit and push, and restart the media-stack deployment.
The bootstrap pushes the new password everywhere, including Homarr's stored qBittorrent
integration credentials. Check progress or failures with:

```powershell
kubectl --kubeconfig provisioning/ansible/kubeconfig logs -n media deploy/media-stack -c bootstrap
```

## What “connected” looks like

Radarr and Sonarr's **Settings > Connect** page contains notification providers. It is not where
their Prowlarr relationship appears. The bootstrap adds an **Emby / Jellyfin** entry there so
Jellyfin refreshes after imports and renames.

| UI | Expected record |
| --- | --- |
| Radarr / Sonarr **Settings > Download Clients** | `qBittorrent` |
| Radarr / Sonarr **Settings > Indexers** | Indexers synchronized from Prowlarr |
| Radarr / Sonarr **Settings > Connect** | `Jellyfin` library-update connection |
| Prowlarr **Settings > Apps** | `Radarr` and `Sonarr`, both set to Full Sync |
| Prowlarr **Settings > Indexers** | EZTV, LimeTorrents, Nyaa.si, and YTS |
| Prowlarr **Settings > Indexers > Proxies** | `FlareSolverr` |
| Seerr **Settings > Services** | Default Radarr and Sonarr instances |
| Maintainerr **Settings** | Jellyfin, Radarr, Sonarr, Seerr, and qBittorrent |
| Bazarr **Settings > Languages** | English and French enabled; `English + French` profile set as the series and movie default (both languages are downloaded - Bazarr has no separate Quebec French) |
| Bazarr **Settings > Providers** | YIFY Subtitles, Gestdown, Sous-Titres.eu, and SubF2M; all four work without stored third-party credentials |
| Bazarr **Settings > Jellyfin** | Enabled with the shared API key, immediate refresh, and the Movies/TV Shows library names and IDs selected |

Bazarr checks wanted movies and episodes every six hours, searches providers concurrently, uses
adaptive searching to preserve provider quotas, and upgrades young subtitle matches for seven days.
Automatic downloads retain Bazarr's conservative score floors of 90 for episodes and 70 for movies.
Downloaded sidecars are UTF-8, mode `0664`, and stored beside the media so Jellyfin sees them. Usable
embedded subtitle tracks count as present. YIFY covers movies particularly well, Gestdown covers TV,
Sous-Titres.eu improves French coverage, and SubF2M is the general fallback. An OpenSubtitles.com or
SubDL account can be added later for broader coverage, but neither credential is required for the
deployed baseline.

The Git-owned baseline uses four public, credential-free indexers: EZTV and Nyaa.si for TV coverage,
YTS for compact movies, and LimeTorrents as a broad fallback. Because the Applications are
reconciled to Full Sync, Prowlarr publishes them to Radarr and Sonarr automatically. FlareSolverr is
available to compatible indexers. Private trackers and providers requiring accounts remain an
intentional manual extension unless their credentials are added through a SealedSecret and their
non-secret schema is added to the bootstrap.

## Homarr dashboard provisioning

The bootstrap also fills Homarr with app tiles (with LAN links and ping URLs),
integrations for Radarr, Sonarr, Prowlarr, Bazarr, Seerr, qBittorrent, and Jellyfin, and a
`media` board pre-populated with those tiles plus calendar, downloads, media-server, and
request widgets. Homarr's own API key gates this provisioning:

1. Open Homarr at `http://10.0.0.220`, create the admin account, then create an API key
   under **Management > Tools > API** (format `id.token`). Put it in `HOMARR_API_KEY` in
   `platform/secrets/media-secrets.yaml`.
2. Reseal and commit the updated ciphertext. Argo updates the ConfigMap/Secrets, and the bootstrap
   watcher reloads desired state. The reconcile helper forces an immediate full check:

```powershell
.\scripts\seal-secrets.ps1
git add platform/components/*/resources/sealed-secret-*.yaml; git commit -m "Add Homarr bootstrap keys"; git push
.\scripts\reconcile-media-config.ps1
```

`JELLYFIN_API_KEY` remains an optional fast path for a restored Jellyfin database. On a blank
database, the bootstrap completes Jellyfin's startup wizard using the sealed `apps-admin`
credentials and obtains a device token automatically; no Jellyfin API key needs to be copied by
hand.

The board is only laid out when the bootstrap creates it: rearranging tiles afterwards is
safe, and deleting the `media` board makes the next restart rebuild it. After Jellyfin's API key is
available, the bootstrap completes Seerr's first-run Jellyfin login, enables its movie/TV
libraries, creates its default Arr services, and configures Maintainerr through its API. It also
reconciles the storage-aware Maintainerr retention rules documented in
[Storage](storage.md#automatic-media-retention). Use a Jellyfin favorite, an Arr `keep` tag, or a
Maintainerr collection exclusion to protect an item from automated deletion.

qBittorrent's traffic uses Gluetun's default route and kill switch. Proton NAT-PMP port forwarding
is enabled, and Gluetun automatically updates qBittorrent's listening port whenever Proton assigns
or changes it. A pod init container enforces qBittorrent's **Bypass authentication for clients on
localhost** setting so only the local Gluetun API call bypasses login; LAN WebUI clients still
authenticate. Confirm the VPN before adding downloads:

```powershell
kubectl --kubeconfig provisioning/ansible/kubeconfig exec -n media deploy/media-stack -c gluetun -- wget -qO- https://ipinfo.io
kubectl --kubeconfig provisioning/ansible/kubeconfig exec -n media deploy/media-stack -c qbittorrent -- wget -qO- https://ipinfo.io
```

The two results must match the VPN address, not the home's public address.

## Jellyfin hardware transcoding

The default manifest uses CPU transcoding. After passing the Intel iGPU through Proxmox to
`k3s-media-01`, add a `hostPath` volume for `/dev/dri`, mount it at `/dev/dri` in
the Jellyfin container, and configure VA-API or QSV in Jellyfin. Confirm the worker sees the device
before changing the manifest.
