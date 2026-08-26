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
    """The requested item is missing or is not a movie."""


class JellyfinStreamUnavailable(JellyfinError):
    """The requested audio or subtitle stream is not available."""


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
            headers={"X-Emby-Token": api_key},
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
    def _safe_movies(data: dict) -> list[dict]:
        results = []
        for item in data.get("Items", []):
            if item.get("Type") != "Movie" or not item.get("Id"):
                continue
            rating = item.get("CommunityRating")
            results.append({
                "id": item["Id"],
                "title": item.get("Name") or "Untitled",
                "year": item.get("ProductionYear"),
                "rating": round(rating, 1) if isinstance(rating, (int, float)) else None,
                "has_image": bool((item.get("ImageTags") or {}).get("Primary")),
            })
        return results

    async def _items(self, **params: Any) -> dict:
        return await self._request("GET", "/Items", params=params)

    async def _item(self, item_id: str) -> dict | None:
        data = await self._items(
            ids=item_id,
            recursive="true",
            limit=1,
            fields="MediaStreams",
        )
        return next(iter(data.get("Items") or []), None)

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
        # Jellyfin 10.11's /Items/{id} route requires a user id even for an API
        # key. The collection route accepts an id filter and is API-key safe.
        item = await self._item(item_id)
        if not item or item.get("Type") != "Movie":
            raise JellyfinItemUnavailable("That movie is no longer available")
        session = await self._required_session()
        await self._request(
            "POST",
            f"/Sessions/{session['Id']}/Playing",
            params={
                "playCommand": "PlayNow",
                "itemIds": item_id,
                "startPositionTicks": 0,
            },
        )
        # Jellyfin Desktop can preserve the previous paused state when PlayNow
        # replaces its queue. Give it a beat to load, then make Play explicit.
        await asyncio.sleep(0.4)
        await self._request(
            "POST", f"/Sessions/{session['Id']}/Playing/Unpause"
        )

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
