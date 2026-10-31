"""Local, deterministic Overseer media fixture (run from apps/house).

No network is used: Jellyfin is backed by httpx.MockTransport and HA/Hyperion
are tiny in-process doubles.  Intended for browser QA only.
"""
from __future__ import annotations

import argparse
import asyncio
import sys
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx
import uvicorn
from fastapi import Request
from fastapi.responses import JSONResponse, RedirectResponse

from app import config
from app.db import Store
from app.jellyfin import Jellyfin
from app.main import COOKIE, app
from app.rooms import TV_MEDIA_PLAYER

USER_ID = "0123456789abcdef0123456789abcdef"
MOVIE = "11111111111111111111111111111111"
SERIES = "22222222222222222222222222222222"
S1 = "33333333333333333333333333333333"
S2 = "44444444444444444444444444444444"
E1 = "55555555555555555555555555555555"
E2 = "66666666666666666666666666666666"
E3 = "77777777777777777777777777777777"

mode = {"playback_ack": True, "disconnected": False, "server_error": False, "tv_asleep": False, "account_missing": False}
events: list[dict] = []
player = {"item": MOVIE, "position": 1200, "paused": False}

def item(i: str, typ: str, name: str, **kw) -> dict:
    out = {"Id": i, "Type": typ, "Name": name, "LocationType": "File", "ImageTags": {}}
    out.update(kw)
    return out

MOVIE_ITEM = item(MOVIE, "Movie", "The QA Movie", RunTimeTicks=7200 * 10_000_000, UserData={"PlaybackPositionTicks": 1200 * 10_000_000})
EPISODES = [item(E1, "Episode", "Pilot", SeriesId=SERIES, SeriesName="QA Show", ParentIndexNumber=1, IndexNumber=1, RunTimeTicks=1800 * 10_000_000, UserData={"PlaybackPositionTicks": 90 * 10_000_000}), item(E2, "Episode", "Finale", SeriesId=SERIES, SeriesName="QA Show", ParentIndexNumber=1, IndexNumber=2, RunTimeTicks=1800 * 10_000_000, UserData={"Played": True}), item(E3, "Episode", "Return", SeriesId=SERIES, SeriesName="QA Show", ParentIndexNumber=2, IndexNumber=1, RunTimeTicks=1800 * 10_000_000, UserData={})]

def session() -> dict | None:
    if mode["disconnected"]:
        return None
    current = next((x for x in [MOVIE_ITEM, *EPISODES] if x["Id"] == player["item"]), MOVIE_ITEM)
    current = {**current, "MediaStreams": []}
    return {"Id": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa", "DeviceName": "home", "SupportsRemoteControl": True, **({} if mode["account_missing"] else {"UserId": USER_ID, "UserName": "Living room"}), "NowPlayingItem": current, "PlayState": {"IsPaused": player["paused"], "CanSeek": True, "PositionTicks": player["position"] * 10_000_000}}

async def jellyfin_transport(request: httpx.Request) -> httpx.Response:
    if mode["server_error"]:
        return httpx.Response(503, request=request)
    path, q = request.url.path, dict(request.url.params)
    if path == "/Sessions": return httpx.Response(200, json=[] if mode["disconnected"] else [session()], request=request)
    if path == "/Items":
        ids = q.get("ids"); values = [MOVIE_ITEM, item(SERIES, "Series", "QA Show"), *EPISODES]
        if ids: values = [x for x in values if x["Id"] in ids.split(",")]
        return httpx.Response(200, json={"Items": values}, request=request)
    if path.endswith("/Seasons"): return httpx.Response(200, json={"Items": [item(S1, "Season", "Season 1", IndexNumber=1), item(S2, "Season", "Season 2", IndexNumber=2)]}, request=request)
    if path.endswith("/Episodes"): return httpx.Response(200, json={"Items": [x for x in EPISODES if not q.get("seasonId") or (q["seasonId"] == S1 and x["ParentIndexNumber"] == 1) or (q["seasonId"] == S2 and x["ParentIndexNumber"] == 2)]}, request=request)
    if path.startswith(f"/Users/{USER_ID}/Items/Resume"): return httpx.Response(200, json={"Items": [MOVIE_ITEM, EPISODES[0]]}, request=request)
    if path == "/Shows/NextUp": return httpx.Response(200, json={"Items": [EPISODES[0], EPISODES[2]]}, request=request)
    if path.endswith("/Images/Primary"): return httpx.Response(404, request=request)
    if request.method == "POST":
        events.append({"path": path, "params": dict(request.url.params)})
        if path.endswith("/Playing") and mode["playback_ack"]:
            player["item"] = q.get("itemIds", player["item"]); player["position"] = round(int(q.get("startPositionTicks", 0)) / 10_000_000)
        elif path.endswith("/Unpause"): player["paused"] = False
        elif path.endswith("/Seek"): player["position"] = round(int(q.get("seekPositionTicks", 0)) / 10_000_000)
        return httpx.Response(204, request=request)
    return httpx.Response(404, request=request)

class HA:
    async def aclose(self): pass
    async def states(self): return {TV_MEDIA_PLAYER: await self.state(TV_MEDIA_PLAYER)}
    async def state(self, entity_id): return {"entity_id": entity_id, "state": "off" if mode["tv_asleep"] else "on", "attributes": {"source": "HDMI"}}
    async def call(self, domain, service, **data): events.append({"path": f"/ha/{domain}/{service}", "params": data}); mode["tv_asleep"] = False
class Hyperion:
    async def aclose(self): pass
    async def is_on(self): return False
    async def set(self, on): events.append({"path": "/hyperion/set", "params": {"on": on}})

@asynccontextmanager
async def lifespan(_app):
    with tempfile.TemporaryDirectory(prefix="overseer-fixture-") as td:
        store = Store(str(Path(td) / "fixture.db")); uid = store.create_user("qa", "QA", "qa", is_admin=True, must_change=False); store.grant(uid, "living_room")
        _app.state.store, _app.state.ha, _app.state.hyperion = store, HA(), Hyperion()
        _app.state.jellyfin = Jellyfin("http://fixture", "fixture", "home", transport=httpx.MockTransport(jellyfin_transport))
        yield
        await _app.state.jellyfin.aclose()

app.router.lifespan_context = lifespan

@app.post("/__qa/mode")
async def qa_mode(request: Request):
    body = await request.json(); mode.update({k: bool(v) for k, v in body.items() if k in mode});
    if body.get("reset"): events.clear(); player.update(item=MOVIE, position=1200, paused=False)
    return {"mode": mode, "events": events}
@app.get("/__qa/events")
async def qa_events(): return {"events": events}
@app.get("/__qa/login")
async def qa_login(request: Request):
    token = app.state.store.create_session(app.state.store.find_user("qa").id)
    response = RedirectResponse("/jellyfin", status_code=303); response.set_cookie(COOKIE, token, httponly=True); return response

if __name__ == "__main__":
    parser = argparse.ArgumentParser(); parser.add_argument("--port", type=int, default=18765); args = parser.parse_args()
    uvicorn.run(app, host="127.0.0.1", port=args.port)
