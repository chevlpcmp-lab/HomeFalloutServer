#!/usr/bin/env bash
# Ambient lighting for the TV kiosk: capture what the kiosk is drawing and push the
# edge colours to the Hue lights.
#
# This works here because of an accident of the setup worth understanding. Normally
# ambient lighting needs hardware in the HDMI path - a Hue Sync Box, or a 2022+ Samsung
# running Philips' own Tizen app, neither of which applies to a 2020 Q80T. But this TV's
# picture is generated on the Proxmox host itself (docs/tv-console.md), so software on
# that host can read the framebuffer directly and skip the HDMI problem entirely.
#
# The limit that follows from the same fact: this only sees what the kiosk draws. TV
# apps, live inputs, and anything else on another HDMI port are invisible to it.
#
# Like tv-kiosk.sh, this runs ON the Proxmox host, not from the laptop:
#
#   scp scripts/tv-hyperion.sh root@10.0.0.254:/root/
#   ssh root@10.0.0.254 'bash /root/tv-hyperion.sh --test'
#   ssh root@10.0.0.254 'bash /root/tv-hyperion.sh --hue'
#   ssh root@10.0.0.254 'bash /root/tv-hyperion.sh'
#
#   --test       capture the kiosk screen and report whether it is grabbable, change nothing
#   --hue        pair with the Hue bridge; press the bridge button first
#   --check      report what is and is not in place, change nothing
#   --uninstall  remove Hyperion and the service, leaving the kiosk untouched
#
# See docs/ambient-lighting.md for the Hue Entertainment area prerequisite, which cannot
# be created from here and must be done in the official Hue app first.
set -euo pipefail

KIOSK_USER="${KIOSK_USER:-tv}"
HUE_BRIDGE="${HUE_BRIDGE:-10.0.0.144}"
# Hyperion's own web UI, where the capture and the LED layout are configured. Nothing in
# this script writes that configuration: Hyperion's UI is good, and a generated config
# JSON is a fragile thing to maintain against upstream schema changes.
HYPERION_PORT=8090
# Pinned rather than tracked, like every other image and chart in this repository. The
# project's apt repo exists but publishes no signing key at any documented URL any more,
# and an unverified apt source on a hypervisor is a worse trade than a pinned download.
HYPERION_VERSION="2.2.1"
HYPERION_DEB_URL="https://github.com/hyperion-project/hyperion.ng/releases/download/${HYPERION_VERSION}/Hyperion-${HYPERION_VERSION}-Linux-amd64.deb"
SERVICE="/etc/systemd/system/hyperion-kiosk.service"
USERDATA="/var/lib/hyperion"
# Written by --hue, read by nothing else: Hyperion wants these pasted into its web UI.
# Root-only, because the clientkey is a credential for every light in the house.
CRED_FILE="/root/.hyperion-hue-credentials"
# A 1920x1080 screenshot of real video content lands in the hundreds of KB. An all-black
# frame - which is what an overlay-rendered video looks like to a screen grabber - is a
# nearly empty PNG and compresses to almost nothing. Crude, but it separates the two
# cases without decoding the image.
MIN_GRAB_BYTES=50000

say()  { printf '==> %s\n' "$*"; }
info() { printf '    %s\n' "$*"; }
warn() { printf '    WARNING: %s\n' "$*" >&2; }
die()  { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

home_of() { getent passwd "$KIOSK_USER" | cut -d: -f6; }

# Printed in hints, so they can be pasted rather than retyped with the address filled in.
HOST_IP="$(hostname -I 2>/dev/null | awk '{print $1}')"


# Run a command against the kiosk session's X server, exactly as tv-kiosk.sh does:
# startx keeps its cookie in /tmp/serverauth.*, owned by the kiosk user.
x_env() {
  local auth
  auth=$(ls -t /tmp/serverauth.* 2>/dev/null | head -1)
  [[ -n "${auth:-}" ]] || return 1
  runuser -u "$KIOSK_USER" -- env DISPLAY=:0 XAUTHORITY="$auth" "$@"
}

# ---------------------------------------------------------------- grab test

# The whole approach rests on the screen being readable. Hardware-decoded video is
# sometimes drawn through a GPU overlay plane that never lands in the framebuffer a
# grabber can see, and the failure is silent: Hyperion installs, runs, reports no error,
# and drives every light to black. Test first, always.
grab_test() {
  [[ $EUID -eq 0 ]] || die "run this as root on the Proxmox host"
  id "$KIOSK_USER" >/dev/null 2>&1 || die "no ${KIOSK_USER} user; run tv-kiosk.sh first"

  command -v scrot >/dev/null 2>&1 || die "scrot is missing; rerun tv-kiosk.sh to install it"

  local out="/tmp/hyperion-grabtest.png"
  rm -f "$out"

  say "capturing the kiosk screen"
  x_env scrot -o "$out" 2>/dev/null \
    || die "could not capture. Is the kiosk session up? systemctl restart getty@tty1.service"

  local size
  size=$(stat -c %s "$out" 2>/dev/null || echo 0)
  info "wrote ${out} (${size} bytes)"

  if (( size < MIN_GRAB_BYTES )); then
    warn "that capture is very small, which usually means the screen is black or the
       video is on a GPU overlay the grabber cannot see. If something was playing when
       you ran this, X11 capture will not work and Hyperion has nothing to read.
       Look at the image before going further:
         scp root@${HOST_IP}:${out} ."
    return 1
  fi

  say "looks grabbable"
  info "Copy it down and confirm the video is actually in the picture, not a black box:"
  info "  scp root@<host>:${out} ."
  return 0
}

# ---------------------------------------------------------------- preflight

preflight() {
  [[ $EUID -eq 0 ]] || die "run this as root on the Proxmox host"

  command -v pveversion >/dev/null 2>&1 \
    || warn "this does not look like a Proxmox host; continuing anyway"

  id "$KIOSK_USER" >/dev/null 2>&1 \
    || die "no ${KIOSK_USER} user. Ambient lighting needs the kiosk; run tv-kiosk.sh first."

  x_env true 2>/dev/null \
    || die "no X session for ${KIOSK_USER}. Start it first:
       systemctl restart getty@tty1.service"

  local free_gb
  free_gb=$(df --output=avail -BG / | tail -1 | tr -dc '0-9')
  (( free_gb >= 2 )) || die "only ${free_gb} GB free on /; this filesystem also holds the VM disks"
  info "kiosk session up, ${free_gb} GB free on /"

  # 16 GB host, most of it committed to VMs and roughly 1 GB left after the kiosk. This
  # is not fatal - hyperiond with a downsampled grab is small - but it is the resource
  # this host is actually short of, so say the number out loud.
  local free_mb
  free_mb=$(free -m | awk '/^Mem:/ { print $7 }')
  info "${free_mb} MB RAM available"
  (( free_mb >= 300 )) || warn "that is tight; hyperiond wants roughly 150 MB"

  if curl -fsS --max-time 5 "http://${HUE_BRIDGE}/api/config" >/dev/null 2>&1; then
    info "Hue bridge reachable at ${HUE_BRIDGE}"
  else
    warn "cannot reach the Hue bridge at ${HUE_BRIDGE} right now"
  fi

  # Non-fatal on purpose: someone may legitimately want to install while the TV shows a
  # menu rather than video, and the real verdict needs a moving picture anyway.
  say "screen grab"
  grab_test || warn "continuing anyway, but fix this before expecting light output"
}

# ---------------------------------------------------------------- install

install_hyperion() {
  say "Hyperion ${HYPERION_VERSION}"
  if command -v hyperiond >/dev/null 2>&1; then
    info "already installed: $(hyperiond --version 2>/dev/null | head -1)"
    return
  fi

  local deb="/tmp/hyperion-${HYPERION_VERSION}.deb"
  info "downloading the pinned package (about 60 MB)"
  curl -fsSL -o "$deb" "$HYPERION_DEB_URL"     || die "could not download ${HYPERION_DEB_URL}"

  # Same enterprise-repo caveat as tv-kiosk.sh: a Proxmox host without a subscription
  # fails apt-get update on that one 401 every time, and it must not abort the run.
  DEBIAN_FRONTEND=noninteractive apt-get update -qq     || warn "apt-get update reported errors (enterprise repo without a subscription?)"

  # Installing the file through apt rather than dpkg so its dependencies resolve; dpkg
  # would leave the package half-configured and the error would point at the wrong thing.
  DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends "$deb"     || die "Hyperion did not install. Read the apt output above; a missing dependency on
       this Debian release is the usual cause."
  rm -f "$deb"

  # The package's postinst enables and starts hyperion@root.service. That instance runs
  # as root with no access to the kiosk's X server, so it can never grab this screen -
  # but it does claim port 8090 and it will fight for the Hue stream, leaving the useful
  # instance dead and the web UI showing the useless one. A glob does not match a running
  # template instance, so enumerate them by name.
  local unit
  while read -r unit; do
    [[ -n "$unit" ]] || continue
    systemctl disable --now "$unit" >/dev/null 2>&1 || true
    info "disabled ${unit} - it cannot see the kiosk display"
  done < <(systemctl list-units --no-legend --plain 'hyperion@*' 2>/dev/null | awk '{print $1}')
  info "installed"
}

# ---------------------------------------------------------------- hue pairing

# Hyperion needs two things from the bridge: a username and a clientkey. The clientkey
# is what the Entertainment API uses to stream, and it is only handed out when the
# request asks for it - which is why an ordinary Hue pairing is not enough here.
pair_hue() {
  [[ $EUID -eq 0 ]] || die "run this as root on the Proxmox host"

  say "pairing with the bridge at ${HUE_BRIDGE}"
  info "press the round button on top of the bridge now; waiting up to 60 seconds"

  local i resp
  for i in $(seq 1 30); do
    resp=$(curl -fsS --max-time 5 -X POST "http://${HUE_BRIDGE}/api" \
      -H 'Content-Type: application/json' \
      -d '{"devicetype":"hyperion#tv-kiosk","generateclientkey":true}' 2>/dev/null || echo "")

    if [[ "$resp" == *'"username"'* ]]; then
      local user key
      user=$(sed -n 's/.*"username":"\([^"]*\)".*/\1/p' <<<"$resp")
      key=$(sed -n 's/.*"clientkey":"\([^"]*\)".*/\1/p' <<<"$resp")

      umask 077
      cat > "$CRED_FILE" <<EOF
# Hyperion credentials for the Hue bridge at ${HUE_BRIDGE}.
# Paste these into Hyperion's web UI under LED Hardware > Philips Hue.
# The clientkey controls every light in the house - keep this file root-only.
HUE_BRIDGE=${HUE_BRIDGE}
HUE_USERNAME=${user}
HUE_CLIENTKEY=${key}
EOF
      say "paired"
      info "credentials written to ${CRED_FILE}"
      info "username:  ${user}"
      info "clientkey: ${key}"
      return 0
    fi

    if [[ "$resp" == *'"link button not pressed"'* ]]; then
      sleep 2
      continue
    fi

    [[ -n "$resp" ]] && warn "unexpected reply: ${resp}"
    sleep 2
  done

  die "the bridge never accepted the pairing. Press the button on the bridge, then rerun
       this within 30 seconds: bash /root/tv-hyperion.sh --hue"
}

# ---------------------------------------------------------------- service

# Hyperion's packaged unit runs as its own user and cannot see the kiosk's X server, so
# this replaces it with one that runs as the kiosk user inside that session's world.
# XAUTHORITY is resolved at start rather than baked in, because startx picks a new
# /tmp/serverauth.* filename every time the session restarts.
write_service() {
  say "service"
  install -d -o "$KIOSK_USER" -g "$KIOSK_USER" -m 0755 "$USERDATA"

  cat > "$SERVICE" <<EOF
[Unit]
Description=Hyperion ambient lighting for the TV kiosk
Documentation=https://docs.hyperion-project.org
After=network-online.target

[Service]
User=${KIOSK_USER}
Environment=DISPLAY=:0
# The kiosk session may not be up yet at boot, and there is no unit to order against
# because it is started by getty autologin rather than by systemd. Fail fast and let
# Restart handle it: the service simply retries until the X cookie appears.
ExecStartPre=/bin/sh -c 'test -n "\$(ls -t /tmp/serverauth.* 2>/dev/null | head -1)"'
ExecStart=/bin/sh -c 'exec env XAUTHORITY=\$(ls -t /tmp/serverauth.* | head -1) /usr/bin/hyperiond --userdata ${USERDATA}'
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF

  systemctl daemon-reload
  systemctl enable --now hyperion-kiosk.service
  info "wrote ${SERVICE} and started it"
}

# ---------------------------------------------------------------- check

check() {
  local rc=0

  say "kiosk"
  if id "$KIOSK_USER" >/dev/null 2>&1 && x_env true 2>/dev/null; then
    info "session is up"
  else
    info "MISSING - the kiosk must be running for any of this to work"
    rc=1
  fi

  say "hyperion"
  if command -v hyperiond >/dev/null 2>&1; then
    info "installed"
  else
    info "MISSING"
    rc=1
  fi

  say "service"
  if systemctl is-active --quiet hyperion-kiosk.service; then
    info "hyperion-kiosk.service active"
  else
    info "NOT RUNNING"
    rc=1
  fi

  say "hue credentials"
  if [[ -f "$CRED_FILE" ]]; then
    info "present in ${CRED_FILE}"
  else
    info "MISSING - run --hue"
    rc=1
  fi

  say "bridge"
  if curl -fsS --max-time 5 "http://${HUE_BRIDGE}/api/config" >/dev/null 2>&1; then
    info "reachable at ${HUE_BRIDGE}"
  else
    info "UNREACHABLE at ${HUE_BRIDGE}"
    rc=1
  fi

  say "web UI"
  if curl -fsS --max-time 5 "http://127.0.0.1:${HYPERION_PORT}/" >/dev/null 2>&1; then
    info "answering on ${HYPERION_PORT}"
  else
    info "NOT ANSWERING on ${HYPERION_PORT}"
    rc=1
  fi

  echo
  if (( rc )); then
    echo "INCOMPLETE - see docs/ambient-lighting.md"
  else
    echo "OK - configure the capture and the Hue output at http://<host>:${HYPERION_PORT}"
  fi
  return "$rc"
}

# ---------------------------------------------------------------- uninstall

uninstall() {
  [[ $EUID -eq 0 ]] || die "run this as root on the Proxmox host"

  say "stopping the service"
  systemctl disable --now hyperion-kiosk.service >/dev/null 2>&1 || true
  rm -f "$SERVICE"
  systemctl daemon-reload
  info "removed"

  say "removing Hyperion"
  if command -v hyperiond >/dev/null 2>&1; then
    DEBIAN_FRONTEND=noninteractive apt-get purge -y --auto-remove hyperion || true
    info "purged"
  else
    info "not installed"
  fi

  say "leaving alone"
  info "${USERDATA} (Hyperion's settings) and ${CRED_FILE} (the Hue credentials)"
  info "The kiosk itself is untouched. Delete the Hue user in the Hue app if you are done."
}

# ---------------------------------------------------------------- main

main() {
  case "${1:-}" in
    --test)      if grab_test; then exit 0; else exit 1; fi ;;
    --check)     if check; then exit 0; else exit 1; fi ;;
    --hue)       pair_hue; exit 0 ;;
    --uninstall) uninstall; exit 0 ;;
    "")          ;;
    *)           die "unknown argument: $1 (expected --test, --check, --hue or --uninstall)" ;;
  esac

  preflight
  install_hyperion
  write_service

  echo
  say "done"
  cat <<EOF
    Hyperion is running. Two things left, both in its web UI:

      http://${HOST_IP}:${HYPERION_PORT}

    1. Capture: enable the Platform Capture, type X11, and confirm the live preview
       shows what is on the TV. If the preview is black, stop here - see
       docs/ambient-lighting.md.

    2. LED Hardware > Philips Hue: enter the bridge address, the username and the
       clientkey from --hue, and pick your Entertainment area.

    The Entertainment area itself has to exist first, and it can only be created in the
    official Hue app - Home Assistant cannot make one. If the dropdown is empty, that is
    why.

    While Hyperion is streaming, those lights belong to it: Home Assistant and the Hue
    app cannot control them until it stops.
EOF
}

main "$@"
