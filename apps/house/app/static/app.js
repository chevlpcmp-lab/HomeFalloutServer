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
  if (!res.ok) {
    let message = String(res.status);
    try { message = (await res.json()).detail || message; } catch { /* keep status */ }
    throw new Error(message);
  }
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
let currentJellyfin = null;
let playbackPending = false;
let seekDragging = false;
let seekItemId = null;
let seekHoldUntil = 0;
let nextEpisodeItem = null;
let nextEpisodeRequest = 0;
let historyUserAvailable = null;

/* Turning sync on or off changes what the server renders (notes, disabled
   controls), so lights pages reload rather than mirroring that logic here. */
let syncWasOn = $("#syncflag") ? $("#syncflag").dataset.on === "1" : null;

function applyState(s) {
  if (syncWasOn !== null && s.sync.on !== null && s.sync.on !== syncWasOn) {
    location.reload();
    return;
  }
  renderRemote(s);
  renderConnection(s);
  renderJellyfin(s.jellyfin);
  renderLights(s);
  renderLightDetail(s);
}

async function refresh() {
  try {
    const res = await fetch("/api/state", { cache: "no-store" });
    if (res.status === 401) { location.href = "/login"; return; }
    if (res.ok) applyState(await res.json());
  } catch {
    const note = $("#player-connection");
    if (note) {
      note.hidden = false;
      $("#connection-title").textContent = "Connection lost";
      $("#connection-help").textContent = "Check your connection to Overseer, then try again.";
      $("#player-connect").textContent = "Check again";
      $("#player-connect").dataset.recovery = "check";
    }
  }
}

function scheduleRefresh(ms) {
  clearTimeout(refreshTimer);
  refreshTimer = setTimeout(refresh, ms);
}

if (PAGE === "remote" || PAGE === "jellyfin" || PAGE === "lights") {
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

function renderConnection(s) {
  const note = $("#player-connection");
  if (!note || !s.jellyfin) return;
  const player = s.jellyfin;
  const tv = s.tv || {};
  const button = $("#player-connect");
  let title = "Ready to play";
  let help = "";
  let recovery = "check";
  let action = "Check again";
  if (!player.reachable) {
    title = "Jellyfin is unavailable";
    help = "Overseer can’t reach your media server. Check again in a moment.";
  } else if (tv.reachable && !tv.on) {
    title = "The TV is asleep";
    help = "Wake it and switch to the Jellyfin input to start watching.";
    recovery = "connect"; action = "Wake TV";
  } else if (!player.connected) {
    title = "Jellyfin player is offline";
    help = "Switch to the Jellyfin input. If it stays offline, open Jellyfin on the TV, then check again.";
    if (tv.reachable && tv.source !== "HDMI") { recovery = "connect"; action = "Open TV input"; }
  } else if (!tv.reachable) {
    title = "Player connected; TV status unavailable";
    help = "Playback controls are connected. TV power and input status can’t be checked right now.";
  } else if (tv.source !== "HDMI") {
    title = "The TV is on another input";
    help = "Switch to Jellyfin to see what’s playing.";
    recovery = "connect"; action = "Open TV input";
  }
  note.hidden = !help;
  $("#connection-title").textContent = title;
  if (!button.dataset.busy) {
    $("#connection-help").textContent = help;
    button.textContent = action;
    button.dataset.recovery = recovery;
  }
  $("#jellystatus").textContent = title;
}

function renderJellyfin(jellyfin) {
  const card = $("#jellyfin-card");
  if (!card || !jellyfin) return;
  currentJellyfin = jellyfin;
  $("#jellydot").classList.toggle("live", jellyfin.connected);
  $("#jellytitle").textContent = jellyfin.title || "Nothing playing";
  $("#jellysub").textContent = jellyfin.subtitle || (
    jellyfin.state === "paused" ? "Paused" : (
      jellyfin.title ? "Playing on the TV" : (jellyfin.connected ? "Choose a movie or TV show below" : "Open Jellyfin on the TV")
    )
  );
  const stateLabel = $("#jelly-state-label");
  if (stateLabel) stateLabel.textContent = jellyfin.state;

  const poster = $("#jelly-poster");
  if (poster) {
    if (jellyfin.item_id) {
      if (poster.dataset.itemId !== jellyfin.item_id) {
        poster.dataset.itemId = jellyfin.item_id;
        poster.src = `/api/jellyfin/movies/${jellyfin.item_id}/artwork`;
      }
      if (poster.complete && poster.currentSrc && !poster.naturalWidth) poster.dataset.failedItem = jellyfin.item_id;
      poster.hidden = poster.dataset.failedItem === jellyfin.item_id;
    } else {
      poster.hidden = true;
      poster.removeAttribute("src");
      delete poster.dataset.itemId;
    }
  }

  const position = Number.isFinite(jellyfin.position) ? jellyfin.position : 0;
  const duration = Number.isFinite(jellyfin.duration) ? jellyfin.duration : 0;
  const seek = $("#jelly-seek");
  if (!seekDragging && Date.now() >= seekHoldUntil) {
    $("#jellyposition").textContent = mediaTime(position);
    seek.max = String(Math.max(1, duration));
    seek.value = String(clamp(position, 0, duration));
    seek.setAttribute("aria-valuetext", `${mediaTime(position)} of ${mediaTime(duration)}`);
  }
  if (seekDragging && seekItemId !== jellyfin.item_id) {
    seekDragging = false;
    seekItemId = null;
    $("#seek-status").textContent = "The title changed. Choose a position in the current title.";
  }
  seek.disabled = !jellyfin.connected || !jellyfin.item_id || !jellyfin.can_seek || !duration;
  $("#jellyduration").textContent = mediaTime(duration);

  $$('[data-jellyfin]').forEach((btn) => { btn.disabled = !jellyfin.title; });
  const toggle = $('[data-jellyfin="play_pause"]');
  if (!toggle.dataset.busy) {
    const playing = jellyfin.state === "playing";
    setIcon(toggle, playing ? "pause" : "play");
    toggle.setAttribute("aria-label", playing ? "Pause" : "Play");
  }

  const streams = $("#stream-controls");
  if (streams) streams.hidden = !jellyfin.title;
  renderStreamSelect($("#audio-select"), jellyfin.audio_streams || [], jellyfin.audio_stream_index);
  renderStreamSelect($("#subtitle-select"), jellyfin.subtitle_streams || [], jellyfin.subtitle_stream_index, true);
  if (nextEpisodeItem !== jellyfin.item_id) {
    nextEpisodeItem = jellyfin.item_id;
    loadNextEpisode(jellyfin.item_id);
  }
  const context = JSON.stringify([jellyfin.user_context_available, jellyfin.user_name]);
  if (historyUserAvailable !== context) {
    historyUserAvailable = context;
    const label = $("#history-account");
    if (label) label.textContent = jellyfin.user_name
      ? `For ${jellyfin.user_name} on the TV` : "For the TV’s Jellyfin account";
    loadPersonalLibrary();
  }
}

function renderStreamSelect(select, choices, selected, allowOff = false) {
  if (!select || select.dataset.busy) return;
  const signature = JSON.stringify(choices);
  if (select.dataset.choices !== signature) {
    const options = [];
    if (allowOff) options.push(new Option("Off", "-1"));
    choices.forEach((choice) => options.push(new Option(choice.label, String(choice.index))));
    select.replaceChildren(...options);
    select.dataset.choices = signature;
  }
  const wanted = String(selected ?? (allowOff ? -1 : ""));
  if ([...select.options].some((option) => option.value === wanted)) select.value = wanted;
  select.disabled = !allowOff && choices.length === 0;
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

async function libraryGet(path) {
  const res = await fetch(path, { cache: "no-store", signal: AbortSignal.timeout(5000) });
  if (res.status === 401) { location.href = "/login"; throw new Error("signed out"); }
  if (!res.ok) {
    let message = "Your library is unavailable right now. Try again.";
    try { message = (await res.json()).detail || message; } catch { /* use fallback */ }
    throw new Error(message);
  }
  return res.json();
}

function mediaError(error) {
  const message = error?.message || "Something went wrong.";
  return /Jellyfin answered|unreachable:|\/Sessions|\/Items/.test(message)
    ? "Jellyfin didn’t answer that request. Check the player connection and try again." : message;
}

async function startMedia(item, status, restart) {
  if (item.playable === false || !/^[a-f0-9]{32}$/i.test(String(item.id || ""))) return;
  if (playbackPending) return;
  playbackPending = true;
  const feedback = $("#playback-feedback");
  const report = (message, state) => {
    feedback.hidden = false;
    feedback.dataset.state = state;
    feedback.textContent = message;
    if (status) status.textContent = message;
  };
  $$('[data-start]').forEach(button => { button.disabled = true; });
  report(`Starting ${item.title} on the TV…`, "pending");
  try {
    const response = await api("/api/jellyfin/play", { item_id: item.id, restart });
    const expected = response.start_seconds || 0;
    const deadline = Date.now() + 12000;
    let confirmed = false;
    while (Date.now() < deadline) {
      try {
        const state = await libraryGet("/api/state");
        applyState(state);
        const player = state.jellyfin;
        if (player?.connected && player.item_id === item.id && player.state === "playing"
            && Number.isFinite(player.position) && Math.abs(player.position - expected) <= 20) {
          confirmed = true;
          break;
        }
      } catch { /* Keep checking; never repeat the playback command. */ }
      await new Promise(resolve => setTimeout(resolve, 700));
    }
    if (!confirmed) {
      report("The TV hasn’t confirmed playback. Check the TV before trying again.", "error");
      return;
    }
    report(response.tv_ready
      ? `Playing ${item.title} on the TV.`
      : `${item.title} is playing. Switch the TV to Jellyfin if the picture isn’t visible.`, "success");
    loadPersonalLibrary();
  } catch (error) {
    report(`Couldn’t start ${item.title}. ${mediaError(error)}`, "error");
  } finally {
    playbackPending = false;
    $$('.movie-result').forEach(row => { if (row._mediaItem) renderAvailability(row, row._mediaItem); });
  }
}

async function loadNextEpisode(itemId) {
  const target = $("#next-episode");
  if (!target) return;
  const ticket = ++nextEpisodeRequest;
  target.hidden = true;
  target.replaceChildren();
  if (!itemId) return;
  try {
    const response = await libraryGet("/api/jellyfin/next-episode");
    if (ticket !== nextEpisodeRequest || !response.item) return;
    const label = document.createElement("strong");
    label.textContent = "Next episode";
    const row = mediaResult(response.item, $("#playback-feedback"));
    const button = row.querySelector(".movie-play");
    if (button) button.textContent = "Play next";
    target.append(label, row);
    target.hidden = false;
  } catch {
    if (ticket === nextEpisodeRequest) {
      const retry = document.createElement("button");
      retry.type = "button";
      retry.className = "library-back";
      retry.textContent = "Check next episode";
      retry.addEventListener("click", () => loadNextEpisode(itemId));
      target.append(retry);
      target.hidden = false;
    }
  }
}

let historyRequest = 0;
async function loadPersonalLibrary() {
  if (!$("#continue-results")) return;
  const ticket = ++historyRequest;
  const sections = [
    ["continue", "No saved progress yet. Start a movie or episode from your library."],
    ["next-up", "No next episodes yet. Your watched shows will appear here."],
  ];
  await Promise.all(sections.map(async ([kind, empty]) => {
    const status = $(`#${kind}-status`);
    const results = $(`#${kind}-results`);
    results.replaceChildren();
    status.textContent = "Loading…";
    try {
      const response = await libraryGet(`/api/jellyfin/${kind}`);
      if (ticket !== historyRequest) return;
      const items = response.items || [];
      status.textContent = items.length ? "" : (
        currentJellyfin?.user_context_available === false
          ? "Sign into Jellyfin on the TV to see its account’s watch history." : empty);
      results.replaceChildren(...items.slice(0, 3).map(item => mediaResult(item, status)));
      if (items.length > 3) {
        const more = document.createElement("button");
        more.type = "button";
        more.className = "library-back library-more";
        more.textContent = `Show all ${items.length}`;
        more.addEventListener("click", () => {
          results.replaceChildren(...items.map(item => mediaResult(item, status)));
        });
        results.append(more);
      }
    } catch (error) {
      if (ticket !== historyRequest) return;
      status.textContent = `Couldn’t load this list. ${mediaError(error)}`;
      const retry = document.createElement("button");
      retry.type = "button";
      retry.className = "library-back";
      retry.textContent = "Try again";
      retry.addEventListener("click", loadPersonalLibrary);
      results.append(retry);
    }
  }));
}

function mediaResult(item, status) {
  const row = document.createElement("article");
  row.className = "movie-result";
  const isSeries = item.type === "Series";
  const isEpisode = item.type === "Episode";
  row.classList.toggle("episode-result", isEpisode);
  row.dataset.itemId = item.id || "";
  row.dataset.mediaKey = item.media_key || item.id || "";
  row._mediaItem = item;

  if (!isEpisode) {
    const poster = document.createElement(item.has_image ? "img" : "div");
    poster.className = "movie-poster";
    if (item.has_image) {
      poster.alt = "";
      poster.loading = "lazy";
      poster.src = `/api/jellyfin/movies/${encodeURIComponent(item.id)}/artwork`;
      poster.addEventListener("error", () => {
        const fallback = document.createElement("div");
        fallback.className = "movie-poster";
        fallback.textContent = "No art";
        poster.replaceWith(fallback);
      }, { once: true });
    } else poster.textContent = "No art";
    row.append(poster);
  }

  const info = document.createElement("div");
  info.className = "movie-info";
  const title = document.createElement("span");
  title.className = "movie-title";
  title.textContent = isEpisode && item.series_title
    ? `${item.series_title} — ${item.title}` : item.title;
  const details = document.createElement("span");
  details.className = "movie-year num";
  details.textContent = isEpisode
    ? `Season ${item.season_number ?? "?"} · Episode ${item.episode_number ?? "?"}`
    : [isSeries ? "TV show" : "Movie", item.year, item.played ? "Watched" : null,
      item.rating == null ? null : `${item.rating} rating`].filter(Boolean).join(" · ");
  info.append(title, details);

  const availabilityEl = document.createElement("span");
  availabilityEl.className = "media-availability";
  info.append(availabilityEl);

  const actions = document.createElement("div");
  actions.className = "media-actions";
  const action = document.createElement("button");
  action.type = "button";
  action.className = "movie-play";
  const playable = isSeries || (item.playable !== false && /^[a-f0-9]{32}$/i.test(String(item.id || "")));
  const resume = playable && !isSeries && Number.isFinite(item.resume_seconds) && item.resume_seconds > 0;
  action.textContent = isSeries ? "Episodes" : (resume ? `Resume ${mediaTime(item.resume_seconds)}` : "Play on TV");
  action.setAttribute("aria-label", isSeries
    ? `Choose an episode of ${item.title}` : `${resume ? "Resume" : "Play"} ${item.title} on the TV`);
  if (!isSeries) {
    action.dataset.start = "1";
    action.disabled = playbackPending || !playable;
  }
  action.addEventListener("click", () => {
    const current = row._mediaItem || item;
    if (isSeries) openSeries(current, action);
    else startMedia(current, status, false);
  });
  actions.append(action);
  if (resume) {
    const restart = document.createElement("button");
    restart.type = "button";
    restart.className = "media-restart";
    restart.dataset.start = "1";
    restart.disabled = playbackPending;
    restart.textContent = "Start over";
    restart.setAttribute("aria-label", `Start ${item.title} from the beginning on the TV`);
    restart.addEventListener("click", () => startMedia(row._mediaItem || item, status, true));
    actions.append(restart);
  }
  if (!isSeries && !playable) action.hidden = true;
  row.append(info, actions);
  renderAvailability(row, item);
  return row;
}

function humanRate(value) {
  if (!Number.isFinite(value) || value <= 0) return "";
  const units = ["B/s", "KB/s", "MB/s", "GB/s"];
  let n = value, i = 0;
  while (n >= 1000 && i < units.length - 1) { n /= 1000; i += 1; }
  return `${n >= 10 ? Math.round(n) : n.toFixed(1)} ${units[i]}`;
}

function availabilityText(a) {
  const state = a?.state || "unknown";
  if (state === "ready") return "Ready to watch";
  if (state === "downloading") return `Downloading${Number.isFinite(a.progress) ? ` ${Math.round(a.progress)}%` : ""}`;
  if (state === "importing") return "Importing";
  if (state === "missing") return a.message || "Missing";
  if (state === "paused") return "Paused";
  if (state === "stalled") return "Stalled";
  if (state === "error") return a.message || "Unavailable";
  return a.message || "Availability unknown";
}

function itemAvailability(item) {
  if (item.availability) return item.availability;
  return /^[a-f0-9]{32}$/i.test(String(item.id || "")) ? { state: "ready" } : { state: "unknown" };
}

function renderAvailability(row, item) {
  const a = itemAvailability(item);
  const el = row.querySelector(".media-availability");
  if (!el) return;
  el.textContent = availabilityText(a);
  const extra = [humanRate(a.speed_bps), Number.isFinite(a.eta_seconds) ? `ETA ${mediaTime(a.eta_seconds)}` : ""].filter(Boolean);
  if (extra.length) el.textContent += ` · ${extra.join(" · ")}`;
  let progress = row.querySelector("progress.media-progress");
  if (["downloading", "importing"].includes(a.state) && Number.isFinite(a.progress)) {
    if (!progress) { progress = document.createElement("progress"); progress.className = "media-progress"; el.after(progress); }
    progress.max = 100; progress.value = clamp(a.progress, 0, 100); progress.setAttribute("aria-label", `${Math.round(a.progress)} percent complete`);
  } else if (progress) progress.remove();
  const playable = item.type === "Series" || (item.playable !== false && /^[a-f0-9]{32}$/i.test(String(item.id || "")));
  const action = row.querySelector(".movie-play");
  if (action && item.type !== "Series") {
    action.disabled = playbackPending || !playable;
    action.hidden = !playable;
    action.textContent = playable ? (Number.isFinite(item.resume_seconds) && item.resume_seconds > 0 ? `Resume ${mediaTime(item.resume_seconds)}` : "Play on TV") : "Play on TV";
    const restart = row.querySelector(".media-restart");
    if (restart) restart.hidden = !playable;
  }
}

let activeSeries = null;
let seriesTrigger = null;
let libraryRequest = 0;
let episodeRequest = 0;
let libraryQuery = "";
let availabilityRefreshInFlight = false;
let availabilityRefreshTicket = 0;

function updateVisibleItems(items) {
  (items || []).forEach(item => {
    const key = String(item.media_key || item.id || "");
    $$('.movie-result').filter(candidate => candidate.dataset.mediaKey === key).forEach(row => {
      row._mediaItem = item;
      row.dataset.itemId = item.id || "";
      renderAvailability(row, item);
    });
  });
}

function renderAvailabilityWarning(response) {
  const notice = $("#availability-warning");
  if (!notice) return;
  notice.textContent = response.availability_warning || "";
  notice.hidden = !notice.textContent;
}

async function refreshVisibleAvailability() {
  if (document.visibilityState !== "visible" || availabilityRefreshInFlight) return;
  const ticket = ++availabilityRefreshTicket;
  const queryAtStart = libraryQuery;
  const seriesAtStart = activeSeries;
  const seasonAtStart = $("#series-season")?.value || "";
  availabilityRefreshInFlight = true;
  try {
    const response = queryAtStart
      ? await api("/api/jellyfin/search", { query: queryAtStart })
      : await libraryGet("/api/jellyfin/recent");
    if (ticket !== availabilityRefreshTicket || queryAtStart !== libraryQuery) return;
    renderAvailabilityWarning(response);
    updateVisibleItems(response.items);
    if (seriesAtStart && seasonAtStart && activeSeries === seriesAtStart && seasonAtStart === $("#series-season")?.value) {
      const episodes = await libraryGet(`/api/jellyfin/series/${encodeURIComponent(seriesAtStart.id)}/episodes?season_id=${encodeURIComponent(seasonAtStart)}`);
      if (ticket !== availabilityRefreshTicket || activeSeries !== seriesAtStart || seasonAtStart !== $("#series-season")?.value) return;
      updateVisibleItems((episodes.episodes || []).map(item => ({ ...item, type: "Episode" })));
    }
    for (const kind of ["continue", "next-up"]) {
      const history = await libraryGet(`/api/jellyfin/${kind}`);
      if (ticket !== availabilityRefreshTicket) return;
      updateVisibleItems(history.items);
    }
  } catch { /* regular state refresh and the next poll will retry */ }
  finally { availabilityRefreshInFlight = false; }
}

function closeSeries() {
  activeSeries = null;
  episodeRequest += 1;
  $("#series-browser").hidden = true;
  $("#movie-results").hidden = false;
}

async function loadEpisodes() {
  const ticket = ++episodeRequest;
  const series = activeSeries;
  const seasonId = $("#series-season").value;
  const status = $("#episode-status");
  const results = $("#episode-results");
  results.replaceChildren();
  if (!series || !seasonId) return;
  status.textContent = "Loading episodes…";
  try {
    const response = await libraryGet(
      `/api/jellyfin/series/${encodeURIComponent(series.id)}/episodes?season_id=${encodeURIComponent(seasonId)}`);
    if (ticket !== episodeRequest) return;
    results.replaceChildren(...response.episodes.map(item => mediaResult({ ...item, type: "Episode" }, status)));
    status.textContent = response.episodes.length
      ? "Choose an episode to play on the TV." : "No episodes in this season are in your library.";
  } catch (error) {
    if (ticket === episodeRequest) status.textContent = mediaError(error);
  }
}

async function openSeries(series, trigger) {
  const ticket = ++libraryRequest;
  episodeRequest += 1;
  activeSeries = series;
  seriesTrigger = trigger;
  $("#movie-results").hidden = true;
  $("#series-browser").hidden = false;
  $("#series-title").textContent = series.title;
  $("#series-title").focus();
  const select = $("#series-season");
  const status = $("#episode-status");
  select.replaceChildren();
  select.disabled = true;
  $("#episode-results").replaceChildren();
  status.textContent = "Loading seasons…";
  try {
    const response = await libraryGet(`/api/jellyfin/series/${encodeURIComponent(series.id)}/seasons`);
    if (ticket !== libraryRequest || activeSeries !== series) return;
    response.seasons.forEach(season => {
      const option = document.createElement("option");
      option.value = season.id;
      option.textContent = season.title || (season.number === 0 ? "Specials" : `Season ${season.number}`);
      select.append(option);
    });
    const firstSeason = response.seasons.find(season => season.number > 0);
    if (firstSeason) select.value = firstSeason.id;
    select.disabled = !response.seasons.length;
    status.textContent = response.seasons.length ? "" : "No seasons are in your library yet.";
    if (response.seasons.length) await loadEpisodes();
  } catch (error) {
    if (ticket === libraryRequest && activeSeries === series) status.textContent = mediaError(error);
  }
}

const movieSearchForm = $("#movie-search-form");
if (movieSearchForm) {
  const status = $("#movie-search-status");
  const results = $("#movie-results");
  const heading = $("#movie-results-title");
  async function loadLibrary(query = "") {
    libraryQuery = query;
    const ticket = ++libraryRequest;
    closeSeries();
    status.textContent = query ? "Searching…" : "Loading your library…";
    results.replaceChildren();
    try {
      const response = query
        ? await api("/api/jellyfin/search", { query })
        : await libraryGet("/api/jellyfin/recent");
      if (ticket !== libraryRequest) return;
      renderAvailabilityWarning(response);
      results.replaceChildren(...response.items.map(item => mediaResult(item, status)));
      heading.textContent = query ? `Results for “${query}”` : "Recently added";
      status.textContent = response.items.length
        ? (query ? `${response.items.length} results. Choose a movie or TV show.` : "Choose a movie or TV show to play.")
        : (query ? "No matching movies or TV shows in your library." : "No movies or TV shows in your library yet.");
    } catch (error) {
      if (ticket === libraryRequest) status.textContent = mediaError(error);
    }
  }
  movieSearchForm.addEventListener("submit", event => {
    event.preventDefault();
    const query = $("#movie-query").value.trim();
    if (query && query.length < 2) { status.textContent = "Enter at least two characters."; return; }
    loadLibrary(query);
  });
  $("#series-back").addEventListener("click", () => {
    libraryRequest += 1;
    closeSeries();
    seriesTrigger?.focus();
  });
  $("#series-season").addEventListener("change", loadEpisodes);
  $("#movie-query").addEventListener("search", event => {
    if (!event.target.value) loadLibrary();
  });
  loadLibrary();
  setInterval(refreshVisibleAvailability, 10000);
}

const jellyPoster = $("#jelly-poster");
if (jellyPoster) jellyPoster.addEventListener("error", () => {
  jellyPoster.dataset.failedItem = jellyPoster.dataset.itemId || "";
  jellyPoster.hidden = true;
});

const playerConnect = $("#player-connect");
if (playerConnect) playerConnect.addEventListener("click", () => act(playerConnect, async () => {
  try {
    if (playerConnect.dataset.recovery === "connect") {
      const response = await api("/api/jellyfin/connect");
      if (!response.tv_ready) throw new Error("The TV didn’t answer. Use its remote to switch to Jellyfin, then check again.");
    }
    await refresh();
  } catch (error) {
    $("#connection-help").textContent = mediaError(error);
    throw error;
  }
}));

const seekControl = $("#jelly-seek");
if (seekControl) {
  const beginSeek = () => {
    if (seekControl.disabled) return;
    if (!seekDragging) seekItemId = currentJellyfin?.item_id;
    seekDragging = true;
  };
  seekControl.addEventListener("pointerdown", beginSeek);
  seekControl.addEventListener("keydown", event => {
    if (["ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown", "Home", "End", "PageUp", "PageDown"].includes(event.key)) beginSeek();
  });
  seekControl.addEventListener("input", () => {
    beginSeek();
    $("#jellyposition").textContent = mediaTime(Number(seekControl.value));
    seekControl.setAttribute("aria-valuetext", `${mediaTime(Number(seekControl.value))} of ${mediaTime(Number(seekControl.max))}`);
  });
  const cancelSeek = () => { seekDragging = false; seekItemId = null; };
  seekControl.addEventListener("pointerup", () => setTimeout(cancelSeek, 0));
  seekControl.addEventListener("blur", cancelSeek);
  seekControl.addEventListener("pointercancel", () => { cancelSeek(); refresh(); });
  seekControl.addEventListener("change", async () => {
    const itemId = seekItemId;
    const seconds = Number(seekControl.value);
    seekDragging = false;
    seekItemId = null;
    if (!itemId) return;
    seekHoldUntil = Date.now() + 1500;
    $("#seek-status").textContent = `Seeking to ${mediaTime(seconds)}…`;
    try {
      await api("/api/jellyfin/seek", { item_id: itemId, seconds });
      $("#seek-status").textContent = `Seek requested at ${mediaTime(seconds)}.`;
      scheduleRefresh(1600);
    } catch (error) {
      seekHoldUntil = 0;
      $("#seek-status").textContent = mediaError(error);
      refresh();
    }
  });
}

const audioSelect = $("#audio-select");
if (audioSelect) audioSelect.addEventListener("change", () =>
  act(audioSelect, async () => {
    await api("/api/jellyfin/audio", { index: Number(audioSelect.value) });
    scheduleRefresh(500);
  }));

const subtitleSelect = $("#subtitle-select");
if (subtitleSelect) subtitleSelect.addEventListener("change", () =>
  act(subtitleSelect, async () => {
    await api("/api/jellyfin/subtitles", { index: Number(subtitleSelect.value) });
    scheduleRefresh(500);
  }));

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
