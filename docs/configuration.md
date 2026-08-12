# First-run application wiring

All media applications except Jellyfin share a pod, so use `localhost` for their internal links:

| From | To | URL |
| --- | --- | --- |
| Radarr / Sonarr | qBittorrent | `http://localhost:8080` |
| Radarr / Sonarr | Prowlarr | configured by Prowlarr using `http://localhost:7878` / `:8989` |
| Prowlarr | FlareSolverr | `http://localhost:8191` |
| Bazarr | Radarr | `http://localhost:7878` |
| Bazarr | Sonarr | `http://localhost:8989` |
| Seerr / Maintainerr | Jellyfin | `http://jellyfin:8096` |

Set the same paths in qBittorrent and the Arr applications:

- qBittorrent incomplete: `/data/downloads/incomplete`
- qBittorrent complete: `/data/downloads/complete`
- Radarr root: `/data/library/movies`
- Sonarr root: `/data/library/tv`

qBittorrent's traffic uses Gluetun's default route and kill switch. Proton NAT-PMP port forwarding
is enabled, and Gluetun automatically updates qBittorrent's listening port whenever Proton assigns
or changes it. This relies on qBittorrent's **Bypass authentication for clients on localhost**
option, which is enabled by default; leave that option on. Confirm the VPN before adding downloads:

```powershell
kubectl --kubeconfig provisioning/ansible/kubeconfig exec -n media deploy/media-stack -c gluetun -- wget -qO- https://ipinfo.io
kubectl --kubeconfig provisioning/ansible/kubeconfig exec -n media deploy/media-stack -c qbittorrent -- wget -qO- https://ipinfo.io
```

The two results must match the VPN address, not the home's public address.

## Jellyfin hardware transcoding

The default manifest uses CPU transcoding. After passing the Intel iGPU through Proxmox to
`k3s-home-01`, add a `hostPath` volume for `/dev/dri`, mount it at `/dev/dri` in
the Jellyfin container, and configure VA-API or QSV in Jellyfin. Confirm the worker sees the device
before changing the manifest.
