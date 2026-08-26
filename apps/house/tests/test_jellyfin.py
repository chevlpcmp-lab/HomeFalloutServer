from __future__ import annotations

import json
import unittest

import httpx

from app.jellyfin import (
    Jellyfin,
    JellyfinItemUnavailable,
    JellyfinSessionUnavailable,
    JellyfinStreamUnavailable,
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
        "Id": MOVIE_ID,
        "Name": "The Example",
        "SeriesName": "Examples",
        "ParentIndexNumber": 2,
        "IndexNumber": 4,
        "RunTimeTicks": 2_700_000_000,
        "MediaStreams": [
            {"Type": "Audio", "Index": 1, "DisplayTitle": "English · AAC · Stereo"},
            {"Type": "Audio", "Index": 2, "Language": "fra", "Codec": "aac"},
            {"Type": "Subtitle", "Index": 3, "DisplayTitle": "English · SUBRIP"},
        ],
    },
    "PlayState": {
        "PositionTicks": 1_230_000_000,
        "IsPaused": True,
        "AudioStreamIndex": 1,
        "SubtitleStreamIndex": -1,
    },
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
                "item_id": MOVIE_ID,
                "audio_streams": [
                    {"index": 1, "label": "English · AAC · Stereo"},
                    {"index": 2, "label": "FRA · AAC"},
                ],
                "subtitle_streams": [
                    {"index": 3, "label": "English · SUBRIP"},
                ],
                "audio_stream_index": 1,
                "subtitle_stream_index": -1,
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
                requested_id = request.url.params.get("ids")
                if requested_id == MOVIE_ID:
                    return httpx.Response(200, json={"Items": [
                        {
                            "Id": MOVIE_ID,
                            "Name": "The Example Movie",
                            "Type": "Movie",
                            "MediaStreams": TV_SESSION["NowPlayingItem"]["MediaStreams"],
                        }
                    ]})
                if requested_id == SERIES_ID:
                    return httpx.Response(200, json={"Items": [
                        {"Id": SERIES_ID, "Name": "Not a movie", "Type": "Series"}
                    ]})
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

    async def test_recent_movies_uses_library_creation_order(self) -> None:
        movies = await self.client.recent_movies()
        self.assertEqual(movies[0]["title"], "The Example Movie")
        self.assertEqual(self.requests[-1].url.params["sortBy"], "DateCreated")
        self.assertEqual(self.requests[-1].url.params["sortOrder"], "Descending")

    async def test_artwork_keeps_bytes_and_media_type_server_side(self) -> None:
        content, media_type = await self.client.artwork(MOVIE_ID)
        self.assertEqual(content, b"poster")
        self.assertEqual(media_type, "image/webp")

    async def test_play_movie_validates_then_targets_tv_session(self) -> None:
        await self.client.play_movie(MOVIE_ID)
        validation = next(
            request for request in self.requests
            if request.url.path == "/Items" and request.url.params.get("ids") == MOVIE_ID
        )
        self.assertEqual(validation.url.params["fields"], "MediaStreams")
        self.assertFalse(any(request.url.path == f"/Items/{MOVIE_ID}" for request in self.requests))
        request = self.requests[-1]
        self.assertEqual(request.url.path, "/Sessions/tv-session/Playing")
        self.assertEqual(request.url.params["playCommand"], "PlayNow")
        self.assertEqual(request.url.params["itemIds"], MOVIE_ID)
        self.assertEqual(request.url.params["startPositionTicks"], "0")

    async def test_play_refuses_non_movie_items(self) -> None:
        with self.assertRaises(JellyfinItemUnavailable):
            await self.client.play_movie(SERIES_ID)
        self.assertFalse(any(r.url.path.endswith("/Playing") for r in self.requests))

    async def test_audio_stream_command_is_validated_and_sent(self) -> None:
        await self.client.set_stream("audio", 2)
        request = self.requests[-1]
        self.assertEqual(request.url.path, "/Sessions/tv-session/Command")
        self.assertEqual(json.loads(request.content), {
            "Name": "SetAudioStreamIndex",
            "Arguments": {"Index": "2"},
        })

    async def test_subtitles_can_be_disabled(self) -> None:
        await self.client.set_stream("subtitle", -1)
        self.assertEqual(json.loads(self.requests[-1].content), {
            "Name": "SetSubtitleStreamIndex",
            "Arguments": {"Index": "-1"},
        })

    async def test_stream_command_refuses_unknown_index(self) -> None:
        with self.assertRaises(JellyfinStreamUnavailable):
            await self.client.set_stream("subtitle", 99)
        self.assertFalse(any(r.url.path.endswith("/Command") for r in self.requests))

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
