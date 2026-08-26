"""The Jellyfin session playing on the living-room TV.

Jellyfin's API key can see every connected client, so this module deliberately
selects one configured device name and exposes only a short allow-list of remote
commands.  Browser requests never receive the key or a Jellyfin session id.
"""

from __future__ import annotations

from typing import Any, Literal

import httpx


class JellyfinError(Exception):
    """Jellyfin could not be reached or refused a command."""


class JellyfinSessionUnavailable(JellyfinError):
    """The configured TV client is not connected to Jellyfin."""


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

    async def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        try:
            response = await self._client.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            raise JellyfinError(f"Jellyfin unreachable: {exc.__class__.__name__}") from exc
        if response.status_code >= 400:
            raise JellyfinError(f"Jellyfin answered {response.status_code} for {path}")
        if not response.content:
            return None
        return response.json()

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
        return session_state(await self._tv_session())

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
