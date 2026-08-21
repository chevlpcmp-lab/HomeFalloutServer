"""The house, spelled out.

This is the registry of everything Overseer is allowed to touch, keyed by the short
slugs that appear in URLs and permissions. Entity IDs were verified against the live
Home Assistant on 2026-08-20 (apps/house/BRIEF.md); two further Hue entities exist in
HA but are stale hardware Charles no longer owns, and are deliberately absent here.

Keeping the registry in code rather than discovering entities at runtime is the
point, not a shortcut: permissions attach to these keys, and a light that is not
listed cannot be controlled through this app no matter what appears in HA.
"""

from __future__ import annotations

from dataclasses import dataclass

TV_MEDIA_PLAYER = "media_player.living_room_television_salon"
TV_REMOTE = "remote.living_room_television_salon"
TV_NAME = "Television Salon"

# The two entries in the TV's source_list — verified to be the whole list.
TV_SOURCES = {"jellyfin": "HDMI", "tv": "TV"}

# Everything remote.send_command may be asked to send. The Samsung integration
# accepts more, but the app only ever needs what its buttons show.
REMOTE_KEYS = {
    "KEY_UP", "KEY_DOWN", "KEY_LEFT", "KEY_RIGHT", "KEY_ENTER",
    "KEY_RETURN", "KEY_HOME", "KEY_MENU", "KEY_SOURCE",
}


@dataclass(frozen=True)
class LightDef:
    slug: str          # URL / API key
    entity: str        # HA entity id
    name: str
    kind: str | None = None   # extra label under the name ("Lightstrip")
    synced: bool = False      # held exclusively by Hue Entertainment while ambient sync runs


@dataclass(frozen=True)
class RoomDef:
    key: str           # permission key, matches db.permissions.room_key
    name: str
    group_entity: str  # the Hue room group, for one-switch control
    lights: tuple[LightDef, ...]


ROOMS: dict[str, RoomDef] = {
    "living_room": RoomDef(
        key="living_room",
        name="Living room",
        group_entity="light.living_room_living_room",
        lights=(
            LightDef(
                slug="living-room-wall",
                entity="light.living_room_living_room_wall",
                name="Living room wall",
                kind="Lightstrip",
                synced=True,  # the bulb Hyperion drives
            ),
        ),
    ),
    # The Hue room is really called "Charles & Charlotte<3" — hence the _3 in the
    # entity ids. The heart stays in the bridge; the app label does without it.
    "charles_charlotte": RoomDef(
        key="charles_charlotte",
        name="Charles & Charlotte",
        group_entity="light.charles_charlotte_3_charles_charlotte_3",
        lights=(
            LightDef(
                slug="main-lamp",
                entity="light.charles_charlotte_3_main_lamp",
                name="Main Lamp",
            ),
            LightDef(
                slug="main-lamp-secondary",
                entity="light.charles_charlotte_3_main_lamp_secondary",
                name="Main Lamp Secondary",
            ),
            LightDef(
                slug="bedroom",
                entity="light.charles_charlotte_3_bedroom",
                name="Bedroom",
            ),
        ),
    ),
}

# slug -> (room, light), for permission checks and URL lookups.
LIGHTS: dict[str, tuple[RoomDef, LightDef]] = {
    light.slug: (room, light) for room in ROOMS.values() for light in room.lights
}

# The colour swatches offered on a light's page. All four bulbs take xy colour, so
# these are plain RGB; the last one is a warm white rather than a hue.
SWATCHES = ("#FFB86B", "#FF6B4A", "#E4508A", "#8B6BF0", "#4C8DF5", "#3FC7A8", "#F4EEE2")
