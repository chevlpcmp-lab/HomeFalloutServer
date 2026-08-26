/* Overseer front-end. Server-rendered pages; this file adds the held-button
   behaviour, the volume accumulator, and a light state poll. No framework — the
   repo has no build chain and is not getting one for a remote control. */

"use strict";

const $ = (sel, el = document) => el.querySelector(sel);
const $$ = (sel, el = document) => [...el.querySelectorAll(sel)];
const clamp = (v, lo, hi) => Math.min(hi, Math.max(lo, v));

async function api(path, body) {
  const res = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body ?? {}),
  });
  if (res.status === 401) { location.href = "/login"; throw new Error("signed out"); }
  if (!res.ok) throw new Error(String(res.status));
  return res.json();
}

/* The held button (BRIEF §7). A press takes a couple of hundred milliseconds to
   reach the Samsung, so the button visibly holds until the server answers rather
   than pretending it already worked. A second press while held is ignored, not
   queued, and a 2-second bail means it can never get stuck. */
async function act(btn, fn) {
  if (btn.dataset.busy) return;
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

function setIcon(btn, name) {
  const use = btn.querySelector("use");
  if (use) use.setAttribute("href", "#i-" + name);
}

function setToggle(el, on) {
  el.classList.toggle("on", on);
  el.dataset.on = on ? "1" : "0";
  el.setAttribute("aria-pressed", on ? "true" : "false");
}

/* ------------------------------------------------------------------ polling */

const PAGE = document.body.dataset.page;
let refreshTimer = 0;

/* Turning sync on or off changes what the server renders (notes, disabled
   controls), so lights pages reload rather than mirroring that logic here. */
let syncWasOn = $("#syncflag") ? $("#syncflag").dataset.on === "1" : null;

function applyState(s) {
  if (syncWasOn !== null && s.sync.on !== null && s.sync.on !== syncWasOn) {
    location.reload();
    return;
  }
  renderRemote(s);
  renderJellyfin(s.jellyfin);
  renderLights(s);
  renderLightDetail(s);
}

async function refresh() {
  try {
    const res = await fetch("/api/state", { cache: "no-store" });
    if (res.status === 401) { location.href = "/login"; return; }
    if (res.ok) applyState(await res.json());
  } catch { /* the next poll retries */ }
}

function scheduleRefresh(ms) {
  clearTimeout(refreshTimer);
  refreshTimer = setTimeout(refresh, ms);
}

if (PAGE === "remote" || PAGE === "lights") {
  refresh();
  setInterval(() => { if (document.visibilityState === "visible") refresh(); }, 5000);
  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible") refresh();
  });
}

/* ------------------------------------------------------------------ remote */

function renderRemote(s) {
  const zone = $("#tvzone");
  if (!zone) return;
  const tv = s.tv;
  const usable = tv.reachable && tv.on;
  $("#tvdot").classList.toggle("live", tv.on);
  zone.classList.toggle("dim", !usable);
  const hint = $("#tvhint");
  hint.textContent = !tv.reachable
    ? "Home Assistant isn't answering, so nothing here will work right now."
    : (tv.on ? "" : "The TV is asleep. Power will wake it.");
  hint.classList.toggle("gone", usable);
  const mute = $('[data-tv="mute"]');
  if (!mute.dataset.busy) {
    mute.disabled = !usable;
    mute.classList.toggle("on", tv.muted);
    mute.setAttribute("aria-pressed", tv.muted ? "true" : "false");
    setIcon(mute, tv.muted ? "mute" : "vol");
  }
  $("#src-jellyfin").classList.toggle("on", usable && tv.source === "HDMI");
  $("#src-tv").classList.toggle("on", usable && tv.source === "TV");
  if (volTarget === null && !volDragging) renderVol(tv.volume);
}

function mediaTime(seconds) {
  if (!Number.isFinite(seconds)) return "0:00";
  const whole = Math.max(0, Math.round(seconds));
  const hours = Math.floor(whole / 3600);
  const minutes = Math.floor((whole % 3600) / 60);
  const secs = String(whole % 60).padStart(2, "0");
  return hours ? `${hours}:${String(minutes).padStart(2, "0")}:${secs}` : `${minutes}:${secs}`;
}

function renderJellyfin(jellyfin) {
  const card = $("#jellyfin-card");
  if (!card || !jellyfin) return;
  $("#jellydot").classList.toggle("live", jellyfin.connected);
  $("#jellystatus").textContent = jellyfin.connected
    ? "TV connected"
    : (jellyfin.reachable ? "Player offline" : "Server unavailable");
  $("#jellytitle").textContent = jellyfin.title || "Nothing playing";
  $("#jellysub").textContent = jellyfin.subtitle || (
    jellyfin.state === "paused" ? "Paused" : (jellyfin.connected ? "Ready on the TV" : "Open Jellyfin on the TV")
  );

  const position = Number.isFinite(jellyfin.position) ? jellyfin.position : 0;
  const duration = Number.isFinite(jellyfin.duration) ? jellyfin.duration : 0;
  $("#jellyposition").textContent = mediaTime(position);
  $("#jellyduration").textContent = mediaTime(duration);
  $("#jellyprogress").style.width = (duration ? clamp(position / duration * 100, 0, 100) : 0) + "%";

  $$('[data-jellyfin]').forEach((btn) => { btn.disabled = !jellyfin.title; });
  const toggle = $('[data-jellyfin="play_pause"]');
  if (!toggle.dataset.busy) {
    const playing = jellyfin.state === "playing";
    setIcon(toggle, playing ? "pause" : "play");
    toggle.setAttribute("aria-label", playing ? "Pause" : "Play");
  }
}

function renderVol(level) {
  const num = $("#volnum");
  if (!num) return;
  num.textContent = level === null || level === undefined ? "—" : level;
  const pct = level ?? 0;
  $("#volfill").style.width = pct + "%";
  const thumb = $("#volthumb");
  thumb.style.left = pct + "%";
  thumb.style.display = level === null || level === undefined ? "none" : "";
  $("#voltrack").setAttribute("aria-valuenow", pct);
}

/* Volume is the one exception to the held contract: taps stack into a target
   level and are debounced into a single volume_set, so holding minus feels
   continuous instead of queuing eight round trips. The steppers still show the
   held state — they just keep accepting taps while held. */
let volTarget = null;
let volTimer = 0;
let volDragging = false;

function volSteppers() { return $$("[data-vol]"); }

function currentVol() {
  if (volTarget !== null) return volTarget;
  const n = parseInt($("#volnum").textContent, 10);
  return Number.isFinite(n) ? n : null;
}

function setVolTarget(level) {
  volTarget = clamp(Math.round(level), 0, 100);
  renderVol(volTarget);
  volSteppers().forEach((b) => b.classList.add("held"));
  clearTimeout(volTimer);
  volTimer = setTimeout(flushVol, 250);
}

async function flushVol() {
  const level = volTarget;
  try { await api("/api/tv/volume", { level }); }
  catch {
    volSteppers().forEach((b) => {
      b.classList.add("failed");
      setTimeout(() => b.classList.remove("failed"), 1200);
    });
  } finally {
    if (volTarget === level) volTarget = null;
    volSteppers().forEach((b) => b.classList.remove("held"));
    scheduleRefresh(1000);
  }
}

function nudgeVol(direction) {
  const current = currentVol();
  if (current === null) return;
  setVolTarget(current + direction);
}

volSteppers().forEach((btn) => {
  const direction = parseInt(btn.dataset.vol, 10);
  let repeat = 0;
  btn.addEventListener("click", (e) => { if (e.detail === 0) nudgeVol(direction); }); // keyboard
  btn.addEventListener("pointerdown", (e) => {
    e.preventDefault();
    nudgeVol(direction);
    repeat = setTimeout(function tick() { nudgeVol(direction); repeat = setTimeout(tick, 140); }, 420);
  });
  ["pointerup", "pointerleave", "pointercancel"].forEach((ev) =>
    btn.addEventListener(ev, () => clearTimeout(repeat)));
});

const voltrack = $("#voltrack");
if (voltrack) {
  const fromEvent = (e) => {
    const rect = voltrack.getBoundingClientRect();
    setVolTarget(((e.clientX - rect.left) / rect.width) * 100);
  };
  voltrack.addEventListener("pointerdown", (e) => {
    if (currentVol() === null) return;
    volDragging = true;
    voltrack.setPointerCapture(e.pointerId);
    fromEvent(e);
  });
  voltrack.addEventListener("pointermove", (e) => { if (volDragging) fromEvent(e); });
  ["pointerup", "pointercancel"].forEach((ev) =>
    voltrack.addEventListener(ev, () => { volDragging = false; }));
  voltrack.addEventListener("keydown", (e) => {
    const step = { ArrowLeft: -1, ArrowDown: -1, ArrowRight: 1, ArrowUp: 1 }[e.key];
    if (step) { e.preventDefault(); nudgeVol(step); }
  });
}

$$("[data-key]").forEach((btn) =>
  btn.addEventListener("click", () =>
    act(btn, () => api("/api/tv/key", { key: btn.dataset.key }))));

const powerBtn = $('[data-tv="power"]');
if (powerBtn) powerBtn.addEventListener("click", () =>
  act(powerBtn, async () => {
    await api("/api/tv/power");
    scheduleRefresh(900); // the TV takes a moment to wake or drop off the network
  }));

const muteBtn = $('[data-tv="mute"]');
if (muteBtn) muteBtn.addEventListener("click", () =>
  act(muteBtn, async () => {
    const r = await api("/api/tv/mute");
    muteBtn.classList.toggle("on", r.muted);
    muteBtn.setAttribute("aria-pressed", r.muted ? "true" : "false");
    setIcon(muteBtn, r.muted ? "mute" : "vol");
  }));

$$("[data-source]").forEach((btn) =>
  btn.addEventListener("click", () =>
    act(btn, async () => {
      await api("/api/tv/source", { source: btn.dataset.source });
      $$(".segbtn").forEach((b) => b.classList.toggle("on", b === btn));
    })));

$$('[data-jellyfin]').forEach((btn) =>
  btn.addEventListener("click", () =>
    act(btn, async () => {
      await api("/api/jellyfin/playback", { action: btn.dataset.jellyfin });
      scheduleRefresh(350);
    })));

/* ------------------------------------------------------------------ lights */

function renderLights(s) {
  if (!$("[data-room-card]")) return;
  const summary = $("#summary");
  if (summary) summary.textContent = s.summary;
  for (const room of s.rooms) {
    const roomSwitch = $(`[data-room="${room.key}"]`);
    if (roomSwitch && !roomSwitch.dataset.busy) setToggle(roomSwitch, room.on);
    for (const light of room.lights) {
      const orb = $(`[data-light="${light.slug}"]`);
      if (orb && !orb.dataset.busy) setToggle(orb, light.on);
      const sub = $(`[data-sub="${light.slug}"]`);
      if (sub) sub.textContent = light.sub;
    }
  }
}

$$("[data-light]").forEach((btn) =>
  btn.addEventListener("click", () =>
    act(btn, async () => {
      const r = await api("/api/lights/" + btn.dataset.light, { on: btn.dataset.on !== "1" });
      setToggle(btn, r.on);
      scheduleRefresh(1200);
    })));

/* :not([data-perm]) — the People page reuses data-room on its permission
   switches, which post somewhere else entirely. */
$$("[data-room]:not([data-perm])").forEach((btn) =>
  btn.addEventListener("click", () =>
    act(btn, async () => {
      const r = await api("/api/rooms/" + btn.dataset.room, { on: btn.dataset.on !== "1" });
      setToggle(btn, r.on);
      scheduleRefresh(1200);
    })));

$$("[data-sync]").forEach((btn) =>
  btn.addEventListener("click", () =>
    act(btn, async () => {
      await api("/api/sync", { on: btn.dataset.on !== "1" });
      location.reload(); // what the page shows for the strip changes server-side
    })));

$$("[data-sync-off]").forEach((btn) =>
  btn.addEventListener("click", () =>
    act(btn, async () => {
      await api("/api/sync", { on: false });
      location.reload();
    })));

$$(".chip2[data-chip]").forEach((chip) =>
  chip.addEventListener("click", () => {
    $$(".chip2").forEach((c) => c.classList.toggle("on", c === chip));
    const key = chip.dataset.chip;
    $$("[data-room-card]").forEach((card) => {
      card.hidden = key !== "all" && card.dataset.roomCard !== key;
    });
  }));

/* ------------------------------------------------------------------ one light */

function renderLightDetail(s) {
  const track = $("#britrack");
  if (!track || briDragging || briTarget !== null) return;
  const slug = track.dataset.bri;
  for (const room of s.rooms) {
    for (const light of room.lights) {
      if (light.slug !== slug) continue;
      const pct = light.on ? light.brightness ?? null : 0;
      $("#brifill").style.width = (pct ?? 0) + "%";
      track.setAttribute("aria-valuenow", pct ?? 0);
      $("#bripct").textContent =
        light.on ? (light.brightness !== null ? light.brightness + "%" : "On") : "Off";
    }
  }
}

let briTarget = null;
let briTimer = 0;
let briDragging = false;

const britrack = $("#britrack");
if (britrack) {
  const setBriTarget = (pct) => {
    briTarget = clamp(Math.round(pct), 1, 100);
    $("#brifill").style.width = briTarget + "%";
    $("#bripct").textContent = briTarget + "%";
    britrack.setAttribute("aria-valuenow", briTarget);
    clearTimeout(briTimer);
    briTimer = setTimeout(flushBri, 250);
  };
  const flushBri = async () => {
    const pct = briTarget;
    try { await api("/api/lights/" + britrack.dataset.bri, { brightness: pct }); }
    catch {
      britrack.classList.add("failed");
      setTimeout(() => britrack.classList.remove("failed"), 1200);
    } finally {
      if (briTarget === pct) briTarget = null;
      scheduleRefresh(1000);
    }
  };
  const fromEvent = (e) => {
    const rect = britrack.getBoundingClientRect();
    setBriTarget(((e.clientX - rect.left) / rect.width) * 100);
  };
  britrack.addEventListener("pointerdown", (e) => {
    briDragging = true;
    britrack.setPointerCapture(e.pointerId);
    fromEvent(e);
  });
  britrack.addEventListener("pointermove", (e) => { if (briDragging) fromEvent(e); });
  ["pointerup", "pointercancel"].forEach((ev) =>
    britrack.addEventListener(ev, () => { briDragging = false; }));
  britrack.addEventListener("keydown", (e) => {
    const step = { ArrowLeft: -5, ArrowDown: -5, ArrowRight: 5, ArrowUp: 5 }[e.key];
    if (step) {
      e.preventDefault();
      setBriTarget((briTarget ?? (parseInt(britrack.getAttribute("aria-valuenow"), 10) || 0)) + step);
    }
  });
}

$$("[data-light-off]").forEach((btn) =>
  btn.addEventListener("click", () =>
    act(btn, async () => {
      await api("/api/lights/" + btn.dataset.lightOff, { on: false });
      scheduleRefresh(800);
    })));

$$("[data-light-color]").forEach((btn) =>
  btn.addEventListener("click", () =>
    act(btn, async () => {
      await api("/api/lights/" + btn.dataset.lightColor, { color: btn.dataset.color });
      scheduleRefresh(800);
    })));

/* ------------------------------------------------------------------ people */

$$("[data-perm]").forEach((btn) =>
  btn.addEventListener("click", () =>
    act(btn, async () => {
      const r = await api(`/people/${btn.dataset.user}/rooms`, {
        room: btn.dataset.room,
        granted: btn.dataset.on !== "1",
      });
      setToggle(btn, r.granted);
    })));
