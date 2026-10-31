from __future__ import annotations

import json
import unittest

import httpx

from app.jellyfin import (
    Jellyfin,
    JellyfinItemUnavailable,
    JellyfinPlaybackUnavailable,
    JellyfinSessionUnavailable,
    JellyfinStreamUnavailable,
    session_state,
)


MOVIE_ID = "b936035f63983b5500b6a746a2885b51"
SERIES_ID = "785c141f7ea8154f0c6bc09fd7fe3d39"
SEASON_ID = "11111111111111111111111111111111"
OTHER_SEASON_ID = "22222222222222222222222222222222"
EPISODE_ID = "33333333333333333333333333333333"
EPISODE_ONE_ID = "55555555555555555555555555555555"
VIRTUAL_EPISODE_ID = "66666666666666666666666666666666"
MISSING_EPISODE_ID = "77777777777777777777777777777777"


TV_SESSION = {
    "Id": "tv-session",
    "UserId": "living-room-user",
    "UserName": "Living room",
    "DeviceName": "home",
    "Client": "Jellyfin Desktop",
    "SupportsRemoteControl": True,
    "LastActivityDate": "2026-08-25T16:00:00Z",
    "NowPlayingItem": {
        "Id": MOVIE_ID,
        "Type": "Movie",
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
        "CanSeek": True,
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
                "series_id": None,
                "can_seek": True,
                "user_context_available": True,
                "user_name": "Living room",
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
                if requested_id == EPISODE_ID:
                    return httpx.Response(200, json={"Items": [
                        {
                            "Id": EPISODE_ID,
                            "Name": "The Example Episode",
                            "Type": "Episode",
                            "ParentIndexNumber": 1,
                            "IndexNumber": 2,
                            "MediaStreams": TV_SESSION["NowPlayingItem"]["MediaStreams"],
                        }
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
            if request.method == "GET" and request.url.path == "/Users/living-room-user/Items/Resume":
                return httpx.Response(200, json={"Items": [{
                    "Id": EPISODE_ID, "Name": "The Example Episode", "Type": "Episode",
                    "SeriesName": "Example Show", "SeriesId": SERIES_ID,
                    "ParentIndexNumber": 1, "IndexNumber": 2,
                    "UserData": {"PlaybackPositionTicks": 900_000_000},
                }]})
            if request.method == "GET" and request.url.path == "/Shows/NextUp":
                return httpx.Response(200, json={"Items": [{
                    "Id": EPISODE_ONE_ID, "Name": "The First Example Episode", "Type": "Episode",
                    "SeriesName": "Example Show", "SeriesId": SERIES_ID,
                    "ParentIndexNumber": 1, "IndexNumber": 1,
                }]})
            if request.method == "GET" and request.url.path == f"/Shows/{SERIES_ID}/Seasons":
                return httpx.Response(200, json={"Items": [
                    {"Id": SEASON_ID, "Name": "Season 1", "IndexNumber": 1},
                    {"Id": OTHER_SEASON_ID, "Name": "Specials", "IndexNumber": 0},
                    {"Id": "88888888888888888888888888888888", "Name": "Missing", "IndexNumber": 9, "IsMissing": True},
                ]})
            if request.method == "GET" and request.url.path == f"/Shows/{SERIES_ID}/Episodes":
                if request.url.params.get("seasonId") is None:
                    return httpx.Response(200, json={"Items": [
                        {"Id": EPISODE_ID, "Name": "Current", "Type": "Episode", "ParentIndexNumber": 1, "IndexNumber": 2},
                        {"Id": EPISODE_ONE_ID, "Name": "Next", "Type": "Episode", "ParentIndexNumber": 2, "IndexNumber": 1},
                    ]})
                if request.url.params.get("seasonId") != SEASON_ID:
                    return httpx.Response(200, json={"Items": []})
                return httpx.Response(200, json={"Items": [
                    {
                        "Id": EPISODE_ID,
                        "Name": "The Example Episode",
                        "Type": "Episode",
                        "ParentIndexNumber": 1,
                        "IndexNumber": 2,
                        "CommunityRating": 8.24,
                        "ImageTags": {"Primary": "episode-tag"},
                    },
                    {
                        "Id": EPISODE_ONE_ID,
                        "Name": "The First Example Episode",
                        "Type": "Episode",
                        "ParentIndexNumber": 1,
                        "IndexNumber": 1,
                    },
                    {
                        "Id": VIRTUAL_EPISODE_ID,
                        "Name": "Virtual Placeholder",
                        "Type": "Episode",
                        "ParentIndexNumber": 1,
                        "IndexNumber": 3,
                        "IsVirtualItem": True,
                    },
                    {
                        "Id": MISSING_EPISODE_ID,
                        "Name": "Missing Episode",
                        "Type": "Episode",
                        "ParentIndexNumber": 1,
                        "IndexNumber": 4,
                        "IsMissing": True,
                    },
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

    async def test_uses_current_jellyfin_authorization_header(self) -> None:
        await self.client.state()
        request = self.requests[0]
        self.assertEqual(request.headers["Authorization"], 'MediaBrowser Token="secret"')
        self.assertNotIn("X-Emby-Token", request.headers)
        self.assertNotIn("api_key", request.url.params)

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

    async def test_search_media_returns_movies_and_series(self) -> None:
        items = await self.client.search_media("example")
        self.assertEqual([item["type"] for item in items], ["Movie", "Series"])
        self.assertEqual(items[1], {
            "id": SERIES_ID,
            "title": "Not a movie",
            "type": "Series",
            "year": None,
            "rating": None,
            "has_image": False,
            "resume_seconds": 0,
            "played": False,
            "series_title": None,
            "series_id": None,
            "season_number": None,
            "episode_number": None,
            "provider_ids": {},
        })
        self.assertEqual(self.requests[-1].url.params["includeItemTypes"], "Movie,Series")

    async def test_seasons_and_episodes_are_scoped_to_series(self) -> None:
        seasons = await self.client.seasons(SERIES_ID)
        self.assertEqual(seasons, [
            {"id": OTHER_SEASON_ID, "title": "Specials", "number": 0},
            {"id": SEASON_ID, "title": "Season 1", "number": 1},
        ])
        episodes = await self.client.episodes(SERIES_ID, SEASON_ID)
        self.assertEqual([episode["id"] for episode in episodes], [EPISODE_ONE_ID, EPISODE_ID])
        self.assertEqual(episodes[1], {
                "id": EPISODE_ID,
                "title": "The Example Episode",
                "season_number": 1,
                "episode_number": 2,
                "year": None,
                "rating": 8.2,
                "has_image": True,
                "type": "Episode",
                "resume_seconds": 0,
                "played": False,
                "series_title": None,
                "series_id": None,
                "provider_ids": {},
        })
        self.assertNotIn(VIRTUAL_EPISODE_ID, {episode["id"] for episode in episodes})
        self.assertNotIn(MISSING_EPISODE_ID, {episode["id"] for episode in episodes})
        episode_request = next(
            request for request in self.requests
            if request.url.path == f"/Shows/{SERIES_ID}/Episodes"
            and request.url.params.get("seasonId") == SEASON_ID
        )
        self.assertEqual(episode_request.url.params["userId"], "living-room-user")
        self.assertEqual(episode_request.url.params["enableUserData"], "true")
        with self.assertRaises(JellyfinItemUnavailable):
            await self.client.episodes(SERIES_ID, "44444444444444444444444444444444")

    async def test_personal_rows_use_only_the_tv_user(self) -> None:
        resume = await self.client.continue_watching()
        next_up = await self.client.next_up()
        self.assertEqual(resume[0]["resume_seconds"], 90)
        self.assertEqual(resume[0]["series_title"], "Example Show")
        self.assertEqual(next_up[0]["id"], EPISODE_ONE_ID)
        resume_request = next(r for r in self.requests if r.url.path.endswith("/Items/Resume"))
        next_request = next(r for r in self.requests if r.url.path == "/Shows/NextUp")
        self.assertEqual(resume_request.url.path, "/Users/living-room-user/Items/Resume")
        self.assertEqual(next_request.url.params["userId"], "living-room-user")

    async def test_personal_rows_are_empty_without_the_tv_user(self) -> None:
        async def no_session():
            return None

        original = self.client._tv_session
        self.client._tv_session = no_session
        try:
            self.assertEqual(await self.client.continue_watching(), [])
            self.assertEqual(await self.client.next_up(), [])
        finally:
            self.client._tv_session = original

    async def test_next_episode_skips_current_and_crosses_season(self) -> None:
        session = dict(TV_SESSION)
        session["NowPlayingItem"] = {
            "Id": EPISODE_ID, "Type": "Episode", "SeriesId": SERIES_ID,
            "ParentIndexNumber": 1, "IndexNumber": 2,
        }
        original = self.client._tv_session

        async def current_episode():
            return session

        self.client._tv_session = current_episode
        try:
            item = await self.client.next_episode()
        finally:
            self.client._tv_session = original
        self.assertEqual(item["id"], EPISODE_ONE_ID)

    async def test_next_episode_is_empty_without_a_current_episode(self) -> None:
        self.assertIsNone(await self.client.next_episode())

    async def test_next_episode_needs_real_episode_numbers(self) -> None:
        session = dict(TV_SESSION)
        session["NowPlayingItem"] = {
            "Id": EPISODE_ID, "Type": "Episode", "SeriesId": SERIES_ID,
            "ParentIndexNumber": 1,
        }

        async def episode_without_number():
            return session

        original = self.client._tv_session
        self.client._tv_session = episode_without_number
        try:
            self.assertIsNone(await self.client.next_episode())
        finally:
            self.client._tv_session = original

    async def test_play_episode_targets_tv_session(self) -> None:
        await self.client.play_item(EPISODE_ID)
        request = next(
            request for request in self.requests
            if request.url.path == "/Sessions/tv-session/Playing"
        )
        self.assertEqual(request.url.params["itemIds"], EPISODE_ID)

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
        self.assertEqual(validation.url.params["fields"], "MediaStreams,ProviderIds,PrimaryImageAspectRatio")
        self.assertEqual(validation.url.params["userId"], "living-room-user")
        self.assertEqual(validation.url.params["enableUserData"], "true")
        self.assertFalse(any(request.url.path == f"/Items/{MOVIE_ID}" for request in self.requests))
        request = next(
            request for request in self.requests
            if request.url.path == "/Sessions/tv-session/Playing"
        )
        self.assertEqual(request.url.path, "/Sessions/tv-session/Playing")
        self.assertEqual(request.url.params["playCommand"], "PlayNow")
        self.assertEqual(request.url.params["itemIds"], MOVIE_ID)
        self.assertEqual(request.url.params["startPositionTicks"], "1230000000")
        self.assertEqual(
            self.requests[-1].url.path,
            "/Sessions/tv-session/Playing/Unpause",
        )

    async def test_play_without_a_tv_user_never_uses_saved_progress(self) -> None:
        session = dict(TV_SESSION)
        session.pop("UserId")
        session.pop("UserName")
        session["NowPlayingItem"] = {"Id": EPISODE_ID, "Type": "Episode"}

        async def no_user_session():
            return session

        async def item_with_saved_progress(item_id, user_id=None):
            self.assertIsNone(user_id)
            return {
                "Id": item_id,
                "Type": "Movie",
                "UserData": {"PlaybackPositionTicks": 900_000_000},
            }

        original_session = self.client._tv_session
        original_item = self.client._item
        self.client._tv_session = no_user_session
        self.client._item = item_with_saved_progress
        try:
            start_seconds = await self.client.play_item(MOVIE_ID)
        finally:
            self.client._tv_session = original_session
            self.client._item = original_item
        play = next(request for request in self.requests if request.url.path.endswith("/Playing"))
        self.assertEqual(play.url.params["startPositionTicks"], "0")
        self.assertEqual(start_seconds, 0)

    async def test_play_refuses_series_items(self) -> None:
        with self.assertRaises(JellyfinItemUnavailable):
            await self.client.play_movie(SERIES_ID)
        self.assertFalse(any(r.url.path.endswith("/Playing") for r in self.requests))

    async def test_restart_ignores_saved_or_live_position(self) -> None:
        await self.client.play_item(MOVIE_ID, restart=True)
        request = next(r for r in self.requests if r.url.path.endswith("/Playing"))
        self.assertEqual(request.url.params["startPositionTicks"], "0")

    async def test_absolute_seek_clamps_to_duration(self) -> None:
        seconds = await self.client.seek(MOVIE_ID, 999)
        self.assertEqual(seconds, 270)
        request = self.requests[-1]
        self.assertEqual(request.url.path, "/Sessions/tv-session/Playing/Seek")
        self.assertEqual(request.url.params["seekPositionTicks"], "2700000000")

    async def test_seek_refuses_a_title_that_changed(self) -> None:
        changed = dict(TV_SESSION)
        changed["NowPlayingItem"] = {"Id": EPISODE_ID, "RunTimeTicks": 1}

        async def changed_session():
            return changed

        original = self.client._tv_session
        self.client._tv_session = changed_session
        try:
            with self.assertRaises(JellyfinPlaybackUnavailable):
                await self.client.seek(MOVIE_ID, 30)
        finally:
            self.client._tv_session = original

    async def test_play_refuses_missing_and_virtual_episodes(self) -> None:
        for unavailable in ({"IsMissing": True}, {"IsVirtualItem": True}, {"LocationType": "Virtual"}):
            with self.subTest(unavailable=unavailable):
                requests = []

                def handler(request):
                    requests.append(request)
                    if request.url.path == "/Sessions":
                        return httpx.Response(200, json=[TV_SESSION])
                    return httpx.Response(200, json={"Items": [
                        {"Id": EPISODE_ID, "Type": "Episode", **unavailable}
                    ]})

                client = Jellyfin("http://jellyfin.test", "secret", "HOME",
                                  transport=httpx.MockTransport(handler))
                try:
                    with self.assertRaises(JellyfinItemUnavailable):
                        await client.play_item(EPISODE_ID)
                    self.assertFalse(any(r.method == "POST" for r in requests))
                finally:
                    await client.aclose()

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
