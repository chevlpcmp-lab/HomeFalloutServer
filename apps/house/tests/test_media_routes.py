import unittest
import httpx
import media_fixture as f

class MediaRoutesTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        f.mode.update({"playback_ack": True, "disconnected": False, "server_error": False, "tv_asleep": False, "account_missing": False})
        f.events.clear(); f.player.update(item=f.MOVIE, position=1200, paused=False)
        self.cm = f.lifespan(f.app); await self.cm.__aenter__()
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=f.app), base_url="http://test")
    async def asyncTearDown(self):
        await self.client.aclose(); await self.cm.__aexit__(None, None, None)
    async def signed(self):
        r = await self.client.get("/__qa/login", follow_redirects=False); self.client.cookies.update(r.cookies)
    async def test_authentication_and_login(self):
        self.assertEqual((await self.client.get("/api/jellyfin/recent")).status_code, 401)
        self.assertEqual((await self.client.post("/api/jellyfin/connect")).status_code, 401)
        await self.signed(); self.assertEqual((await self.client.get("/jellyfin")).status_code, 200)
    async def test_play_restart_forwarded_and_result(self):
        await self.signed(); r = await self.client.post("/api/jellyfin/play", json={"item_id": f.MOVIE, "restart": True})
        self.assertEqual(r.status_code, 200); self.assertEqual(r.json()["start_seconds"], 0)
        self.assertTrue(any(e["path"].endswith("/Playing") and e["params"]["startPositionTicks"] == "0" for e in f.events))
    async def test_seek_validation_stale_and_nonfinite(self):
        await self.signed()
        self.assertEqual((await self.client.post("/api/jellyfin/seek", json={"item_id": f.MOVIE, "seconds": -1})).status_code, 422)
        self.assertEqual((await self.client.post("/api/jellyfin/seek", json={"item_id": f.MOVIE, "seconds": "nope"})).status_code, 422)
        body = '{"item_id":"' + f.MOVIE + '","seconds":Infinity}'
        self.assertEqual((await self.client.post("/api/jellyfin/seek", content=body, headers={"content-type":"application/json"})).status_code, 400)
        self.assertEqual((await self.client.post("/api/jellyfin/seek", json={"item_id": "2" * 32, "seconds": 1})).status_code, 409)
    async def test_connect_awake_is_noop_and_asleep_calls_ha(self):
        await self.signed(); r = await self.client.post("/api/jellyfin/connect")
        self.assertEqual(r.status_code, 200); self.assertFalse(f.events)
        f.mode["tv_asleep"] = True; r = await self.client.post("/api/jellyfin/connect")
        self.assertEqual(r.status_code, 200); self.assertTrue(any(e["path"].startswith("/ha/") for e in f.events))
        self.assertFalse(any(e["path"].startswith("/Sessions/") for e in f.events))
