# Overseer — build brief

Hand this to a fresh session. Everything below is verified against the live house on
2026-08-20/21, not assumed. Where something is unverified it says so.

Mockups and the design rationale: the design artifact for this app (ask Charles for the link).

---

## 1. What you are building

A small web app, self-hosted in this repo's k3s cluster, that the four people in the house open
on their phones to run the living room:

- The Samsung TV: power, source (Jellyfin / TV), D-pad, volume.
- The Hue lights, grouped by room.
- Ambient sync on/off (Hyperion drives the lightstrip from what's on screen).
- Per-person logins, and **per-room permissions** enforced server-side.

Reachable on the LAN at `house.home.lan` and over the existing Tailscale subnet router.
Later, not now: a media tab for requesting films and watching downloads land.

---

## 2. Decisions already made — do not relitigate

| Decision | Why |
| --- | --- |
| Custom app, not a Home Assistant dashboard | Charles wants a single-purpose UI his housemates can use without HA's chrome |
| The app talks **only** to Home Assistant, never to the TV or bridge directly | One credential, one integration point; new devices become HA integrations rather than new code here |
| Per-person accounts with per-room permissions | Charles + Charlotte's bedroom must not be controllable by Zach or Tim |
| Permissions enforced in the request handler | Hiding buttons in CSS is decoration, not access control |
| Python FastAPI + SQLite, server-rendered Jinja, vanilla JS | No build chain exists in this repo; don't add npm |
| Image built by GitHub Actions to GHCR | First self-built image in this repo; everything else is upstream |
| Dark only | It's a remote used in a dark living room |

---

## 3. Environment — verified facts

### Home Assistant

- LAN: `http://10.0.0.241:8123` · in-cluster: `http://home-assistant.media.svc.cluster.local:8123`
- Runs `hostNetwork: true` on `k3s-apps-01` (needed for SSDP/mDNS discovery and Wake-on-LAN).
- A long-lived token already exists and is in `platform/secrets/house-secrets.yaml` (gitignored).

### The TV

| | |
| --- | --- |
| Model | Samsung `QN55Q80TAFXZC` (Q80T, 55", 2020) |
| Name | `[TV] Television Salon` |
| Address | `10.0.0.163`, wireless — **give it a DHCP reservation** |
| Wi-Fi MAC | `64:E7:D8:36:52:60` |
| `media_player` entity | `media_player.living_room_television_salon` |
| `remote` entity | `remote.living_room_television_salon` |

Verified from the HA API:

```
source_list: ['TV', 'HDMI']      # exactly two — the integration's whole source map
supported_features: 24509        # includes VOLUME_SET, VOLUME_STEP, SELECT_SOURCE, TURN_ON/OFF
volume_level: null               # null while the TV is OFF; readable when on
```

- **Volume is absolute.** HA reaches it over the TV's UPnP rendering service, so
  `media_player.volume_set` works and `volume_level` reads back. Do not fake a counter.
- **Source switching is only `TV` and `HDMI`.** `KEY_HDMI` may cycle HDMI inputs rather than
  landing on HDMI 1 — **this is still untested** because the TV was off. Test it before wiring
  the Jellyfin button, and fall back to `KEY_SOURCE` + arrow navigation if it cycles.
- Key codes for `remote.send_command`: `KEY_POWER KEY_VOLUP KEY_VOLDOWN KEY_MUTE KEY_UP KEY_DOWN
  KEY_LEFT KEY_RIGHT KEY_ENTER KEY_RETURN KEY_HOME KEY_EXIT KEY_MENU KEY_TOOLS KEY_INFO
  KEY_SOURCE KEY_HDMI KEY_PLAY KEY_PAUSE KEY_STOP`.
- Tizen drops its network stack in standby, so a sleeping TV looks like a TV that was never on
  the network. Handle the off state explicitly.

### The lights

Only **four** bulbs are real. Two entities in HA are stale hardware Charles no longer owns and
must not appear in the app:

| Entity | Name | Use |
| --- | --- | --- |
| `light.living_room_living_room` | Living room (group) | Room-level control |
| `light.living_room_living_room_wall` | Living room wall — Hue lightstrip plus | The bulb Hyperion drives |
| `light.charles_charlotte_3_charles_charlotte_3` | Charles & Charlotte<3 (group) | Room-level control |
| `light.charles_charlotte_3_main_lamp` | Main Lamp | |
| `light.charles_charlotte_3_main_lamp_secondary` | Main Lamp Secondary | |
| `light.charles_charlotte_3_bedroom` | Bedroom | |
| ~~`light.gone`~~ | Kitchen | **STALE — exclude** |
| ~~`light.gone2`~~ | David room | **STALE — exclude** |

All support `color_temp` and `xy`, so brightness, colour and warmth are all available.

### Ambient sync (Hyperion)

Runs on the Proxmox host, driving the lightstrip from the Jellyfin kiosk's screen.

- Endpoint: `POST http://10.0.0.254:8090/json-rpc` — **no authentication required** for this call.
- Turn on / off:
  ```json
  {"command":"componentstate","componentstate":{"component":"LEDDEVICE","state":true}}
  ```
- Read current state: `{"command":"serverinfo"}` → `info.components[]`, find `LEDDEVICE`.
- **It is off by default and must stay that way.** `device.autoStart` is `false`; the app and
  Hyperion's own UI are the only things that turn it on. Do not re-enable autoStart.
- Verified working end to end: enabling it flips the Hue bridge's `Living Room TV` entertainment
  configuration to `status: active`, disabling it returns `inactive`.

---

## 4. People and permissions

| Person | Role | Rooms |
| --- | --- | --- |
| **Charles** | admin | everything |
| **Charlotte** | user | Living room + Charles & Charlotte<3 |
| **Zach** | user | Living room |
| **Tim** | user | Living room |

The remote, volume and ambient sync are available to everyone with an account. There is no
"Kitchen" and no person called David.

Seed all four at first boot with random one-time passwords printed to the pod log,
`must_change_password = 1`. Charles can reset any password from the admin screen, which shows the
new one once.

---

## 5. Credentials

Follow the repo's existing sealed-secrets flow — see `docs/secrets.md`.

- Plaintext lives in `platform/secrets/house-secrets.yaml` (gitignored; already written, contains
  a real `HA_TOKEN` and a generated `SESSION_SECRET`).
- Template committed at `platform/secrets/house-secrets.example.yaml`.
- **You must add it to `scripts/seal-secrets.ps1`**: append the source path to `$secretSources`
  and add `'house-secrets' = platform\components\house\resources` to `$secretDestinations`.
- Charles should rotate `HA_TOKEN` once the build settles — it passed through a chat transcript.

---

## 6. What already exists

```
apps/house/requirements.txt      fastapi 0.141.1, uvicorn 0.52.4, jinja2 3.1.6,
                                 httpx 0.28.1, python-multipart 0.0.32   (versions verified on PyPI)
apps/house/app/db.py             users, sessions, per-room permissions.
                                 scrypt via hashlib (no passlib), server-side sessions in SQLite,
                                 constant-time compare, dummy hash on unknown usernames.
platform/secrets/house-secrets.example.yaml
platform/secrets/house-secrets.yaml   (gitignored, filled in)
```

Everything else is unwritten.

---

## 7. Interaction rules

### The held button — this replaced an earlier "optimistic UI" rule

A key press takes a couple of hundred milliseconds to reach the Samsung. **Do not** acknowledge
optimistically and reconcile later. Instead:

1. On press the button enters a **held** state immediately — visibly depressed, accent ring — and
   is disabled for further input.
2. It stays held until the server responds.
3. On success it releases. On failure it shows a brief error state.
4. A **2-second timeout** always releases it, so it can never get stuck.

The point is that the user can *see* it working, so there is nothing to spam, and two impatient
presses can never become two channel changes.

```js
async function act(btn, fn) {
  if (btn.dataset.busy) return;               // second press is ignored, not queued
  btn.dataset.busy = "1";
  btn.classList.add("held");
  btn.setAttribute("aria-busy", "true");
  const release = () => {
    btn.classList.remove("held");
    btn.removeAttribute("aria-busy");
    delete btn.dataset.busy;
  };
  const bail = setTimeout(release, 2000);
  try { await fn(); }
  catch { btn.classList.add("failed"); setTimeout(() => btn.classList.remove("failed"), 1200); }
  finally { clearTimeout(bail); release(); }
}
```

**Volume is the one exception.** Taps stack into a target level and are debounced (~250 ms) into a
single `media_player.volume_set`, so holding minus feels continuous instead of queuing eight round
trips. The steppers still show the held state, they just accept taps while held.

### The sync conflict

While Hyperion streams, Hue Entertainment holds `light.living_room_living_room_wall`
**exclusively** — brightness and colour calls will appear to succeed and do nothing.

So: when sync is on, the app must disable that light's brightness/colour controls, say why, and
offer one button that turns sync off and hands the light back. Never render controls that silently
no-op.

---

## 8. Design system

Full mockups in the artifact. Tokens:

```css
--a-bg:     #0A0B0E;   /* ground */
--a-card:   #15171C;   /* card */
--a-card2:  #1E212A;   /* raised */
--a-line:   #272B34;   /* hairline */
--a-text:   #F3F3F5;
--a-muted:  #888C96;
--a-accent: #FF7A2F;   /* on / selected / live only */
--a-live:   #34D07F;   /* status dot */
```

- Type: **Plus Jakarta Sans** 400/500/600/700/800 for the whole app. Weight carries hierarchy;
  there is no second face. `font-variant-numeric: tabular-nums` anywhere digits stack.
- **Icons are inline SVG on one stroke weight. Never emoji.** Charles asked for this explicitly.
- Geometry is round: 20–28px card radii, circles for controls.
- **Active inverts to white**: an off light is a dark circle, an on light is a solid white disc
  with an accent-coloured icon. This is the signature state cue — keep it.
- Minimum 44px touch targets, visible `:focus-visible` rings, honour `prefers-reduced-motion`.
- The D-pad is a **circular disc** with OK at the centre and four circular satellites (Home,
  Source, Back, Menu) at the corners — not a grid of squares.
- Volume is a horizontal track with round − and + buttons either side.
- Bottom tab bar, three tabs: Remote · Lights · People (People is admin-only).

---

## 9. Deploying it — this repo's conventions

Read `docs/architecture.md` and copy the shape of `platform/components/home-assistant/`.

1. `platform/components/house/resources/house.yaml` — PVC (`config-local-retain`), Deployment,
   Service. Namespace `media`, `nodeSelector: homelab.charles/pool: apps`, `strategy: Recreate`.
2. Register in `platform/values/values-prod.yaml`: `tier: apps`, `project: applications`,
   `waveOffset: 11` (Home Assistant is 9, ingress is 10 — **bump ingress to 12** or pick an offset
   below it; the ingress component must sync last because its rules name Services).
3. Add `house.home.lan` to the `media` Ingress in
   `platform/components/ingress/resources/ingress.yaml`.
4. MetalLB: `10.0.0.241` is Home Assistant. **`10.0.0.242` is the next free address** in the
   `media-and-photos` pool (230–250).
5. Optional: add a tile to the Homarr board via the media-stack bootstrap ConfigMap.

**Validate before pushing** — CI runs all of this:

```bash
./scripts/validate-platform.sh          # helm render + path checks
python3 scripts/validate-bootstrap.py   # python-in-ConfigMap syntax
yamllint -d '{extends: relaxed, rules: {line-length: {max: 200}}}' platform/
```

House style is a **branch + PR**, squash-merged with `(#N)` in the title. CI is kubeconform +
yamllint + gitleaks. `.gitattributes` enforces LF for `.py`/`.sh`/`.yaml` and CRLF for `.ps1`.

### The image

First self-built image in this repo. Add `.github/workflows/build-house.yaml` publishing
`ghcr.io/chevlpcmp-lab/house:<sha>` (and `:latest`) on pushes touching `apps/house/**`. Pin the
Deployment to the digest or the sha tag, not `latest`, so Argo CD rollouts are deliberate.

---

## 10. Still open

1. **Name** — "Overseer" is a working title, a Fallout nod to match the repo. Confirm with Charles.
2. **HDMI 1** — untested. Does `select_source: HDMI` land on HDMI 1 or cycle?
3. **Scenes** — the mockups show invented Movie / Relax / Bright / Off. The Hue bridge already has
   real scenes (Honolulu, Cancun, Read, Nightlight, Relax, Energize…) exposed to HA as
   `scene.*` entities. Ask Charles which he wants.
4. **TLS** — the ingress is plain HTTP, so passwords cross the LAN unencrypted. Acceptable for now;
   worth revisiting.
5. **Media tab** — requesting films, watching downloads. Explicitly later.
