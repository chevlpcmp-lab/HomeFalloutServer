"""Overseer: the living room, on a phone.

Server-rendered pages with a little JS on top. Every control action is a POST here,
and every handler re-checks the caller's session and room permissions before it
touches Home Assistant — the browser hides what you cannot use, but this file is
what enforces it.
"""

from __future__ import annotations

import asyncio
import secrets
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from markupsafe import Markup
from fastapi.responses import JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, Field

from . import config
from .db import SESSION_TTL, Store, User
from .ha import HAError, HomeAssistant
from .hyperion import Hyperion, HyperionError
from .rooms import (
    LIGHTS, REMOTE_KEYS, ROOMS, SWATCHES,
    TV_MEDIA_PLAYER, TV_NAME, TV_REMOTE, TV_SOURCES,
    LightDef, RoomDef,
)

COOKIE = "house_session"

# Anything the Samsung integration reports as not-off counts as on; deep standby
# shows up as "off", "unavailable", or a missing entity, and all three mean the
# same thing to a person holding a remote.
TV_ON_STATES = {"on", "idle", "playing", "paused", "buffering"}

# username, display name, admin, rooms — the four residents, seeded at first boot.
SEED_PEOPLE = (
    ("charles", "Charles", True, ("living_room", "charles_charlotte")),
    ("charlotte", "Charlotte", False, ("living_room", "charles_charlotte")),
    ("zach", "Zach", False, ("living_room",)),
    ("tim", "Tim", False, ("living_room",)),
)

AVATAR_COLORS = {
    "charles": "#FF7A2F", "charlotte": "#E4508A", "zach": "#4C8DF5", "tim": "#3FC7A8",
}

_APP_DIR = Path(__file__).resolve().parent
templates = Jinja2Templates(directory=_APP_DIR / "templates")


def _icon(name: str, size: int = 20) -> Markup:
    """Inline SVG from the sprite in _icons.html. Icons are one stroke weight and
    never emoji — Charles asked for this explicitly."""
    return Markup(
        f'<svg class="ic" width="{size}" height="{size}" aria-hidden="true">'
        f'<use href="#i-{name}"/></svg>'
    )


templates.env.globals["icon"] = _icon


def _seed_if_empty(store: Store) -> None:
    if store.all_users():
        return
    lines = []
    for username, display, is_admin, rooms in SEED_PEOPLE:
        password = secrets.token_urlsafe(9)
        user_id = store.create_user(username, display, password, is_admin=is_admin)
        for room in rooms:
            store.grant(user_id, room)
        lines.append(f"  {username:<11}{password}{'   (admin)' if is_admin else ''}")
    bar = "=" * 60
    print("\n".join([
        bar,
        "  OVERSEER FIRST BOOT — one-time passwords",
        *lines,
        "  Each stops working at first sign-in, when a real password",
        "  replaces it. This block prints once and is stored nowhere.",
        bar,
    ]), flush=True)


@asynccontextmanager
async def lifespan(app: FastAPI):
    config.require_ha_token()
    app.state.store = Store(config.HOUSE_DB)
    _seed_if_empty(app.state.store)
    app.state.store.purge_expired_sessions()
    app.state.ha = HomeAssistant(config.HA_URL, config.HA_TOKEN)
    app.state.hyperion = Hyperion(config.HYPERION_URL)
    yield
    await app.state.ha.aclose()
    await app.state.hyperion.aclose()


app = FastAPI(title="Overseer", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=_APP_DIR / "static"), name="static")


# ---------------------------------------------------------------- auth plumbing

class _RedirectTo(Exception):
    def __init__(self, target: str) -> None:
        self.target = target


@app.exception_handler(_RedirectTo)
async def _redirect_handler(request: Request, exc: _RedirectTo):
    return RedirectResponse(exc.target, status_code=303)


def _session_user(request: Request) -> User | None:
    return request.app.state.store.session_user(request.cookies.get(COOKIE))


def page_user(request: Request) -> User:
    """Pages bounce anonymous visitors to sign-in, and everyone with a one-time
    password to the change form, until both are dealt with."""
    user = _session_user(request)
    if user is None:
        raise _RedirectTo("/login")
    if user.must_change_password and request.url.path != "/password":
        raise _RedirectTo("/password")
    return user


def api_user(request: Request) -> User:
    user = _session_user(request)
    if user is None:
        raise HTTPException(401, "Not signed in")
    if user.must_change_password:
        raise HTTPException(403, "Change your password first")
    return user


def admin_user(request: Request) -> User:
    user = page_user(request)
    if not user.is_admin:
        raise HTTPException(403, "Admin only")
    return user


def _rooms_for(request: Request, user: User) -> list[RoomDef]:
    if user.is_admin:
        return list(ROOMS.values())
    allowed = request.app.state.store.rooms_for(user.id)
    return [room for room in ROOMS.values() if room.key in allowed]


def _require_room(request: Request, user: User, room_key: str) -> RoomDef:
    room = ROOMS.get(room_key)
    if room is None:
        raise HTTPException(404, "No such room")
    if not user.is_admin and room_key not in request.app.state.store.rooms_for(user.id):
        # The permission check, not a hidden button. 403 by design.
        raise HTTPException(403, "Not your room")
    return room


async def _refuse_if_screen_holds(request: Request, lights: tuple[LightDef, ...]) -> None:
    """While ambient sync streams, Hue Entertainment owns the lightstrip
    exclusively — writes would succeed and change nothing. Refusing here keeps the
    API as honest as the UI. If Hyperion cannot be asked, it is treated as off,
    because off is its resting state (autoStart is disabled)."""
    if not any(light.synced for light in lights):
        return
    try:
        held = await request.app.state.hyperion.is_on()
    except HyperionError:
        return
    if held:
        raise HTTPException(409, "The screen is driving this light while sync is on")


# ---------------------------------------------------------------- state assembly

def _light_state(states: dict | None, light: LightDef, held: bool) -> dict:
    row = (states or {}).get(light.entity)
    on = bool(row and row.get("state") == "on")
    brightness = None
    if on:
        raw = (row.get("attributes") or {}).get("brightness")
        if isinstance(raw, (int, float)):
            brightness = max(1, round(raw / 2.55))
    if held and light.synced:
        sub = "Following the screen"
    else:
        level = f"{brightness}%" if brightness is not None else "On"
        state_text = level if on else "Off"
        sub = f"{light.kind} · {state_text}" if light.kind else (
            f"On · {brightness}%" if on and brightness is not None else state_text
        )
    return {
        "slug": light.slug, "name": light.name, "kind": light.kind,
        "on": on, "brightness": brightness,
        "held": held and light.synced, "sub": sub,
    }


async def gather_state(request: Request, user: User) -> dict:
    ha: HomeAssistant = request.app.state.ha
    hyperion: Hyperion = request.app.state.hyperion
    states, sync_on = await asyncio.gather(
        ha.states(), hyperion.is_on(), return_exceptions=True,
    )
    ha_ok = not isinstance(states, BaseException)
    sync_ok = not isinstance(sync_on, BaseException)
    if not ha_ok:
        states = None
    held = sync_ok and sync_on is True

    tv_row = (states or {}).get(TV_MEDIA_PLAYER)
    tv_attrs = (tv_row or {}).get("attributes") or {}
    tv_on = bool(tv_row and tv_row.get("state") in TV_ON_STATES)
    volume = tv_attrs.get("volume_level")
    rooms = []
    lights_on = 0
    for room in _rooms_for(request, user):
        room_row = (states or {}).get(room.group_entity)
        room_held = held and any(light.synced for light in room.lights)
        lights = [_light_state(states, light, held) for light in room.lights]
        lights_on += sum(1 for light in lights if light["on"])
        rooms.append({
            "key": room.key, "name": room.name,
            "on": bool(room_row and room_row.get("state") == "on"),
            "held": room_held, "lights": lights,
        })

    if not ha_ok:
        summary = "Can't reach Home Assistant"
    else:
        words = ("All lights off", "One light on", "Two lights on",
                 "Three lights on", "Four lights on")
        summary = words[lights_on] if lights_on < len(words) else f"{lights_on} lights on"

    return {
        "tv": {
            "reachable": ha_ok,
            "on": tv_on,
            "name": TV_NAME,
            "volume": round(volume * 100) if isinstance(volume, (int, float)) else None,
            "muted": bool(tv_attrs.get("is_volume_muted")),
            "source": tv_attrs.get("source"),
        },
        "sync": {"reachable": sync_ok, "on": sync_on if sync_ok else None},
        "rooms": rooms,
        "summary": summary,
    }


def render(request: Request, template: str, user: User | None, **context):
    return templates.TemplateResponse(request, template, {
        "user": user,
        "avatar_colors": AVATAR_COLORS,
        **context,
    })


def _daypart() -> str:
    hour = time.localtime().tm_hour
    if 5 <= hour < 12:
        return "morning"
    if 12 <= hour < 17:
        return "afternoon"
    return "evening"


# ---------------------------------------------------------------- pages

@app.get("/healthz")
async def healthz():
    return {"ok": True}


@app.get("/login")
async def login_page(request: Request):
    if _session_user(request):
        return RedirectResponse("/", status_code=303)
    return render(request, "login.html", None)


@app.post("/login")
async def login(request: Request, username: str = Form(""), password: str = Form("")):
    store: Store = request.app.state.store
    user = await asyncio.to_thread(store.verify, username.strip(), password)
    if user is None:
        return render(request, "login.html", None, error="Wrong name or password.")
    token = store.create_session(user.id)
    target = "/password" if user.must_change_password else "/"
    response = RedirectResponse(target, status_code=303)
    response.set_cookie(COOKIE, token, max_age=SESSION_TTL, httponly=True, samesite="lax")
    return response


@app.post("/logout")
async def logout(request: Request):
    request.app.state.store.destroy_session(request.cookies.get(COOKIE))
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(COOKIE)
    return response


@app.get("/password")
async def password_page(request: Request, user: User = Depends(page_user)):
    return render(request, "password.html", user)


@app.post("/password")
async def password_change(
    request: Request,
    user: User = Depends(page_user),
    current: str = Form(""),
    new: str = Form(""),
    repeat: str = Form(""),
):
    store: Store = request.app.state.store
    error = None
    if await asyncio.to_thread(store.verify, user.username, current) is None:
        error = "That current password isn't right."
    elif len(new) < 8:
        error = "Eight characters at least."
    elif new != repeat:
        error = "The two copies don't match."
    if error:
        return render(request, "password.html", user, error=error)
    await asyncio.to_thread(store.set_password, user.id, new, False)
    # A password change orphans every existing cookie, including the one that
    # made this request — so mint a fresh session for this browser.
    store.destroy_user_sessions(user.id)
    token = store.create_session(user.id)
    response = RedirectResponse("/", status_code=303)
    response.set_cookie(COOKIE, token, max_age=SESSION_TTL, httponly=True, samesite="lax")
    return response


@app.get("/")
async def remote_page(request: Request, user: User = Depends(page_user)):
    state = await gather_state(request, user)
    return render(request, "remote.html", user, page="remote", state=state)


@app.get("/lights")
async def lights_page(request: Request, user: User = Depends(page_user)):
    state = await gather_state(request, user)
    return render(
        request, "lights.html", user, page="lights",
        state=state, daypart=_daypart(),
    )


@app.get("/lights/{slug}")
async def light_page(request: Request, slug: str, user: User = Depends(page_user)):
    if slug not in LIGHTS:
        raise HTTPException(404, "No such light")
    room, light = LIGHTS[slug]
    _require_room(request, user, room.key)
    state = await gather_state(request, user)
    room_state = next(r for r in state["rooms"] if r["key"] == room.key)
    light_state = next(l for l in room_state["lights"] if l["slug"] == slug)
    return render(
        request, "light.html", user, page="lights",
        state=state, light=light_state, swatches=SWATCHES,
    )


@app.get("/people")
async def people_page(request: Request, u: str | None = None, user: User = Depends(admin_user)):
    return _render_people(request, user, selected_name=u)


def _render_people(request: Request, admin: User, selected_name: str | None = None,
                   revealed: dict | None = None):
    store: Store = request.app.state.store
    people = []
    selected = None
    for person in store.all_users():
        room_keys = store.rooms_for(person.id)
        if person.is_admin:
            sub = "Admin · all rooms"
        elif person.must_change_password:
            sub = "Hasn't signed in yet"
        else:
            names = [room.name for room in ROOMS.values() if room.key in room_keys]
            sub = ", ".join(names) if names else "No rooms yet"
        row = {"user": person, "sub": sub, "rooms": room_keys}
        people.append(row)
        if selected_name and person.username == selected_name and not person.is_admin:
            selected = row
    words = ("No", "One", "Two", "Three", "Four", "Five", "Six")
    return render(
        request, "people.html", admin, page="people",
        people=people, selected=selected, all_rooms=list(ROOMS.values()),
        revealed=revealed,
        account_word=words[len(people)] if len(people) < len(words) else str(len(people)),
    )


@app.post("/people/{user_id}/reset")
async def reset_password(request: Request, user_id: int, admin: User = Depends(admin_user)):
    store: Store = request.app.state.store
    target = store.get_user(user_id)
    if target is None:
        raise HTTPException(404, "No such person")
    password = secrets.token_urlsafe(9)
    await asyncio.to_thread(store.set_password, target.id, password, True)
    store.destroy_user_sessions(target.id)
    return _render_people(
        request, admin, selected_name=target.username,
        revealed={"name": target.display_name, "password": password},
    )


class PermissionChange(BaseModel):
    room: str
    granted: bool


@app.post("/people/{user_id}/rooms")
async def change_rooms(request: Request, user_id: int, change: PermissionChange,
                       admin: User = Depends(admin_user)):
    store: Store = request.app.state.store
    target = store.get_user(user_id)
    if target is None:
        raise HTTPException(404, "No such person")
    if target.is_admin:
        raise HTTPException(400, "Admins hold every room")
    if change.room not in ROOMS:
        raise HTTPException(404, "No such room")
    if change.granted:
        store.grant(target.id, change.room)
    else:
        store.revoke(target.id, change.room)
    return {"ok": True, "granted": change.granted}


# ---------------------------------------------------------------- control API

def _ha(request: Request) -> HomeAssistant:
    return request.app.state.ha


@app.exception_handler(HAError)
async def _ha_error(request: Request, exc: HAError):
    return JSONResponse({"detail": str(exc)}, status_code=502)


@app.exception_handler(HyperionError)
async def _hyperion_error(request: Request, exc: HyperionError):
    return JSONResponse({"detail": str(exc)}, status_code=502)


@app.get("/api/state")
async def api_state(request: Request, user: User = Depends(api_user)):
    return await gather_state(request, user)


@app.post("/api/tv/power")
async def tv_power(request: Request, user: User = Depends(api_user)):
    # Toggle against what the TV actually reports rather than what the client
    # believes, so two phones cannot fight it into the wrong state.
    row = await _ha(request).state(TV_MEDIA_PLAYER)
    on = bool(row and row.get("state") in TV_ON_STATES)
    service = "turn_off" if on else "turn_on"
    await _ha(request).call("media_player", service, entity_id=TV_MEDIA_PLAYER)
    return {"ok": True, "on": not on}


class KeyPress(BaseModel):
    key: str


@app.post("/api/tv/key")
async def tv_key(request: Request, press: KeyPress, user: User = Depends(api_user)):
    if press.key not in REMOTE_KEYS:
        raise HTTPException(400, "Unknown key")
    await _ha(request).call("remote", "send_command",
                            entity_id=TV_REMOTE, command=press.key)
    return {"ok": True}


class SourceChoice(BaseModel):
    source: Literal["jellyfin", "tv"]


@app.post("/api/tv/source")
async def tv_source(request: Request, choice: SourceChoice, user: User = Depends(api_user)):
    # The TV's source_list is exactly ["TV", "HDMI"]. Whether HDMI lands on
    # HDMI 1 or cycles inputs is still untested (BRIEF §10) — if it cycles, the
    # fallback is KEY_SOURCE + arrow navigation via remote.send_command.
    target = TV_SOURCES[choice.source]
    await _ha(request).call("media_player", "select_source",
                            entity_id=TV_MEDIA_PLAYER, source=target)
    return {"ok": True, "source": target}


class VolumeLevel(BaseModel):
    level: int = Field(ge=0, le=100)


@app.post("/api/tv/volume")
async def tv_volume(request: Request, volume: VolumeLevel, user: User = Depends(api_user)):
    # Absolute, not a counter: the TV's UPnP rendering service accepts a set and
    # reads back, so the bar always shows a real position.
    await _ha(request).call("media_player", "volume_set",
                            entity_id=TV_MEDIA_PLAYER, volume_level=volume.level / 100)
    return {"ok": True, "volume": volume.level}


@app.post("/api/tv/mute")
async def tv_mute(request: Request, user: User = Depends(api_user)):
    row = await _ha(request).state(TV_MEDIA_PLAYER)
    muted = bool(((row or {}).get("attributes") or {}).get("is_volume_muted"))
    await _ha(request).call("media_player", "volume_mute",
                            entity_id=TV_MEDIA_PLAYER, is_volume_muted=not muted)
    return {"ok": True, "muted": not muted}


class SyncSwitch(BaseModel):
    on: bool


@app.post("/api/sync")
async def sync_switch(request: Request, switch: SyncSwitch, user: User = Depends(api_user)):
    await request.app.state.hyperion.set(switch.on)
    return {"ok": True, "on": switch.on}


class LightChange(BaseModel):
    on: bool | None = None
    brightness: int | None = Field(default=None, ge=1, le=100)
    color: str | None = Field(default=None, pattern=r"^#[0-9A-Fa-f]{6}$")


@app.post("/api/lights/{slug}")
async def light_change(request: Request, slug: str, change: LightChange,
                       user: User = Depends(api_user)):
    if slug not in LIGHTS:
        raise HTTPException(404, "No such light")
    room, light = LIGHTS[slug]
    _require_room(request, user, room.key)
    await _refuse_if_screen_holds(request, (light,))
    if change.on is False:
        await _ha(request).call("light", "turn_off", entity_id=light.entity)
        return {"ok": True, "on": False}
    data: dict = {"entity_id": light.entity}
    if change.brightness is not None:
        data["brightness_pct"] = change.brightness
    if change.color is not None:
        raw = change.color.lstrip("#")
        data["rgb_color"] = [int(raw[i:i + 2], 16) for i in (0, 2, 4)]
    await _ha(request).call("light", "turn_on", **data)
    return {"ok": True, "on": True}


class RoomSwitch(BaseModel):
    on: bool


@app.post("/api/rooms/{room_key}")
async def room_switch(request: Request, room_key: str, switch: RoomSwitch,
                      user: User = Depends(api_user)):
    room = _require_room(request, user, room_key)
    await _refuse_if_screen_holds(request, room.lights)
    service = "turn_on" if switch.on else "turn_off"
    await _ha(request).call("light", service, entity_id=room.group_entity)
    return {"ok": True, "on": switch.on}
