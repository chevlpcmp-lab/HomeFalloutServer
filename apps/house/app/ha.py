"""A thin Home Assistant REST client.

Overseer talks to nothing but Home Assistant (and Hyperion, which has its own
module): one base URL, one long-lived token, two endpoints. httpx keeps a pooled
connection so a button press costs one round trip on a warm socket.

The timeout is deliberately shorter than the front-end's 2-second held-button
release: a slow Home Assistant should surface as a failed press, not a button that
un-sticks on its own while the request is still in flight.
"""

from __future__ import annotations

from typing import Any

import httpx


class HAError(Exception):
    """Home Assistant could not be reached or refused the call."""


class HomeAssistant:
    def __init__(self, url: str, token: str, timeout: float = 1.8) -> None:
        self._client = httpx.AsyncClient(
            base_url=url.rstrip("/"),
            headers={"Authorization": f"Bearer {token}"},
            timeout=timeout,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _request(self, method: str, path: str, json: dict | None = None) -> Any:
        try:
            response = await self._client.request(method, path, json=json)
        except httpx.HTTPError as exc:
            raise HAError(f"Home Assistant unreachable: {exc.__class__.__name__}") from exc
        if response.status_code >= 400:
            raise HAError(f"Home Assistant answered {response.status_code} for {path}")
        return response.json()

    async def states(self) -> dict[str, dict]:
        """Every entity state in one call, keyed by entity id.

        One request for the whole poll beats one per entity: the reply is a few
        hundred KB from a pod on the same LAN, and the app only keeps the handful
        of entities it knows about.
        """
        listed = await self._request("GET", "/api/states")
        return {row["entity_id"]: row for row in listed}

    async def state(self, entity_id: str) -> dict | None:
        try:
            return await self._request("GET", f"/api/states/{entity_id}")
        except HAError as exc:
            # A 404 means the entity is missing, which for the TV in deep standby
            # is a normal answer, not an outage.
            if "404" in str(exc):
                return None
            raise

    async def call(self, domain: str, service: str, /, **data: Any) -> None:
        await self._request("POST", f"/api/services/{domain}/{service}", json=data)
