"""Challonge v2.1 API client."""

from __future__ import annotations

import logging
from typing import Any, Sequence

import httpx

from .budget import ADMIN_REASONS, Budget
from .models import Match, Participant, Tournament

log = logging.getLogger(__name__)

BASE_URL = "https://api.challonge.com/v2.1"


class ChallongeError(RuntimeError):
    """A non-2xx response from Challonge, with the JSON:API errors unpacked."""

    def __init__(self, status: int, details: Sequence[str], url: str) -> None:
        self.status = status
        self.details = list(details)
        self.url = url
        joined = "; ".join(details) if details else "no detail provided"
        super().__init__(f"Challonge returned {status} for {url}: {joined}")


class RateLimited(ChallongeError):
    """HTTP 429: the monthly quota is spent or requests are coming too fast."""


class NotFound(ChallongeError):
    """HTTP 404: no such tournament, usually a typo in the URL."""


def _extract_errors(payload: Any) -> list[str]:
    if not isinstance(payload, dict):
        return []
    errors = payload.get("errors")
    if isinstance(errors, list):
        out = []
        for err in errors:
            if isinstance(err, dict):
                detail = err.get("detail") or err.get("title") or str(err)
                pointer = (err.get("source") or {}).get("pointer")
                out.append(f"{detail} ({pointer})" if pointer else str(detail))
            else:
                out.append(str(err))
        return out
    if isinstance(errors, str):
        return [errors]
    return []


def _resources(payload: Any) -> list[dict[str, Any]]:
    """Normalise a JSON:API response body to a list of resource objects."""
    if payload is None:
        return []
    if isinstance(payload, list):
        return [r for r in payload if isinstance(r, dict)]
    if not isinstance(payload, dict):
        return []
    data = payload.get("data", payload)
    if isinstance(data, dict):
        return [data]
    if isinstance(data, list):
        return [r for r in data if isinstance(r, dict)]
    return []


def _single(payload: Any) -> dict[str, Any]:
    resources = _resources(payload)
    return resources[0] if resources else {}


def slug_from(reference: str) -> str:
    """Pull the tournament id or slug out of whatever the organiser pasted.

    Accepts a full URL, a subdomain URL, or a bare id. Only the last path
    segment is kept, so a stray query string or trailing slash is harmless.
    """
    cleaned = reference.strip().split("?")[0].split("#")[0].rstrip("/")
    return cleaned.split("/")[-1] or cleaned


class ChallongeClient:
    """Async, read-only Challonge client. One instance per bot process."""

    def __init__(
        self,
        api_key: str,
        budget: Budget,
        *,
        timeout: float = 20.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._budget = budget
        self._client = httpx.AsyncClient(
            base_url=BASE_URL,
            timeout=timeout,
            transport=transport,
            headers={
                "Content-Type": "application/vnd.api+json",
                "Accept": "application/json",
                "Authorization-Type": "v1",
                "Authorization": api_key,
                "User-Agent": "TournamentBot (+discord.py)",
            },
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> "ChallongeClient":
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.aclose()

    async def _get(self, path: str, *, reason: str) -> Any:
        """Issue an authenticated GET request subject to budget reservation."""
        await self._budget.reserve(reason)
        try:
            response = await self._client.get(path)
        finally:
            # A request that errored still hit their servers and still counted.
            await self._budget.record(reason)

        log.debug("challonge GET %s -> %s", path, response.status_code)

        try:
            payload = response.json() if response.content else None
        except ValueError:
            payload = None

        if response.status_code >= 400:
            details = _extract_errors(payload) or [response.text[:300]]
            error_cls = {404: NotFound, 429: RateLimited}.get(
                response.status_code, ChallongeError
            )
            raise error_cls(response.status_code, details, str(response.url))

        return payload

    async def get_tournament(
        self, tournament: int | str, *, reason: str
    ) -> Tournament:
        return Tournament.parse(
            _single(await self._get(f"/tournaments/{tournament}.json", reason=reason))
        )

    async def list_participants(
        self, tournament: int | str, *, reason: str
    ) -> list[Participant]:
        payload = await self._get(
            f"/tournaments/{tournament}/participants.json", reason=reason
        )
        return [Participant.parse(r) for r in _resources(payload)]

    async def list_matches(self, tournament: int | str, *, reason: str) -> list[Match]:
        payload = await self._get(
            f"/tournaments/{tournament}/matches.json", reason=reason
        )
        return [Match.parse(r) for r in _resources(payload)]

    # ------------------------------------------------------------- the write

    async def report_match(
        self,
        tournament: int | str,
        match_id: int,
        *,
        winner_id: int,
        scores_csv: str,
        reason: str = "admin:report",
    ) -> Match:
        """Report match outcome to Challonge API."""
        if reason not in ADMIN_REASONS:
            raise ValueError(
                f"{reason!r} cannot write. A write has to be attributable to an "
                "admin action."
            )
        await self._budget.reserve(reason)
        body = {
            "data": {
                "type": "match",
                "attributes": {"scores_csv": scores_csv, "winner_id": winner_id},
            }
        }
        try:
            response = await self._client.put(
                f"/tournaments/{tournament}/matches/{match_id}.json", json=body
            )
        finally:
            await self._budget.record(reason)

        try:
            payload = response.json() if response.content else None
        except ValueError:
            payload = None

        if response.status_code >= 400:
            details = _extract_errors(payload) or [response.text[:300]]
            error_cls = {404: NotFound, 429: RateLimited}.get(
                response.status_code, ChallongeError
            )
            raise error_cls(response.status_code, details, str(response.url))

        return Match.parse(_single(payload))
