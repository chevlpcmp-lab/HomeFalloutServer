# Tailscale remote access

A Tailscale **subnet router** (`platform/components/tailscale/`) advertises the home
networks to the tailnet, so any device logged into Tailscale — phone, laptop, anywhere —
can reach everything as if it were on the LAN, with no ports opened on the router:

| Route | What it exposes |
| --- | --- |
| `10.0.0.0/24` | The whole LAN: Proxmox, Argo CD (`10.0.0.200`), Homarr (`10.0.0.220`), Jellyfin, the arr apps, Immich, printers, everything |
| `10.42.0.0/16` | Cluster pod network (debugging) |
| `10.43.0.0/16` | Cluster Services by ClusterIP (debugging) |

It is intentionally a subnet router only. Exit-node advertising is disabled so the pod does not
claim default IPv4/IPv6 routes or require IPv6 forwarding. It can be added later as a separate,
explicit choice.

The component is enabled in `platform/values/values-prod.yaml` and pinned to the apps worker.

## Enabling it

1. Create a tailnet at https://tailscale.com if you don't have one, and install the client
   on the devices that should get access.
2. In the admin console, **Settings > Keys > Generate auth key**: reusable OFF, ephemeral
   OFF, pre-approved ON (if device approval is enabled). Copy the `tskey-auth-...` value.
3. Paste it into `platform/secrets/tailscale-secrets.yaml` (gitignored), then seal it:

   ```powershell
   .\scripts\seal-secrets.ps1
   ```

4. Commit and push the sealed secret. Argo CD deploys the enabled router.
5. In the admin console, open **Machines**, find `homefallout`, and **approve the
   advertised subnet routes** (Edit route settings). Without approval the routes stay inactive.
6. From a tailnet device off the home network, verify: `http://10.0.0.200` (Argo CD),
   `https://<proxmox-ip>:8006`, `http://10.0.0.220` (Homarr).

The auth key is single-use: after the first login the node identity lives in the
`tailscale-state` PVC and survives restarts (`TS_AUTH_ONCE=true`). If the state PVC is
ever lost, generate a fresh key and reseal.

## Notes

- Devices on the tailnet use their normal DNS; to resolve nothing special is needed since
  everything here is addressed by IP. MagicDNS can be enabled tailnet-wide independently.
- Disable **key expiry** for the `homefallout` machine in the admin console (Machine >
  Disable key expiry) so the router does not drop off the tailnet after the default
  180 days.
- Access can be narrowed later with tailnet ACLs (for example, family devices may reach
  only `10.0.0.230-240`).
