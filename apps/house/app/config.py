"""Environment-driven settings.

Everything configurable arrives as an environment variable, because the deployment
delivers credentials through the `house-secrets` Secret and everything else is a
plain `env:` entry. Defaults are the in-cluster values so the image runs unmodified
there; local development overrides HA_URL and HA_TOKEN.
"""

from __future__ import annotations

import os

# Home Assistant. The Service resolves to the hostNetwork pod on the apps node,
# so this works from inside the cluster.
HA_URL = os.environ.get("HA_URL", "http://home-assistant.media.svc.cluster.local:8123")
HA_TOKEN = os.environ.get("HA_TOKEN", "")

# Jellyfin remote control. The API key is kept server-side and the client module
# restricts it to the configured TV device and an explicit command allow-list.
JELLYFIN_URL = os.environ.get("JELLYFIN_URL", "http://jellyfin.media.svc.cluster.local:8096")
JELLYFIN_API_KEY = os.environ.get("JELLYFIN_API_KEY", "")
JELLYFIN_DEVICE_NAME = os.environ.get("JELLYFIN_DEVICE_NAME", "home")

# Read-only availability tracking. Missing credentials leave Jellyfin browsing usable.
RADARR_URL = os.environ.get("RADARR_URL", "http://radarr.media.svc.cluster.local")
RADARR_API_KEY = os.environ.get("RADARR_API_KEY", "")
SONARR_URL = os.environ.get("SONARR_URL", "http://sonarr.media.svc.cluster.local")
SONARR_API_KEY = os.environ.get("SONARR_API_KEY", "")
QBIT_URL = os.environ.get("QBIT_URL", "http://qbittorrent.media.svc.cluster.local")
QBIT_USERNAME = os.environ.get("QBIT_USERNAME", "")
QBIT_PASSWORD = os.environ.get("QBIT_PASSWORD", "")

# Hyperion runs on the Proxmox host, not in the cluster, and takes this one
# unauthenticated JSON-RPC call. See docs/ambient-lighting.md.
HYPERION_URL = os.environ.get("HYPERION_URL", "http://10.0.0.254:8090/json-rpc")

# SQLite on the PVC. The default keeps a relative path with a real directory in it
# because db.Store makedirs the dirname.
HOUSE_DB = os.environ.get("HOUSE_DB", "data/house.db")

# Reserved. Sessions are unforgeable random rows in SQLite and nothing is signed
# today; this exists so a future signed-cookie or CSRF-token change is a code
# change, not a secret-rotation ceremony.
SESSION_SECRET = os.environ.get("SESSION_SECRET", "")


def require_ha_token() -> None:
    """Refuse to start without a token — a pod that cannot talk to Home Assistant
    should crashloop visibly, not serve a remote where every button fails."""
    if not HA_TOKEN:
        raise RuntimeError(
            "HA_TOKEN is not set. In the cluster it comes from the house-secrets "
            "Secret; locally, export a Home Assistant long-lived access token."
        )


def require_jellyfin_key() -> None:
    """Refuse to present a half-configured Jellyfin remote."""
    if not JELLYFIN_API_KEY:
        raise RuntimeError(
            "JELLYFIN_API_KEY is not set. In the cluster it comes from the "
            "homarr-secrets Secret."
        )
