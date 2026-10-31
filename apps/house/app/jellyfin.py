"""The Jellyfin session playing on the living-room TV.

Jellyfin's API key can see every connected client, so this module deliberately
selects one configured device name and exposes only a short allow-list of remote
commands.  Browser requests never receive the key or a Jellyfin session id.
"""

from __future__ import annotations

import asyncio
from typing import Any, Literal

import httpx


class JellyfinError(Exception):
    """Jellyfin could not be reached or refused a command."""


class JellyfinSessionUnavailable(JellyfinError):
    """The configured TV client is not connected to Jellyfin."""


class JellyfinItemUnavailable(JellyfinError):
    """The requested item is missing or cannot be played."""


class JellyfinStreamUnavailable(JellyfinError):
    """The requested audio or subtitle stream is not available."""


class JellyfinPlaybackUnavailable(JellyfinError):
    """The active player cannot safely perform the requested playback change."""


PlaybackAction = Literal[
    "play_pause", "stop", "previous", "next", "seek_backward", "seek_forward"
]

PLAYBACK_COMMANDS: dict[PlaybackAction, str] = {
    "play_pause": "PlayPause",
    "stop": "Stop",
    "previous": "PreviousTrack",
    "next": "NextTrack",
}

NAVIGATION_COMMANDS = {
    "KEY_UP": "MoveUp",
    "KEY_DOWN": "MoveDown",
    "KEY_LEFT": "MoveLeft",
    "KEY_RIGHT": "MoveRight",
    "KEY_ENTER": "Select",
    "KEY_RETURN": "Back",
    "KEY_HOME": "GoHome",
    "KEY_MENU": "ToggleContextMenu",
}


def _ticks_to_seconds(value: Any) -> int | None:
    if not isinstance(value, (int, float)):
        return None
    return max(0, round(value / 10_000_000))


def _stream_label(stream: dict) -> str:
    label = stream.get("DisplayTitle") or stream.get("Title")
    if label:
        return str(label)
    parts = [stream.get("Language"), stream.get("Codec")]
    return " · ".join(str(part).upper() for part in parts if part) or "Unknown track"


def _stream_choices(item: dict, stream_type: str) -> list[dict]:
    return [
        {"index": stream["Index"], "label": _stream_label(stream)}
        for stream in item.get("MediaStreams") or []
        if stream.get("Type") == stream_type and isinstance(stream.get("Index"), int)
    ]


def session_state(session: dict | None, reachable: bool = True) -> dict:
    """Return the small, stable shape exposed by Overseer's state endpoint."""
    if session is None:
        return {
            "reachable": reachable,
            "connected": False,
            "state": "unavailable" if not reachable else "idle",
            "title": None,
            "subtitle": None,
            "position": None,
            "duration": None,
            "item_id": None,
            "series_id": None,
            "can_seek": False,
            "user_context_available": False,
            "user_name": None,
            "audio_streams": [],
            "subtitle_streams": [],
            "audio_stream_index": None,
            "subtitle_stream_index": -1,
        }

    item = session.get("NowPlayingItem") or {}
    play_state = session.get("PlayState") or {}
    title = item.get("Name")
    if title:
        state = "paused" if play_state.get("IsPaused") else "playing"
    else:
        state = "idle"

    subtitle = item.get("SeriesName")
    if subtitle and item.get("IndexNumber") is not None:
        numbers = []
        if item.get("ParentIndexNumber") is not None:
            numbers.append(f"S{item['ParentIndexNumber']}")
        numbers.append(f"E{item['IndexNumber']}")
        subtitle = f"{subtitle} · {''.join(numbers)}"

    return {
        "reachable": reachable,
        "connected": True,
        "state": state,
        "title": title,
        "subtitle": subtitle,
        "position": _ticks_to_seconds(play_state.get("PositionTicks")),
        "duration": _ticks_to_seconds(item.get("RunTimeTicks")),
        "item_id": item.get("Id"),
        "series_id": item.get("SeriesId"),
        "can_seek": bool(play_state.get("CanSeek")),
        "user_context_available": bool(session.get("UserId")),
        "user_name": session.get("UserName") if session.get("UserId") else None,
        "audio_streams": _stream_choices(item, "Audio"),
        "subtitle_streams": _stream_choices(item, "Subtitle"),
        "audio_stream_index": play_state.get("AudioStreamIndex"),
        "subtitle_stream_index": play_state.get("SubtitleStreamIndex", -1),
    }


class Jellyfin:
    def __init__(
        self,
        url: str,
        api_key: str,
        device_name: str,
        timeout: float = 1.8,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.device_name = device_name
        self._client = httpx.AsyncClient(
            base_url=url.rstrip("/"),
            headers={"Authorization": f'MediaBrowser Token="{api_key}"'},
            timeout=timeout,
            transport=transport,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _response(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        try:
            response = await self._client.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            raise JellyfinError(f"Jellyfin unreachable: {exc.__class__.__name__}") from exc
        if response.status_code >= 400:
            raise JellyfinError(f"Jellyfin answered {response.status_code} for {path}")
        return response

    async def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        response = await self._response(method, path, **kwargs)
        if not response.content:
            return None
        return response.json()

    @staticmethod
    def _is_playable(item: dict, allowed_types: set[str]) -> bool:
        return (
            item.get("Type") in allowed_types
            and bool(item.get("Id"))
            and item.get("IsMissing") is not True
            and item.get("IsVirtualItem") is not True
            and item.get("LocationType") != "Virtual"
        )

    @staticmethod
    def _safe_item(item: dict) -> dict:
        rating = item.get("CommunityRating")
        user_data = item.get("UserData") or {}
        provider_ids = {
            str(key).lower(): str(value)
            for key, value in (item.get("ProviderIds") or {}).items()
            if str(key).lower() in {"tmdb", "tvdb", "imdb"} and value not in (None, "")
        }
        return {
            "id": item["Id"],
            "title": item.get("Name") or "Untitled",
            "type": item["Type"],
            "year": item.get("ProductionYear"),
            "rating": round(rating, 1) if isinstance(rating, (int, float)) else None,
            "has_image": bool((item.get("ImageTags") or {}).get("Primary")),
            "resume_seconds": _ticks_to_seconds(user_data.get("PlaybackPositionTicks")) or 0,
            "played": bool(user_data.get("Played")),
            "series_title": item.get("SeriesName"),
            "series_id": item.get("SeriesId"),
            "season_number": item.get("ParentIndexNumber"),
            "episode_number": item.get("IndexNumber"),
            "provider_ids": provider_ids,
        }

    @classmethod
    def _safe_media(cls, data: dict) -> list[dict]:
        results = []
        for item in data.get("Items", []):
            if not cls._is_playable(item, {"Movie", "Series"}):
                continue
            results.append(cls._safe_item(item))
        return results

    @staticmethod
    def _safe_movies(data: dict) -> list[dict]:
        """Keep the old movie-only client shape for callers that still use it."""
        return [
            {key: item[key] for key in ("id", "title", "year", "rating", "has_image")}
            for item in Jellyfin._safe_media(data)
            if item["type"] == "Movie"
        ]

    async def _items(self, **params: Any) -> dict:
        return await self._request("GET", "/Items", params=params)

    async def _tv_user_id(self) -> str | None:
        session = await self._tv_session()
        user_id = (session or {}).get("UserId")
        return str(user_id) if user_id else None

    async def _library_params(self) -> dict[str, str]:
        user_id = await self._tv_user_id()
        return {"userId": user_id, "enableUserData": "true"} if user_id else {}

    async def _item(self, item_id: str, user_id: str | None = None) -> dict | None:
        params: dict[str, Any] = {
            "ids": item_id,
            "recursive": "true",
            "limit": 1,
            "fields": "MediaStreams,ProviderIds,PrimaryImageAspectRatio",
        }
        if user_id:
            params.update({"userId": user_id, "enableUserData": "true"})
        data = await self._items(
            **params,
        )
        return next(
            (item for item in data.get("Items") or [] if item.get("Id") == item_id),
            None,
        )

    async def search_movies(self, query: str, limit: int = 12) -> list[dict]:
        data = await self._items(
            searchTerm=query,
            includeItemTypes="Movie",
            recursive="true",
            limit=limit,
            fields="PrimaryImageAspectRatio",
            sortBy="SortName",
            sortOrder="Ascending",
        )
        return self._safe_movies(data)

    async def search_media(self, query: str, limit: int = 24) -> list[dict]:
        user_params = await self._library_params()
        data = await self._items(
            searchTerm=query,
            includeItemTypes="Movie,Series",
            recursive="true",
            limit=limit,
            fields="PrimaryImageAspectRatio,ProviderIds",
            sortBy="SortName",
            sortOrder="Ascending",
            **user_params,
        )
        return self._safe_media(data)

    async def recent_movies(self, limit: int = 12) -> list[dict]:
        data = await self._items(
            includeItemTypes="Movie",
            recursive="true",
            limit=limit,
            fields="PrimaryImageAspectRatio",
            sortBy="DateCreated",
            sortOrder="Descending",
        )
        return self._safe_movies(data)

    async def recent_media(self, limit: int = 24) -> list[dict]:
        user_params = await self._library_params()
        data = await self._items(
            includeItemTypes="Movie,Series",
            recursive="true",
            limit=limit,
            fields="PrimaryImageAspectRatio,ProviderIds",
            sortBy="DateCreated",
            sortOrder="Descending",
            **user_params,
        )
        return self._safe_media(data)

    async def seasons(self, series_id: str) -> list[dict]:
        user_id = await self._tv_user_id()
        series = await self._item(series_id, user_id)
        if not series or series.get("Type") != "Series":
            raise JellyfinItemUnavailable("That show is no longer available")
        data = await self._request(
            "GET",
            f"/Shows/{series_id}/Seasons",
            params={"fields": "PrimaryImageAspectRatio", **(
                {"userId": user_id, "enableUserData": "true"} if user_id else {})},
        )
        results = [
            {
                "id": item["Id"],
                "title": item.get("Name") or "Season",
                "number": item.get("IndexNumber"),
            }
            for item in data.get("Items", [])
            if (
                item.get("Id")
                and isinstance(item.get("IndexNumber"), int)
                and item.get("IsMissing") is not True
                and item.get("IsVirtualItem") is not True
                and item.get("LocationType") != "Virtual"
            )
        ]
        return sorted(results, key=lambda season: (season["number"], season["title"].casefold()))

    async def media_metadata(self, item_id: str) -> dict | None:
        """Safe identity metadata for local read-only library adapters."""
        item = await self._item(item_id)
        if not item or item.get("Type") not in {"Movie", "Series", "Episode"}:
            return None
        return self._safe_item(item)

    async def episodes(self, series_id: str, season_id: str) -> list[dict]:
        # Fetching seasons first prevents a season id from another series being
        # used to browse unrelated content.
        season_ids = {season["id"] for season in await self.seasons(series_id)}
        if season_id not in season_ids:
            raise JellyfinItemUnavailable("That season is no longer available")
        user_id = await self._tv_user_id()
        data = await self._request(
            "GET",
            f"/Shows/{series_id}/Episodes",
            params={
                "seasonId": season_id,
                "fields": "PrimaryImageAspectRatio",
                **({"userId": user_id, "enableUserData": "true"} if user_id else {}),
            },
        )
        results = []
        for item in data.get("Items", []):
            if (
                item.get("Type") != "Episode"
                or not item.get("Id")
                or item.get("IsMissing") is True
                or item.get("IsVirtualItem") is True
                or item.get("LocationType") == "Virtual"
            ):
                continue
            results.append({
                **self._safe_item(item), "type": "Episode",
            })
        return sorted(
            results,
            key=lambda episode: (
                episode["season_number"]
                if isinstance(episode["season_number"], int)
                else 10**9,
                episode["episode_number"]
                if isinstance(episode["episode_number"], int)
                else 10**9,
                episode["title"].casefold(),
            ),
        )

    async def artwork(self, item_id: str) -> tuple[bytes, str]:
        response = await self._response(
            "GET",
            f"/Items/{item_id}/Images/Primary",
            params={"maxWidth": 240, "quality": 85},
        )
        return response.content, response.headers.get("content-type", "image/jpeg")

    async def _tv_session(self) -> dict | None:
        sessions = await self._request("GET", "/Sessions")
        wanted = self.device_name.casefold()
        matches = [
            session for session in sessions
            if str(session.get("DeviceName", "")).casefold() == wanted
            and session.get("SupportsRemoteControl") is True
        ]
        if not matches:
            return None
        # Prefer the session currently playing, then the most recently active
        # connection. A duplicate stale session must never beat the real player.
        return max(
            matches,
            key=lambda session: (
                bool(session.get("NowPlayingItem")),
                str(session.get("LastActivityDate", "")),
            ),
        )

    async def state(self) -> dict:
        session = await self._tv_session()
        item = (session or {}).get("NowPlayingItem") or {}
        if session and item.get("Id") and not item.get("MediaStreams"):
            full_item = await self._item(item["Id"])
            if full_item:
                session = {**session, "NowPlayingItem": {**item, **full_item}}
        return session_state(session)

    async def continue_watching(self, limit: int = 12) -> list[dict]:
        user_id = await self._tv_user_id()
        if not user_id:
            return []
        data = await self._request("GET", f"/Users/{user_id}/Items/Resume", params={
            "includeItemTypes": "Movie,Episode", "limit": limit,
            "fields": "PrimaryImageAspectRatio",
            "enableUserData": "true",
        })
        return [self._safe_item(item) for item in data.get("Items", [])
                if self._is_playable(item, {"Movie", "Episode"})]

    async def next_up(self, limit: int = 12) -> list[dict]:
        user_id = await self._tv_user_id()
        if not user_id:
            return []
        data = await self._request("GET", "/Shows/NextUp", params={
            "userId": user_id, "limit": limit,
            "fields": "PrimaryImageAspectRatio",
            "enableUserData": "true",
        })
        return [self._safe_item(item) for item in data.get("Items", [])
                if self._is_playable(item, {"Episode"})]

    async def next_episode(self) -> dict | None:
        session = await self._tv_session()
        current = (session or {}).get("NowPlayingItem") or {}
        series_id = current.get("SeriesId")
        if (
            current.get("Type") != "Episode"
            or not series_id
            or not isinstance(current.get("ParentIndexNumber"), int)
            or not isinstance(current.get("IndexNumber"), int)
        ):
            return None
        params = {"fields": "PrimaryImageAspectRatio"}
        if session.get("UserId"):
            params.update({"userId": str(session["UserId"]), "enableUserData": "true"})
        data = await self._request("GET", f"/Shows/{series_id}/Episodes", params=params)
        current_key = (current["ParentIndexNumber"], current["IndexNumber"])
        candidates = [item for item in data.get("Items", [])
                      if self._is_playable(item, {"Episode"})
                      and isinstance(item.get("ParentIndexNumber"), int)
                      and isinstance(item.get("IndexNumber"), int)]
        later = [item for item in candidates if (
            item.get("ParentIndexNumber") if isinstance(item.get("ParentIndexNumber"), int) else 10**9,
            item.get("IndexNumber") if isinstance(item.get("IndexNumber"), int) else 10**9,
        ) > current_key]
        if not later:
            return None
        next_item = min(later, key=lambda item: (
            item.get("ParentIndexNumber") if isinstance(item.get("ParentIndexNumber"), int) else 10**9,
            item.get("IndexNumber") if isinstance(item.get("IndexNumber"), int) else 10**9,
            str(item.get("Name", "")).casefold(),
        ))
        return self._safe_item(next_item)

    async def _required_session(self) -> dict:
        session = await self._tv_session()
        if session is None:
            raise JellyfinSessionUnavailable("The TV's Jellyfin player is not connected")
        return session

    async def navigate(self, tv_key: str) -> None:
        command = NAVIGATION_COMMANDS.get(tv_key)
        if command is None:
            raise ValueError("Unsupported Jellyfin navigation key")
        session = await self._required_session()
        await self._request(
            "POST", f"/Sessions/{session['Id']}/Command/{command}"
        )

    async def playback(self, action: PlaybackAction) -> None:
        session = await self._required_session()
        if action in ("seek_backward", "seek_forward"):
            play_state = session.get("PlayState") or {}
            item = session.get("NowPlayingItem") or {}
            position = int(play_state.get("PositionTicks") or 0)
            delta = 10 * 10_000_000 * (-1 if action == "seek_backward" else 1)
            target = max(0, position + delta)
            duration = item.get("RunTimeTicks")
            if isinstance(duration, (int, float)):
                target = min(target, int(duration))
            await self._request(
                "POST",
                f"/Sessions/{session['Id']}/Playing/Seek",
                params={"seekPositionTicks": target},
            )
            return
        await self._request(
            "POST",
            f"/Sessions/{session['Id']}/Playing/{PLAYBACK_COMMANDS[action]}",
        )

    async def play_movie(self, item_id: str) -> None:
        await self.play_item(item_id)

    async def play_item(self, item_id: str, restart: bool = False) -> int:
        # Jellyfin 10.11's /Items/{id} route requires a user id even for an API
        # key. The collection route accepts an id filter and is API-key safe.
        session = await self._required_session()
        user_id = str(session["UserId"]) if session.get("UserId") else None
        item = await self._item(item_id, user_id)
        if (
            not item
            or item.get("Type") not in {"Movie", "Episode"}
            or item.get("IsMissing") is True
            or item.get("IsVirtualItem") is True
            or item.get("LocationType") == "Virtual"
        ):
            raise JellyfinItemUnavailable("That item cannot be played")
        resume = 0 if restart or not user_id else int(
            ((item.get("UserData") or {}).get("PlaybackPositionTicks") or 0)
        )
        current_item = session.get("NowPlayingItem") or {}
        if not restart and current_item.get("Id") == item_id:
            live_position = (session.get("PlayState") or {}).get("PositionTicks")
            if isinstance(live_position, (int, float)):
                resume = max(resume, int(live_position))
        await self._request(
            "POST",
            f"/Sessions/{session['Id']}/Playing",
            params={
                "playCommand": "PlayNow",
                "itemIds": item_id,
                "startPositionTicks": max(0, resume),
            },
        )
        # Jellyfin Desktop can preserve the previous paused state when PlayNow
        # replaces its queue. Give it a beat to load, then make Play explicit.
        await asyncio.sleep(0.4)
        await self._request(
            "POST", f"/Sessions/{session['Id']}/Playing/Unpause"
        )
        return _ticks_to_seconds(resume) or 0

    async def seek(self, item_id: str, seconds: float) -> int:
        session = await self._required_session()
        item = session.get("NowPlayingItem") or {}
        play_state = session.get("PlayState") or {}
        if item.get("Id") != item_id:
            raise JellyfinPlaybackUnavailable("The TV changed what it is playing")
        if not play_state.get("CanSeek"):
            raise JellyfinPlaybackUnavailable("This title cannot be scrubbed")
        duration = item.get("RunTimeTicks")
        if not isinstance(duration, (int, float)) or duration <= 0:
            raise JellyfinPlaybackUnavailable("This title has no seekable duration")
        ticks = min(max(0, round(seconds * 10_000_000)), int(duration))
        await self._request("POST", f"/Sessions/{session['Id']}/Playing/Seek",
                            params={"seekPositionTicks": ticks})
        return _ticks_to_seconds(ticks) or 0

    async def set_stream(self, stream_type: Literal["audio", "subtitle"], index: int) -> None:
        session = await self._required_session()
        item = session.get("NowPlayingItem") or {}
        if not item.get("Id"):
            raise JellyfinStreamUnavailable("Nothing is playing on the TV")
        if not item.get("MediaStreams"):
            item = await self._item(item["Id"]) or item

        jellyfin_type = "Audio" if stream_type == "audio" else "Subtitle"
        valid_indexes = {choice["index"] for choice in _stream_choices(item, jellyfin_type)}
        if stream_type == "subtitle" and index == -1:
            pass
        elif index not in valid_indexes:
            raise JellyfinStreamUnavailable(f"That {stream_type} track is unavailable")

        command = (
            "SetAudioStreamIndex" if stream_type == "audio"
            else "SetSubtitleStreamIndex"
        )
        await self._request(
            "POST",
            f"/Sessions/{session['Id']}/Command",
            json={"Name": command, "Arguments": {"Index": str(index)}},
        )
