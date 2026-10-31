import unittest
import asyncio
import httpx

from app.downloads import Downloads


class Jellyfin:
    async def search_media(self, query):
        return [{"id": "a" * 32, "title": "Arr Movie", "type": "Movie", "provider_ids": {"tmdb": "7"}}]
    async def recent_media(self): return []
    async def seasons(self, series_id): return []
    async def episodes(self, series_id, season_id): return []


class DownloadsTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.calls = []
        def handler(request):
            self.calls.append(request)
            path = request.url.path
            if request.url.host == "radarr" and path.endswith("/api/v3/movie"):
                return httpx.Response(200, json=[{"id": 10, "title": "Arr Movie", "tmdbId": 7, "hasFile": True}])
            if request.url.host == "sonarr" and path.endswith("/api/v3/series"):
                return httpx.Response(200, json=[{"id": 20, "title": "Show", "tvdbId": 11}])
            if request.url.host == "radarr" and path.endswith("/api/v3/queue"): return httpx.Response(200, json={"records": []})
            if request.url.host == "sonarr" and path.endswith("/api/v3/queue"):
                return httpx.Response(200, json={"records": [{"seriesId": 20, "seasonNumber": 1, "downloadId": "ABC", "status": "downloading"}]})
            if request.url.host == "sonarr" and path.endswith("/api/v3/episode"):
                return httpx.Response(200, json=[{"id": 30, "seriesId": 20, "seasonNumber": 1, "episodeNumber": 2, "title": "Ep", "hasFile": False}])
            if path.endswith("/api/v2/torrents/info"):
                return httpx.Response(200, json=[{"hash": "abc", "progress": .5, "dlspeed": 50, "eta": 9, "state": "downloading"}])
            return httpx.Response(404)
        self.client = Downloads("http://radarr", "r", "http://sonarr", "s", "http://qbit", transport=httpx.MockTransport(handler))

    async def asyncTearDown(self): await self.client.aclose()

    async def test_exact_provider_match_is_ready_and_safe(self):
        items = await self.client.search_media("arr", Jellyfin())
        movie = next(x for x in items if x["title"] == "Arr Movie")
        self.assertEqual(movie["id"], "a" * 32)
        self.assertEqual(movie["media_key"], "radarr-10")
        self.assertTrue(movie["playable"])
        self.assertEqual(movie["availability"]["state"], "ready")
        self.assertNotIn("X-Api-Key", str(items))

    async def test_season_pack_uses_qbit_exact_download_id(self):
        episodes = await self.client.episodes("sonarr-20", "sonarr-20-season-1", Jellyfin())
        self.assertEqual(episodes[0]["availability"], {"state": "downloading", "progress": 50.0, "speed_bps": 50.0, "eta_seconds": 9, "message": ""})
        self.assertFalse(episodes[0]["playable"])

    async def test_arr_failure_never_claims_missing(self):
        broken = Downloads("http://broken", "r", "", "", transport=httpx.MockTransport(lambda r: httpx.Response(500)))
        try:
            items = await broken.search_media("whatever", Jellyfin())
            self.assertEqual(items[0]["availability"]["state"], "ready")
        finally: await broken.aclose()


class EmptyJellyfin(Jellyfin):
    async def search_media(self, query):
        return []


class AvailabilityRegressionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.failed = False
        self.queue_failed = False
        self.qstate = "downloading"
        self.progress = .64
        self.calls = []
        def handler(request):
            self.calls.append(request)
            path = request.url.path
            if path == "/api/v3/movie":
                if self.failed: return httpx.Response(503)
                return httpx.Response(200, json=[{"id": 1, "title": "Tracked Movie", "tmdbId": 7, "hasFile": False}])
            if path == "/api/v3/series":
                return httpx.Response(200, json=[{"id": 2, "title": "Tracked Show", "tmdbId": 7, "tvdbId": 0}])
            if path == "/api/v3/queue":
                if self.queue_failed: return httpx.Response(503)
                return httpx.Response(200, json={"records": [{"movieId": 1, "downloadId": "private-hash", "size": 100, "sizeleft": 36,
                    "status": "downloading", "errorMessage": "/private/path/secret", "title": "private-release"}] if request.url.host == "radarr" else []})
            if path == "/api/v2/torrents/info":
                return httpx.Response(200, json=[{"hash": "private-hash", "state": self.qstate, "progress": self.progress, "eta": 8640000}])
            return httpx.Response(404)
        self.client = Downloads("http://radarr", "r", "http://sonarr", "s", "http://qbit", transport=httpx.MockTransport(handler))

    async def asyncTearDown(self):
        await self.client.aclose()

    async def test_movie_and_show_provider_namespaces_do_not_collide(self):
        rows = await self.client.search_media("Tracked", Jellyfin())
        show = next(x for x in rows if x["type"] == "Series")
        self.assertEqual(show["id"], "sonarr-2")
        self.assertFalse(show["playable"])
        self.assertNotIn("tvdb", show["provider_ids"])

    async def test_cached_catalog_failure_becomes_unknown(self):
        await self.client.search_media("Tracked", EmptyJellyfin())
        self.failed = True
        self.client.cached_at = 0
        rows = await self.client.search_media("Tracked", EmptyJellyfin())
        movie = next(x for x in rows if x["type"] == "Movie")
        self.assertEqual(movie["availability"]["state"], "unknown")
        self.assertFalse(movie["playable"])

    async def test_queue_failure_does_not_claim_missing(self):
        self.queue_failed = True
        rows = await self.client.search_media("Tracked", EmptyJellyfin())
        movie = next(x for x in rows if x["type"] == "Movie")
        self.assertEqual(movie["availability"]["state"], "unknown")

    async def test_qbit_states_and_sensitive_details(self):
        for qstate, progress, expected in [("downloading", .64, "downloading"), ("stoppedDL", .64, "paused"),
                                           ("stalledDL", .64, "stalled"), ("queuedUP", 1, "importing"), ("missingFiles", .64, "error")]:
            with self.subTest(qstate=qstate):
                self.qstate, self.progress = qstate, progress
                self.client.cached_at = 0
                rows = await self.client.search_media("Tracked", EmptyJellyfin())
                movie = next(x for x in rows if x["type"] == "Movie")
                self.assertEqual(movie["availability"]["state"], expected)
                self.assertIsNone(movie["availability"]["eta_seconds"])
                for private in ("private-hash", "/private/path", "private-release"):
                    self.assertNotIn(private, str(rows))
        self.assertTrue(all(r.method == "GET" for r in self.calls))

    async def test_concurrent_reads_share_refresh_and_episodes_stay_lazy(self):
        await asyncio.gather(*(self.client.snapshot() for _ in range(8)))
        self.assertEqual(sum(r.url.path == "/api/v3/movie" for r in self.calls), 1)
        self.assertFalse(any(r.url.path == "/api/v3/episode" for r in self.calls))
