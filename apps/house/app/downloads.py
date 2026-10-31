"""Read-only tracked-library availability from Radarr, Sonarr and qBittorrent."""
from __future__ import annotations

import asyncio
import math
import time
from typing import Any

import httpx

from .jellyfin import JellyfinError, JellyfinItemUnavailable

TTL = 10.0
_IDS = ("tmdb", "tvdb", "imdb")


def _ids(row: dict) -> dict[str, str]:
    result = {str(k).lower(): str(v) for k, v in (row.get("provider_ids") or row.get("ProviderIds") or {}).items()
              if str(k).lower() in _IDS and v not in (None, "", 0, "0")}
    for name in _IDS:
        if row.get(name + "Id") not in (None, "", 0, "0"):
            result[name] = str(row[name + "Id"])
    return result


def _matches(a: dict, b: dict) -> bool:
    left, right = _ids(a), _ids(b)
    return any(left.get(k) and left[k] == right.get(k) for k in _IDS)


def _availability(state: str, progress: Any = None, speed: Any = None, eta: Any = None, message: Any = "") -> dict:
    progress = float(progress) if isinstance(progress, (int, float)) and math.isfinite(progress) else None
    if progress is not None:
        progress = progress * 100 if 0 <= progress <= 1 else progress
        progress = max(0.0, min(100.0, progress))
    speed = float(speed) if isinstance(speed, (int, float)) and math.isfinite(speed) and speed >= 0 else None
    eta = int(eta) if isinstance(eta, (int, float)) and math.isfinite(eta) and eta >= 0 else None
    return {"state": state if state in {"ready", "downloading", "importing", "missing", "unknown", "paused", "stalled", "error"} else "unknown",
            "progress": progress, "speed_bps": speed, "eta_seconds": eta, "message": str(message or "")[:240]}


class Downloads:
    def __init__(self, radarr_url: str, radarr_key: str, sonarr_url: str, sonarr_key: str,
                 qbit_url: str = "", qbit_username: str = "", qbit_password: str = "",
                 transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.arr = {"radarr": (radarr_url.rstrip("/"), radarr_key), "sonarr": (sonarr_url.rstrip("/"), sonarr_key)}
        self.qbit = (qbit_url.rstrip("/"), qbit_username, qbit_password)
        self.client = httpx.AsyncClient(timeout=3, transport=transport)
        self.cache: dict | None = None
        self.cached_at = 0.0
        self.lock = asyncio.Lock()
        self.episode_cache: dict[int, tuple[float, list[dict], bool]] = {}
        self.episode_lock = asyncio.Lock()
        self.qbit_authenticated = False

    async def aclose(self) -> None:
        await self.client.aclose()

    async def _arr_get(self, service: str, path: str, **params: Any) -> Any:
        base, key = self.arr[service]
        if not base or not key: raise RuntimeError("not configured")
        r = await self.client.get(f"{base}/api/v3/{path}", headers={"X-Api-Key": key}, params=params)
        r.raise_for_status(); return r.json()

    async def _service(self, service: str) -> tuple[list[dict], list[dict], bool, bool]:
        rows: list[dict] = []; records: list[dict] = []; catalog_ok = queue_ok = False
        try:
            rows = list(await self._arr_get(service, "movie" if service == "radarr" else "series") or []); catalog_ok = True
        except (httpx.HTTPError, RuntimeError, ValueError): pass
        try:
            queue = await self._arr_get(service, "queue", page=1, pageSize=1000)
            raw = (queue or {}).get("records", []) if isinstance(queue, dict) else queue
            records = list(raw) if isinstance(raw, list) else []; queue_ok = True
        except (httpx.HTTPError, RuntimeError, ValueError): pass
        return rows, records, catalog_ok, queue_ok

    async def _qbit(self) -> tuple[dict[str, dict], bool]:
        url, user, password = self.qbit
        if not url: return {}, True
        try:
            origin = str(httpx.URL(url).copy_with(path="/", query=None, fragment=None))
            headers = {"Referer": origin}
            if (user or password) and not self.qbit_authenticated:
                r = await self.client.post(url + "/api/v2/auth/login", data={"username": user, "password": password}, headers=headers); r.raise_for_status()
                self.qbit_authenticated = True
            r = await self.client.get(url + "/api/v2/torrents/info", headers=headers)
            if r.status_code in (401, 403) and (user or password):
                self.qbit_authenticated = False
                r = await self.client.post(url + "/api/v2/auth/login", data={"username": user, "password": password}, headers=headers); r.raise_for_status()
                self.qbit_authenticated = True
                r = await self.client.get(url + "/api/v2/torrents/info", headers=headers)
            r.raise_for_status()
            return {str(x.get("hash")).lower(): x for x in r.json() if x.get("hash")}, True
        except (httpx.HTTPError, ValueError): return {}, False

    async def snapshot(self) -> dict:
        if self.cache and time.monotonic() - self.cached_at < TTL: return self.cache
        async with self.lock:
            if self.cache and time.monotonic() - self.cached_at < TTL: return self.cache
            (movies, rq, movies_ok, rq_ok), (series, sq, series_ok, sq_ok), (torrents, qbit_ok) = await asyncio.gather(self._service("radarr"), self._service("sonarr"), self._qbit())
            previous = self.cache or {}
            # Retain a bounded last-known catalogue for a transient failure,
            # but label its unavailable rows unknown below rather than missing.
            if not movies_ok: movies = previous.get("movies", [])
            if not series_ok: series = previous.get("series", [])
            # Episode lists can be large.  They are fetched only for the show
            # currently being browsed, with the same bounded freshness window.
            episodes: dict[int, list[dict]] = {}; episodes_ok = series_ok
            self.cache = {"movies": movies, "series": series, "episodes": episodes, "rq": rq, "sq": sq, "torrents": torrents,
                          "ok": {"movies": movies_ok, "series": series_ok, "episodes": episodes_ok, "rq": rq_ok, "sq": sq_ok, "qbit": qbit_ok}}
            self.cached_at = time.monotonic(); return self.cache

    async def _series_episodes(self, series_id: int) -> tuple[list[dict], bool]:
        cached = self.episode_cache.get(series_id)
        if cached and time.monotonic() - cached[0] < TTL: return cached[1], cached[2]
        async with self.episode_lock:
            cached = self.episode_cache.get(series_id)
            if cached and time.monotonic() - cached[0] < TTL: return cached[1], cached[2]
            try:
                rows = list(await self._arr_get("sonarr", "episode", seriesId=series_id) or [])
                result = (rows, True)
            except (httpx.HTTPError, RuntimeError, ValueError): result = ([], False)
            self.episode_cache[series_id] = (time.monotonic(), *result)
            return result

    @staticmethod
    def _queue(row: dict, queues: list[dict], kind: str) -> dict | None:
        key = "movieId" if kind == "movie" else "episodeId" if kind == "episode" else "seriesId"
        direct = next((x for x in queues if x.get(key) == row.get("id")), None)
        if direct or kind != "episode": return direct
        # A Sonarr season-pack queue row describes a series/season rather than
        # each episode; it still supplies truthful per-episode progress.
        return next((x for x in queues if x.get("episodeId") is None
                     and x.get("seriesId") == row.get("seriesId")
                     and x.get("seasonNumber") == row.get("seasonNumber")), None)

    def _state(self, row: dict, queue: dict | None, snap: dict, kind: str) -> dict:
        if not snap["ok"]["movies" if kind == "movie" else "series"]:
            return _availability("unknown", message="Availability unavailable")
        if row.get("hasFile") is True: return _availability("importing")
        if not snap["ok"]["rq" if kind == "movie" else "sq"]: return _availability("unknown", message="Availability unavailable")
        if not queue: return _availability("missing")
        status = " ".join(str(queue.get(k) or "") for k in ("trackedDownloadStatus", "trackedDownloadState", "status")).casefold()
        # Upstream messages commonly contain release names and absolute paths.
        # The browser gets only a fixed, non-sensitive explanation.
        state = "error" if any(x in status for x in ("error", "failed", "warning", "importblocked")) else "importing" if any(x in status for x in ("completed", "importpending", "importing")) or queue.get("sizeleft") == 0 else "paused" if "pause" in status else "stalled" if any(x in status for x in ("stall", "delay")) else "downloading"
        message = "Download needs attention" if state == "error" else "Importing into library" if state == "importing" else ""
        torrent = snap["torrents"].get(str(queue.get("downloadId") or "").lower()) if snap["ok"]["qbit"] else None
        if torrent:
            qstate = str(torrent.get("state") or "").casefold()
            if state == "error" or "error" in qstate or "missingfiles" in qstate: state = "error"
            elif isinstance(torrent.get("progress"), (int, float)) and torrent["progress"] >= 1: state = "importing"
            elif "stoppeddl" in qstate or "pause" in qstate: state = "paused"
            elif "stalledup" in qstate or "upload" in qstate: state = "importing"
            elif "stall" in qstate: state = "stalled"
            elif "error" in qstate: state = "error"
            eta = torrent.get("eta")
            return _availability(state, torrent.get("progress"), torrent.get("dlspeed"), None if eta == 8640000 else eta, message)
        size, left = queue.get("size"), queue.get("sizeleft")
        progress = 1 - float(left) / max(float(size), 1) if isinstance(size, (int, float)) and isinstance(left, (int, float)) else None
        return _availability(state, progress, message=message)

    @staticmethod
    def _base(row: dict, ident: str, type_: str) -> dict:
        return {"id": ident, "media_key": ident, "title": row.get("title") or "Untitled", "type": type_, "year": row.get("year"), "rating": None, "has_image": False,
                "resume_seconds": 0, "played": False, "series_title": None, "series_id": None, "season_number": row.get("seasonNumber"),
                "episode_number": row.get("episodeNumber"), "provider_ids": _ids(row), "playable": False}

    def _merge(self, row: dict, jellyfin: list[dict], snap: dict, kind: str, ident: str, type_: str, matched: dict | None = None) -> dict:
        result = {**self._base(row, ident, type_), "availability": self._state(row, self._queue(row, snap["rq" if kind == "movie" else "sq"], kind), snap, kind)}
        jf = matched or next((x for x in jellyfin if x.get("type") == type_ and _matches(x, row)), None)
        if jf:
            result.update(jf); result["playable"] = jf.get("type") in {"Movie", "Episode"} and bool(jf.get("id")); result["availability"] = _availability("ready")
        return result

    async def search_media(self, query: str, jellyfin: Any) -> list[dict]:
        snap = await self.snapshot(); jf = await jellyfin.search_media(query)
        out = [dict(x, media_key="jellyfin-" + str(x["id"]), playable=x.get("type") == "Movie", availability=_availability("ready")) for x in jf]; seen = {x["id"] for x in out}; needle = query.casefold().strip()
        for rows, kind, typ, prefix, okay in ((snap["movies"], "movie", "Movie", "radarr-", snap["ok"]["movies"]), (snap["series"], "series", "Series", "sonarr-", snap["ok"]["series"])):
            if rows:
                for row in rows:
                    if needle in str(row.get("title", "")).casefold():
                        item = self._merge(row, jf, snap, kind, prefix + str(row["id"]), typ)
                        if item["id"] in seen:
                            # Preserve the arr key even once the item has a
                            # Jellyfin id, so polling patches the same card.
                            out[:] = [item if x["id"] == item["id"] else x for x in out]
                        else:
                            out.append(item); seen.add(item["id"])
        return out

    async def recent_media(self, jellyfin: Any) -> list[dict]: return await self.enrich(await jellyfin.recent_media())

    async def enrich(self, items: list[dict]) -> list[dict]:
        snap = await self.snapshot(); out = []
        for item in items:
            movie = next((x for x in snap["movies"] if item.get("type") == "Movie" and _matches(x, item)), None)
            series = next((x for x in snap["series"] if item.get("type") == "Series" and _matches(x, item)), None)
            if movie: out.append(self._merge(movie, [item], snap, "movie", "radarr-" + str(movie["id"]), "Movie"))
            elif series: out.append(self._merge(series, [item], snap, "series", "sonarr-" + str(series["id"]), "Series"))
            else: out.append(dict(item, media_key="jellyfin-" + str(item.get("id")), playable=item.get("type") in {"Movie", "Episode"}, availability=_availability("ready")))
        return out

    async def _series_context(self, series_id: str, jellyfin: Any, snap: dict) -> tuple[dict | None, dict | None]:
        if series_id.startswith("sonarr-") and series_id[7:].isdigit():
            row = next((x for x in snap["series"] if x.get("id") == int(series_id[7:])), None)
            return row, None
        meta = await jellyfin.media_metadata(series_id)
        if not meta or meta.get("type") != "Series": return None, None
        return next((x for x in snap["series"] if _matches(x, meta)), None), meta

    async def seasons(self, series_id: str, jellyfin: Any) -> list[dict]:
        snap = await self.snapshot(); series, native = await self._series_context(series_id, jellyfin, snap)
        if not series:
            if series_id.startswith("sonarr-"):
                raise JellyfinItemUnavailable("That tracked show is unavailable. Try again.")
            return await jellyfin.seasons(series_id)
        sid = series["id"]; rows, episodes_ok = await self._series_episodes(sid)
        if not episodes_ok:
            raise JellyfinError("Episode availability is temporarily unavailable. Try again.")
        jf_seasons = []
        if series:
            candidates = [native] if native else await jellyfin.search_media(str(series.get("title") or ""))
            match = next((x for x in candidates if x and x.get("type") == "Series" and _matches(x, series)), None)
            if match: jf_seasons = await jellyfin.seasons(match["id"])
        numbers = sorted({x.get("seasonNumber") for x in rows if isinstance(x.get("seasonNumber"), int)})
        return [{"id": f"sonarr-{sid}-season-{n}", "media_key": f"sonarr-{sid}-season-{n}", "title": f"Season {n}", "number": n, "playable": False,
                 "availability": _availability("ready" if any(x.get("number") == n for x in jf_seasons) else "missing")} for n in numbers]

    async def episodes(self, series_id: str, season_id: str, jellyfin: Any) -> list[dict]:
        snap = await self.snapshot(); series, native = await self._series_context(series_id, jellyfin, snap)
        if not series:
            if series_id.startswith("sonarr-"):
                raise JellyfinItemUnavailable("That tracked show is unavailable. Try again.")
            return await self.enrich(await jellyfin.episodes(series_id, season_id))
        if season_id.startswith(f"sonarr-{series['id']}-season-"):
            season = int(season_id.rsplit("-", 1)[1])
        else:
            jf_season = next((x for x in await jellyfin.seasons(series_id) if x["id"] == season_id), None)
            if not jf_season: return []
            season = jf_season["number"]
        sid = series["id"]
        rows, episodes_ok = await self._series_episodes(sid)
        if not episodes_ok:
            raise JellyfinError("Episode availability is temporarily unavailable. Try again.")
        jf_episodes = []
        if series:
            candidates = [native] if native else await jellyfin.search_media(str(series.get("title") or ""))
            match = next((x for x in candidates if x.get("type") == "Series" and _matches(x, series)), None)
            if match:
                jf_season = next((x for x in await jellyfin.seasons(match["id"]) if x.get("number") == season), None)
                if jf_season: jf_episodes = await jellyfin.episodes(match["id"], jf_season["id"])
        # Provider ids are preferred.  Episode number is only used after an
        # exact provider-id series match above, so titles never participate.
        output = []
        for row in rows:
            if row.get("seasonNumber") != season: continue
            candidates = [x for x in jf_episodes if _matches(x, row)] or [x for x in jf_episodes if x.get("episode_number") == row.get("episodeNumber")]
            item = self._merge(row, candidates, snap, "episode", "sonarr-episode-" + str(row["id"]), "Episode", candidates[0] if candidates else None)
            item["series_title"] = series.get("title"); item["series_id"] = series_id
            output.append(item)
        return output
