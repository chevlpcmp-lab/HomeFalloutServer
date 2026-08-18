#!/usr/bin/env bash
# Turn the Proxmox host's HDMI output into a Jellyfin appliance for the living-room TV.
#
# This is the one script in this repository that runs ON the Proxmox host rather than
# from the Windows laptop, because it configures the hypervisor's own console:
#
#   scp scripts/tv-kiosk.sh root@10.0.0.254:/root/
#   ssh root@10.0.0.254 'bash /root/tv-kiosk.sh --check'
#   ssh root@10.0.0.254 'bash /root/tv-kiosk.sh'
#
#   --check          report what is and is not in place, change nothing
#   --audio          point audio at HDMI; run once after the session is first up
#   --display 4k     3840x2160 at 30 Hz - sharp, but the Jellyfin UI renders tiny
#   --display 1080p  1920x1080 at 60 Hz - the default; readable from a sofa
#   --display        show the current mode and what else the TV offers
#   --uninstall      put the host back to a plain text console
#
# Everything here is idempotent: rerun it after a Proxmox upgrade to repair the session.
# See docs/tv-console.md for the first-run checklist and the audio troubleshooting.
set -euo pipefail

KIOSK_USER="${KIOSK_USER:-tv}"
JELLYFIN_URL="${JELLYFIN_URL:-http://10.0.0.230:8096}"
# Jellyfin Desktop, the successor to Jellyfin Media Player. The old Flathub ID
# com.github.iwalton3.jellyfin-media-player no longer resolves; `flatpak install` quietly
# falls back to a name search and lands here anyway, which hides the rename until the
# session tries to launch the dead ID and shows a black screen.
FLATPAK_APP="org.jellyfin.JellyfinDesktop"
VAAPI_EXT="org.freedesktop.Platform.VAAPI.Intel"
GETTY_DROPIN="/etc/systemd/system/getty@tty1.service.d/autologin.conf"
# Where --display records its choice. Read by .xinitrc at session start, because that
# file is regenerated on every run of this script and would lose a hand-edited xrandr.
DISPLAY_CONF_REL=".config/tv-kiosk/display.conf"
# 1080p60 by default, not the TV's native 4K30. The Jellyfin web UI renders at 1:1 CSS
# pixels with no DPI scaling, so on a 4K panel every label is a quarter of the size it
# should be and unreadable from a sofa. 1080p also buys 60 Hz, since this panel offers
# no 4K60 at all. Override per-host with --display 4k.
DEFAULT_MODE="1920x1080"
DEFAULT_RATE="60"
# The KDE runtime that Jellyfin Desktop pulls in is the bulk of this, and the Proxmox
# root filesystem is the same one holding the VM disks, so check before spending it.
MIN_FREE_GB=5

PACKAGES=(
  xserver-xorg-core
  xserver-xorg-input-libinput
  xinit
  x11-xserver-utils
  openbox
  flatpak
  dbus-user-session
  pipewire
  pipewire-pulse
  wireplumber
  pulseaudio-utils
  # Without rtkit, PipeWire cannot acquire realtime priority and falls back to nice 0,
  # logging an RTKit error on every start. It is a recommends, and recommends are off.
  rtkit
  # Lets an operator see what is actually on the TV over SSH instead of walking to it.
  scrot
)

say()  { printf '==> %s\n' "$*"; }
info() { printf '    %s\n' "$*"; }
warn() { printf '    WARNING: %s\n' "$*" >&2; }
die()  { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

home_of() { getent passwd "$KIOSK_USER" | cut -d: -f6; }

installed() {
  dpkg-query -W -f='${Status}' "$1" 2>/dev/null | grep -q '^install ok installed$'
}

# ---------------------------------------------------------------- preflight

preflight() {
  [[ $EUID -eq 0 ]] || die "run this as root on the Proxmox host"

  command -v pveversion >/dev/null 2>&1 \
    || warn "this does not look like a Proxmox host; continuing anyway"

  [[ -e /dev/dri/renderD128 ]] \
    || die "no /dev/dri/renderD128 - the iGPU is not available to the host. If it has
       been passed through to a VM, this kiosk cannot work; see docs/tv-console.md."

  local free_gb
  free_gb=$(df --output=avail -BG / | tail -1 | tr -dc '0-9')
  if (( free_gb < MIN_FREE_GB )); then
    die "only ${free_gb} GB free on / and the runtime needs about ${MIN_FREE_GB} GB. Free
       space first; this filesystem also holds the VM disks."
  fi
  info "iGPU present, ${free_gb} GB free on /"

  if curl -fsS --max-time 5 "${JELLYFIN_URL}/System/Info/Public" >/dev/null 2>&1; then
    info "Jellyfin reachable at ${JELLYFIN_URL}"
  else
    # Not fatal: the media VM may simply be down while the host is being set up.
    warn "cannot reach ${JELLYFIN_URL} right now - the kiosk will still install"
  fi
}

# ---------------------------------------------------------------- install

install_packages() {
  say "packages"
  local pkg
  local missing=()
  for pkg in "${PACKAGES[@]}"; do
    installed "$pkg" || missing+=("$pkg")
  done

  if (( ${#missing[@]} == 0 )); then
    info "all present"
    return
  fi

  info "installing: ${missing[*]}"
  # A Proxmox host with the enterprise repo but no subscription fails `apt-get update`
  # on that one 401 every time. That must not abort the run, so warn and press on with
  # whatever package lists are already cached.
  DEBIAN_FRONTEND=noninteractive apt-get update -qq \
    || warn "apt-get update reported errors (enterprise repo without a subscription?)"
  # No recommends: this is a hypervisor, not a workstation, and the recommends chain
  # drags in a desktop's worth of packages it will never use.
  DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends "${missing[@]}"
}

create_user() {
  say "kiosk user '${KIOSK_USER}'"
  if id "$KIOSK_USER" >/dev/null 2>&1; then
    info "exists"
  else
    useradd --create-home --shell /bin/bash --comment 'Jellyfin TV kiosk' "$KIOSK_USER"
    # No password is ever set: this account is reachable only by autologin on the
    # physical console, and locking it keeps it out of SSH and su.
    passwd -l "$KIOSK_USER" >/dev/null
    info "created and locked"
  fi
  # video/render for the iGPU, input for the keyboard and mouse, audio for HDMI out.
  # Skip any that this Debian does not define rather than failing the whole run.
  local group
  local groups=()
  for group in video render input audio; do
    getent group "$group" >/dev/null && groups+=("$group")
  done
  if (( ${#groups[@]} )); then
    usermod -aG "$(IFS=,; echo "${groups[*]}")" "$KIOSK_USER"
    info "groups: ${groups[*]}"
  fi
}

vaapi_present() {
  flatpak list --system --columns=application 2>/dev/null \
    | grep -qx "$VAAPI_EXT"
}

install_app() {
  say "Jellyfin Desktop"
  flatpak remote-add --if-not-exists --system \
    flathub https://dl.flathub.org/repo/flathub.flatpakrepo

  if flatpak info --system "$FLATPAK_APP" >/dev/null 2>&1; then
    info "already installed"
  else
    info "installing from Flathub (app plus a KDE runtime, about 2 GB)"
    flatpak install -y --system flathub "$FLATPAK_APP"
  fi

  # The KDE runtime declares the Intel VA-API driver as download-if=have-intel-gpu, so
  # flatpak pulls it in unprompted on a host with an iGPU. Verify rather than install:
  # the extension's branch tracks the freedesktop runtime, not the app, and guessing it
  # produces a confident-sounding warning about a driver that is already there.
  if vaapi_present; then
    info "Intel VA-API driver present, video decodes on the iGPU"
  else
    warn "no Intel VA-API driver; video will decode on the CPU. Try:
       flatpak install --system flathub ${VAAPI_EXT}"
  fi
}

write_display_default() {
  say "display default"
  local home conf
  home="$(home_of)"
  conf="${home}/${DISPLAY_CONF_REL}"

  # Never clobber a deliberate --display choice on a rerun.
  if [[ -f "$conf" ]]; then
    info "keeping the existing choice in ${conf}"
    return
  fi

  mkdir -p "$(dirname "$conf")"
  # OUTPUT is deliberately blank: at install time X has never run, so there is nothing
  # to detect. .xinitrc fills it in from the connected output at session start.
  cat > "$conf" <<EOF
# Written by tv-kiosk.sh. Applied by .xinitrc at session start.
# Change with: tv-kiosk.sh --display 4k | 1080p
OUTPUT=
MODE=${DEFAULT_MODE}
RATE=${DEFAULT_RATE}
EOF
  chown -R "${KIOSK_USER}:${KIOSK_USER}" "${home}/.config"
  info "${DEFAULT_MODE} at ${DEFAULT_RATE} Hz"
}

configure_wm() {
  say "window manager"
  local home
  home="$(home_of)"
  local dir="${home}/.config/openbox"
  mkdir -p "$dir"

  # Start from the packaged defaults rather than a hand-written fragment, so openbox is
  # never left guessing at sections this file does not mention, then force the player
  # fullscreen and undecorated. Without this it opens as a small window in the middle of
  # a 4K desktop, complete with a title bar, and someone has to fix it by hand at the TV.
  if [[ -f /etc/xdg/openbox/rc.xml ]]; then
    cp /etc/xdg/openbox/rc.xml "${dir}/rc.xml"
    sed -i 's|</openbox_config>|<applications>\n  <application class="*">\n    <fullscreen>yes</fullscreen>\n    <decor>no</decor>\n  </application>\n</applications>\n</openbox_config>|' \
      "${dir}/rc.xml"
    chown -R "${KIOSK_USER}:${KIOSK_USER}" "${home}/.config"
    info "wrote ${dir}/rc.xml, player forced fullscreen"
  else
    warn "no /etc/xdg/openbox/rc.xml to start from; the player will open windowed"
  fi
}

write_session() {
  say "session files"
  local home
  home="$(home_of)"
  [[ -n "$home" ]] || die "cannot resolve the home directory for ${KIOSK_USER}"

  # Unquoted heredoc so the app ID comes from FLATPAK_APP and cannot drift away from
  # the ID the rest of the script installs and checks. There is nothing else in here
  # for the shell to expand.
  cat > "${home}/.xinitrc" <<EOF
#!/bin/sh
# Started by startx from ~/.bash_profile. Managed by scripts/tv-kiosk.sh - edits here
# are overwritten the next time that script runs.

# A TV is not a monitor: never blank, never sleep, never show a screensaver.
xset s off
xset s noblank
xset -dpms

# Apply the saved display mode, if there is one. Without this the TV comes back at
# its own preferred mode on every restart. No backticks anywhere in this heredoc: it
# is unquoted, so they would run as command substitution while the file is generated.
if [ -r "\$HOME/${DISPLAY_CONF_REL}" ]; then
    . "\$HOME/${DISPLAY_CONF_REL}"
    # OUTPUT is blank in the default conf, written before X had ever run on this host.
    [ -z "\$OUTPUT" ] && OUTPUT=\$(xrandr | awk '/ connected/ { print \$1; exit }')
    [ -n "\$MODE" ] && xrandr --output "\$OUTPUT" --mode "\$MODE" --rate "\$RATE" || true
fi

# A window manager for the player to ask fullscreen from. Openbox is about 1 MB and
# does nothing else here.
openbox &

# If the player exits - a crash, or someone picking Quit from the menu - come straight
# back rather than dropping the TV to a login prompt.
while true; do
    flatpak run ${FLATPAK_APP}
    sleep 2
done
EOF

  cat > "${home}/.bash_profile" <<'EOF'
# Managed by scripts/tv-kiosk.sh - edits here are overwritten the next time it runs.
# Start the TV session only on the physical console, so an SSH login as this user or a
# rescue shell on another VT still gets an ordinary prompt.
if [ -z "${DISPLAY:-}" ] && [ "$(tty)" = "/dev/tty1" ]; then
    exec startx -- vt1 > "$HOME/.xsession.log" 2>&1
fi
EOF

  chown "${KIOSK_USER}:${KIOSK_USER}" "${home}/.xinitrc" "${home}/.bash_profile"
  chmod 0644 "${home}/.xinitrc" "${home}/.bash_profile"
  info "wrote ${home}/.xinitrc and ${home}/.bash_profile"
}

enable_autologin() {
  say "console autologin on tty1"
  mkdir -p "$(dirname "$GETTY_DROPIN")"
  cat > "$GETTY_DROPIN" <<EOF
# Managed by scripts/tv-kiosk.sh. Autologin ${KIOSK_USER} on the physical console so the
# TV lands on Jellyfin after a power cut without anyone typing anything.
[Service]
ExecStart=
ExecStart=-/sbin/agetty --autologin ${KIOSK_USER} --noclear %I \$TERM
EOF
  systemctl daemon-reload
  info "wrote ${GETTY_DROPIN}"
}

# ---------------------------------------------------------------- display

# Run a command against the kiosk session's X server. startx keeps its cookie in
# /tmp/serverauth.*, owned by the kiosk user, so point XAUTHORITY at the newest one.
x_env() {
  local auth
  auth=$(ls -t /tmp/serverauth.* 2>/dev/null | head -1)
  [[ -n "${auth:-}" ]] || return 1
  runuser -u "$KIOSK_USER" -- env DISPLAY=:0 XAUTHORITY="$auth" "$@"
}

connected_output() {
  x_env xrandr 2>/dev/null | awk '/ connected/ { print $1; exit }'
}

set_display() {
  [[ $EUID -eq 0 ]] || die "run this as root on the Proxmox host"

  local home
  home="$(home_of)"
  [[ -n "$home" ]] || die "no ${KIOSK_USER} user; run the install first"

  local output
  output=$(connected_output)     || die "no session for ${KIOSK_USER}. Start it first: systemctl restart getty@tty1.service"
  [[ -n "${output:-}" ]] || die "no connected output found"

  local choice="${1:-}"
  local mode rate
  case "$choice" in
    4k)    mode="3840x2160"; rate="30" ;;
    1080p) mode="1920x1080"; rate="60" ;;
    ""|list)
      say "current mode on ${output}"
      x_env xrandr | awk -v o="$output" '$1 == o, /^[A-Z]/ && $1 != o'         | grep -E '\*|connected' | head -3
      say "choices"
      info "--display 4k     3840x2160 at 30 Hz"
      info "--display 1080p  1920x1080 at 60 Hz (default)"
      return 0
      ;;
    *) die "unknown display choice: ${choice} (expected 4k or 1080p)" ;;
  esac

  say "display"
  # Refuse a mode the TV does not advertise rather than leaving a black screen behind.
  if ! x_env xrandr | grep -qE "^\s+${mode}\s"; then
    die "${output} does not offer ${mode}; run --display with no argument to see the list"
  fi

  x_env xrandr --output "$output" --mode "$mode" --rate "$rate"     || die "xrandr rejected ${mode} at ${rate} Hz on ${output}"
  info "${output} now ${mode} at ${rate} Hz"

  # Persist for the next session. .xinitrc reads this file on start.
  local conf="${home}/${DISPLAY_CONF_REL}"
  mkdir -p "$(dirname "$conf")"
  cat > "$conf" <<EOF
# Written by tv-kiosk.sh --display. Applied by .xinitrc at session start.
OUTPUT=${output}
MODE=${mode}
RATE=${rate}
EOF
  chown -R "${KIOSK_USER}:${KIOSK_USER}" "${home}/.config"
  info "saved to ${conf}"

  # Regenerate .xinitrc too. Saving the mode is only half of persistence: a session
  # file written before this feature existed has no idea the conf file is there, and
  # the TV silently reverts on the next restart. write_session is idempotent.
  write_session
}

# ---------------------------------------------------------------- audio

# HDMI is not the default sink on a fresh install - the analog jack wins on priority,
# and the TV is silent. This cannot run during install because it needs the kiosk
# session's PipeWire to be up, so it is a separate step to run once after first boot.
# WirePlumber persists the result under ~/.local/state, so it survives reboots.
fix_audio() {
  [[ $EUID -eq 0 ]] || die "run this as root on the Proxmox host"

  local uid rt
  uid=$(id -u "$KIOSK_USER" 2>/dev/null) || die "no ${KIOSK_USER} user; run the install first"
  rt="/run/user/${uid}"
  [[ -d "$rt" ]]     || die "no session for ${KIOSK_USER}. Start it first: systemctl restart getty@tty1.service"

  # Proxmox ships no sudo, hence runuser; and runuser has no shell to parse a leading
  # VAR=value, hence env.
  as_kiosk() { runuser -u "$KIOSK_USER" -- env XDG_RUNTIME_DIR="$rt" "$@"; }

  say "audio card"
  local card
  card=$(as_kiosk pactl list cards short 2>/dev/null | awk '{ print $2; exit }')
  [[ -n "${card:-}" ]] || die "no audio card visible to the ${KIOSK_USER} session"
  info "$card"

  say "HDMI profile"
  # Plain stereo rather than surround: the TV's own speakers are stereo, and a surround
  # profile on a set that cannot use it is a good way to end up with no sound at all.
  if as_kiosk pactl list cards | grep -q 'output:hdmi-stereo: .*available: yes'; then
    as_kiosk pactl set-card-profile "$card" output:hdmi-stereo
    info "set output:hdmi-stereo"
    sleep 3
  else
    warn "no available output:hdmi-stereo profile on ${card}; leaving the profile alone"
  fi

  say "default sink"
  local sink
  sink=$(as_kiosk pactl list sinks short 2>/dev/null | awk '/hdmi/ { print $2; exit }')
  if [[ -n "${sink:-}" ]]; then
    as_kiosk pactl set-default-sink "$sink"
    # Full volume, because the TV's own remote is the volume control people will reach for.
    as_kiosk pactl set-sink-volume "$sink" 100%
    as_kiosk pactl set-sink-mute "$sink" 0
    info "$sink at 100%, unmuted"
  else
    warn "no HDMI sink appeared; the TV will stay silent"
  fi
}

# ---------------------------------------------------------------- check

check() {
  local rc=0

  say "packages"
  local pkg
  local missing=()
  for pkg in "${PACKAGES[@]}"; do
    installed "$pkg" || missing+=("$pkg")
  done
  if (( ${#missing[@]} )); then
    info "MISSING: ${missing[*]}"
    rc=1
  else
    info "all present"
  fi

  say "kiosk user"
  if id "$KIOSK_USER" >/dev/null 2>&1; then
    info "'${KIOSK_USER}' exists"
  else
    info "MISSING"
    rc=1
  fi

  say "player"
  if flatpak info --system "$FLATPAK_APP" >/dev/null 2>&1; then
    info "${FLATPAK_APP} installed"
  else
    info "MISSING"
    rc=1
  fi

  say "hardware decode"
  if vaapi_present; then
    info "${VAAPI_EXT} present"
  else
    info "MISSING - video would decode on the CPU"
    rc=1
  fi

  say "launch command"
  # The session is only as good as the ID it launches; catch a stale .xinitrc.
  local home_x
  home_x="$(home_of)"
  if [[ -n "$home_x" ]] && grep -q "flatpak run ${FLATPAK_APP}\$" "${home_x}/.xinitrc" 2>/dev/null; then
    info "matches the installed app"
  else
    info "MISSING or stale - .xinitrc does not launch ${FLATPAK_APP}"
    rc=1
  fi

  say "session files"
  local home
  home="$(home_of)"
  if [[ -n "$home" && -f "${home}/.xinitrc" && -f "${home}/.bash_profile" ]]; then
    info "present in ${home}"
  else
    info "MISSING"
    rc=1
  fi

  say "autologin"
  if [[ -f "$GETTY_DROPIN" ]]; then
    info "configured"
  else
    info "MISSING"
    rc=1
  fi

  say "iGPU"
  if [[ -e /dev/dri/renderD128 ]]; then
    info "/dev/dri/renderD128 present"
  else
    info "MISSING - passed through to a VM?"
    rc=1
  fi

  say "Jellyfin"
  if curl -fsS --max-time 5 "${JELLYFIN_URL}/System/Info/Public" >/dev/null 2>&1; then
    info "reachable at ${JELLYFIN_URL}"
  else
    info "UNREACHABLE at ${JELLYFIN_URL}"
    rc=1
  fi

  echo
  if (( rc )); then
    echo "INCOMPLETE - rerun without --check to install or repair"
  else
    echo "OK - the TV kiosk is fully configured"
  fi
  return "$rc"
}

# ---------------------------------------------------------------- uninstall

uninstall() {
  [[ $EUID -eq 0 ]] || die "run this as root on the Proxmox host"

  say "removing autologin"
  rm -f "$GETTY_DROPIN"
  rmdir --ignore-fail-on-non-empty "$(dirname "$GETTY_DROPIN")" 2>/dev/null || true
  systemctl daemon-reload
  systemctl restart getty@tty1.service || true
  info "tty1 is an ordinary login prompt again"

  say "removing the player"
  if flatpak info --system "$FLATPAK_APP" >/dev/null 2>&1; then
    flatpak uninstall -y --system "$FLATPAK_APP"
    flatpak uninstall -y --system --unused
    info "removed, and unused runtimes reclaimed"
  else
    info "not installed"
  fi

  say "removing the kiosk user"
  if id "$KIOSK_USER" >/dev/null 2>&1; then
    pkill -u "$KIOSK_USER" 2>/dev/null || true
    userdel -r "$KIOSK_USER" 2>/dev/null || userdel "$KIOSK_USER"
    info "'${KIOSK_USER}' and its home are gone"
  else
    info "no such user"
  fi

  echo
  echo "The X and audio packages were deliberately left installed; pulling them off a"
  echo "running hypervisor is riskier than the disk they occupy. To reclaim it anyway:"
  echo
  echo "  apt-get purge --auto-remove ${PACKAGES[*]}"
}

# ---------------------------------------------------------------- main

main() {
  case "${1:-}" in
    # `if` rather than `check; exit $?`, so errexit does not swallow the reporting.
    --check)     if check; then exit 0; else exit 1; fi ;;
    --audio)     fix_audio; exit 0 ;;
    --display)   set_display "${2:-}"; exit 0 ;;
    --uninstall) uninstall; exit 0 ;;
    "")          ;;
    *)           die "unknown argument: $1 (expected --check, --audio, --display or --uninstall)" ;;
  esac

  preflight
  install_packages
  create_user
  install_app
  write_display_default
  configure_wm
  write_session
  enable_autologin

  echo
  say "done"
  cat <<EOF
    Switch the TV to this machine's HDMI input and restart the console:

      systemctl restart getty@tty1.service

    Then two one-time steps with the keyboard and mouse at the TV:

      1. Point the player at ${JELLYFIN_URL}
      2. Sign in and tick "remember me"

    Fullscreen is handled by the window manager, so there is nothing to set there.

    If the picture is right but there is no sound, the HDMI output is not the default
    audio sink yet. Once the session is up, fix it with:

      bash /root/tv-kiosk.sh --audio
EOF
}

main "$@"
