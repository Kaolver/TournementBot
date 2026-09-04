"""Null Rush game relay client."""

from __future__ import annotations

import logging
from dataclasses import dataclass

import httpx

log = logging.getLogger(__name__)


class RelayError(RuntimeError):
    """The relay refused or could not be reached. Always shown to the user."""


@dataclass(frozen=True)
class RelayMatch:
    code: str
    match_id: str
    best_of: int
    state: str = "open"

    @property
    def settled(self) -> bool:
        return self.state == "reported"


class RelayClient:
    def __init__(
        self,
        base_url: str | None,
        token: str | None,
        *,
        timeout: float = 30.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._configured = bool(base_url and token)
        self._token = token or ""
        # Generous, because a free-tier relay can be asleep and the first
        # request of the day pays a cold start. Failing at 5s would tell an
        # admin the relay is broken when it is merely waking.
        self._client = (
            httpx.AsyncClient(
                base_url=base_url or "http://unconfigured",
                timeout=timeout,
                transport=transport,
            )
            if base_url
            else None
        )

    @property
    def configured(self) -> bool:
        return self._configured

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()

    def _require(self) -> httpx.AsyncClient:
        if not self._configured or self._client is None:
            raise RelayError(
                "No relay is configured, so in-game matches are off. Set "
                "RELAY_URL and RELAY_TOKEN to turn them on."
            )
        return self._client

    async def create_match(
        self,
        *,
        match_id: str,
        tournament: str,
        players: list[str],
        best_of: int = 1,
    ) -> RelayMatch:
        """Reserve a room for one bracket match and get its code."""
        client = self._require()
        try:
            response = await client.post(
                "/api/matches",
                headers={"Authorization": f"Bearer {self._token}"},
                json={
                    "matchId": match_id,
                    "tournament": tournament,
                    "players": players,
                    "bestOf": best_of,
                },
            )
        except httpx.HTTPError as exc:
            raise RelayError(f"could not reach the relay: {exc}") from exc

        if response.status_code == 401:
            raise RelayError("the relay rejected our token; check RELAY_TOKEN")
        if response.status_code >= 400:
            raise RelayError(
                f"the relay refused ({response.status_code}): {response.text[:200]}"
            )

        body = response.json()
        return RelayMatch(
            code=body["code"],
            match_id=str(body.get("matchId", match_id)),
            best_of=int(body.get("bestOf", best_of)),
        )

    async def get_match(self, code: str) -> RelayMatch | None:
        """Read a match back. The fallback for when a webhook goes missing."""
        client = self._require()
        try:
            response = await client.get(
                f"/api/matches/{code}",
                headers={"Authorization": f"Bearer {self._token}"},
            )
        except httpx.HTTPError as exc:
            raise RelayError(f"could not reach the relay: {exc}") from exc

        if response.status_code == 404:
            return None
        if response.status_code >= 400:
            raise RelayError(
                f"the relay refused ({response.status_code}): {response.text[:200]}"
            )

        body = response.json()
        return RelayMatch(
            code=body["code"],
            match_id=str(body.get("match_id", "")),
            best_of=int(body.get("best_of", 1)),
            state=str(body.get("state", "open")),
        )
