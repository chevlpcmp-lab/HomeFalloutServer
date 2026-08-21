"""Ambient sync, as one boolean.

Hyperion runs on the Proxmox host and drives the living-room lightstrip from
whatever the Jellyfin kiosk is showing (docs/ambient-lighting.md). The app needs
exactly two things from it: is the LED output on, and turn it on or off. Both are
one unauthenticated JSON-RPC call against the LEDDEVICE component.

Nothing here touches device.autoStart — sync stays off by default across Hyperion
restarts, and only this toggle and Hyperion's own UI ever turn it on.
"""

from __future__ import annotations

import httpx

COMPONENT = "LEDDEVICE"


class HyperionError(Exception):
    """Hyperion could not be reached or rejected the command."""


class Hyperion:
    def __init__(self, url: str, timeout: float = 1.8) -> None:
        self._url = url
        self._client = httpx.AsyncClient(timeout=timeout)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _rpc(self, payload: dict) -> dict:
        try:
            response = await self._client.post(self._url, json=payload)
            response.raise_for_status()
            body = response.json()
        except httpx.HTTPError as exc:
            raise HyperionError(f"Hyperion unreachable: {exc.__class__.__name__}") from exc
        if not body.get("success", False):
            raise HyperionError(f"Hyperion refused {payload.get('command')}: {body.get('error')}")
        return body

    async def is_on(self) -> bool:
        body = await self._rpc({"command": "serverinfo"})
        for component in body.get("info", {}).get("components", []):
            if component.get("name") == COMPONENT:
                return bool(component.get("enabled"))
        raise HyperionError("Hyperion serverinfo had no LEDDEVICE component")

    async def set(self, on: bool) -> None:
        await self._rpc({
            "command": "componentstate",
            "componentstate": {"component": COMPONENT, "state": on},
        })
