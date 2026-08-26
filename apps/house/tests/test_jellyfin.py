from __future__ import annotations

import json
import unittest

import httpx

from app.jellyfin import (
    Jellyfin,
    JellyfinItemUnavailable,
    JellyfinSessionUnavailable,
    session_state,
)


MOVIE_ID = "b936035f63983b5500b6a746a2885b51"
SERIES_ID = "785c141f7ea8154f0c6bc09fd7fe3d39"


TV_SESSION = {
    "Id": "tv-session",
    "DeviceName": "home",
    "Client": "Jellyfin Desktop",
    "SupportsRemoteControl": True,
    "LastActivityDate": "2026-08-25T16:00:00Z",
    "NowPlayingItem": {
        "Name": "The Example",
        "SeriesName": "Examples",
        "ParentIndexNumber": 2,
        "IndexNumber": 4,
        "RunTimeTicks": 2_700_000_000,
    },
    "PlayState": {"PositionTicks": 1_230_000_000, "IsPaused": True},
}


class JellyfinStateTests(unittest.TestCase):
    def test_episode_state_is_small_and_user_facing(self) -> None:
        self.assertEqual(
            session_state(TV_SESSION),
            {
                "reachable": True,
                "connected": True,
                "state": "paused",
                "title": "The Example",
                "subtitle": "Examples · S2E4",
                "position": 123,
                "duration": 270,
            },
        )

    def test_unreachable_state_does_not_leak_internal_details(self) -> None:
        self.assertEqual(session_state(None, reachable=False)["state"], "unavailable")
        self.assertFalse(session_state(None, reachable=False)["connected"])


class JellyfinClientTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.requests: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            self.requests.append(request)
            if request.method == "GET" and request.url.path == "/Sessions":
                sessions = [
                    {
                        "Id": "phone",
                        "DeviceName": "Charles's phone",
                        "SupportsRemoteControl": True,
                    },
                    TV_SESSION,
                ]
                return httpx.Response(200, json=sessions)
            if request.method == "GET" and request.url.path == "/Items":
                return httpx.Response(200, json={"Items": [
                    {
                        "Id": MOVIE_ID,
                        "Name": "The Example Movie",
                        "Type": "Movie",
                        "ProductionYear": 2026,
                        "CommunityRating": 7.86,
                        "ImageTags": {"Primary": "poster-tag"},
                    },
                    {"Id": SERIES_ID, "Name": "Not a movie", "Type": "Series"},
                ]})
            if request.method == "GET" and request.url.path == f"/Items/{MOVIE_ID}":
                return httpx.Response(200, json={"Id": MOVIE_ID, "Type": "Movie"})
            if request.method == "GET" and request.url.path == f"/Items/{SERIES_ID}":
                return httpx.Response(200, json={"Id": SERIES_ID, "Type": "Series"})
            if request.method == "GET" and request.url.path.endswith("/Images/Primary"):
                return httpx.Response(
                    200, content=b"poster", headers={"Content-Type": "image/webp"}
                )
            return httpx.Response(204)

        self.client = Jellyfin(
            "http://jellyfin.test",
            "secret",
            "HOME",
            transport=httpx.MockTransport(handler),
        )

    async def asyncTearDown(self) -> None:
        await self.client.aclose()

    async def test_selects_only_the_configured_tv(self) -> None:
        state = await self.client.state()
        self.assertTrue(state["connected"])
        self.assertEqual(state["title"], "The Example")

    async def test_navigation_uses_whitelisted_command(self) -> None:
        await self.client.navigate("KEY_LEFT")
        self.assertEqual(
            self.requests[-1].url.path,
            "/Sessions/tv-session/Command/MoveLeft",
        )

    async def test_seek_is_relative_and_clamped(self) -> None:
        await self.client.playback("seek_forward")
        self.assertEqual(self.requests[-1].url.path, "/Sessions/tv-session/Playing/Seek")
        self.assertEqual(
            self.requests[-1].url.params["seekPositionTicks"],
            "1330000000",
        )

    async def test_api_key_never_appears_in_state(self) -> None:
        state = await self.client.state()
        self.assertNotIn("secret", json.dumps(state))
        self.assertNotIn("session", json.dumps(state))

    async def test_search_returns_only_safe_movie_fields(self) -> None:
        movies = await self.client.search_movies("example")
        self.assertEqual(movies, [{
            "id": MOVIE_ID,
            "title": "The Example Movie",
            "year": 2026,
            "rating": 7.9,
            "has_image": True,
        }])
        self.assertEqual(self.requests[-1].url.params["includeItemTypes"], "Movie")
        self.assertEqual(self.requests[-1].url.params["recursive"], "true")

    async def test_artwork_keeps_bytes_and_media_type_server_side(self) -> None:
        content, media_type = await self.client.artwork(MOVIE_ID)
        self.assertEqual(content, b"poster")
        self.assertEqual(media_type, "image/webp")

    async def test_play_movie_validates_then_targets_tv_session(self) -> None:
        await self.client.play_movie(MOVIE_ID)
        request = self.requests[-1]
        self.assertEqual(request.url.path, "/Sessions/tv-session/Playing")
        self.assertEqual(request.url.params["playCommand"], "PlayNow")
        self.assertEqual(request.url.params["itemIds"], MOVIE_ID)
        self.assertEqual(request.url.params["startPositionTicks"], "0")

    async def test_play_refuses_non_movie_items(self) -> None:
        with self.assertRaises(JellyfinItemUnavailable):
            await self.client.play_movie(SERIES_ID)
        self.assertFalse(any(r.url.path.endswith("/Playing") for r in self.requests))

    async def test_missing_tv_refuses_commands(self) -> None:
        client = Jellyfin(
            "http://jellyfin.test",
            "secret",
            "not-the-tv",
            transport=self.client._client._transport,
        )
        try:
            with self.assertRaises(JellyfinSessionUnavailable):
                await client.playback("play_pause")
        finally:
            await client.aclose()
