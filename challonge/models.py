"""Dataclasses parsed from Challonge JSON:API payloads."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


def _attrs(resource: dict[str, Any]) -> dict[str, Any]:
    """Merge a JSON:API resource's top level and its ``attributes`` block."""
    merged: dict[str, Any] = {
        k: v for k, v in resource.items() if k not in ("attributes", "relationships")
    }
    merged.update(resource.get("attributes") or {})
    return merged


def _relationships(resource: dict[str, Any]) -> dict[str, Any]:
    """Relationships may sit beside or inside ``attributes``."""
    rels = resource.get("relationships")
    if not rels:
        rels = (resource.get("attributes") or {}).get("relationships")
    return rels or {}


def _rel_id(rels: dict[str, Any], name: str) -> int | None:
    """Pull the resource id out of a relationship entry."""
    node = rels.get(name)
    if not isinstance(node, dict):
        return None
    data = node.get("data", node)
    if isinstance(data, list):
        data = data[0] if data else None
    if not isinstance(data, dict):
        return None
    raw = data.get("id")
    return int(raw) if raw is not None else None


def _rel_name(rels: dict[str, Any], name: str) -> str | None:
    node = rels.get(name)
    if not isinstance(node, dict):
        return None
    data = node.get("data", node)
    if isinstance(data, list):
        data = data[0] if data else None
    if not isinstance(data, dict):
        return None
    inner = {**data, **(data.get("attributes") or {})}
    return inner.get("name") or inner.get("display_name")


def _as_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


@dataclass(slots=True)
class Tournament:
    id: int
    name: str
    url: str
    state: str
    tournament_type: str
    full_challonge_url: str
    raw: dict[str, Any] = field(repr=False, default_factory=dict)

    @classmethod
    def parse(cls, resource: dict[str, Any]) -> "Tournament":
        a = _attrs(resource)
        url = a.get("url") or ""
        return cls(
            id=_as_int(a.get("id")) or 0,
            name=a.get("name") or "",
            url=url,
            state=a.get("state") or "pending",
            tournament_type=a.get("tournament_type") or "",
            full_challonge_url=a.get("full_challonge_url")
            or (f"https://challonge.com/{url}" if url else ""),
            raw=resource,
        )


@dataclass(slots=True)
class Participant:
    id: int
    name: str
    seed: int | None
    misc: str | None
    final_rank: int | None
    active: bool
    raw: dict[str, Any] = field(repr=False, default_factory=dict)

    @property
    def discord_id(self) -> int | None:
        """We stash the Discord user id in ``misc`` when registering players."""
        return _as_int(self.misc)

    @classmethod
    def parse(cls, resource: dict[str, Any]) -> "Participant":
        a = _attrs(resource)
        return cls(
            id=_as_int(a.get("id")) or 0,
            name=a.get("name") or "",
            seed=_as_int(a.get("seed")),
            misc=a.get("misc"),
            final_rank=_as_int(a.get("final_rank")),
            active=bool(a.get("active", True)),
            raw=resource,
        )


@dataclass(slots=True)
class Match:
    id: int
    state: str
    round: int
    identifier: str
    suggested_play_order: int | None
    scores: str | None
    winner_id: int | None
    player1_id: int | None
    player2_id: int | None
    player1_name: str | None
    player2_name: str | None
    raw: dict[str, Any] = field(repr=False, default_factory=dict)

    @property
    def is_open(self) -> bool:
        return self.state == "open"

    @property
    def is_complete(self) -> bool:
        return self.state == "complete"

    @property
    def has_both_players(self) -> bool:
        return self.player1_id is not None and self.player2_id is not None

    @classmethod
    def parse(cls, resource: dict[str, Any]) -> "Match":
        a = _attrs(resource)
        rels = _relationships(resource)
        return cls(
            id=_as_int(a.get("id")) or 0,
            state=a.get("state") or "pending",
            round=_as_int(a.get("round")) or 0,
            identifier=a.get("identifier") or "",
            suggested_play_order=_as_int(a.get("suggested_play_order")),
            scores=a.get("scores") or a.get("scores_csv"),
            winner_id=_as_int(a.get("winner_id")),
            player1_id=_as_int(a.get("player1_id")) or _rel_id(rels, "player1"),
            player2_id=_as_int(a.get("player2_id")) or _rel_id(rels, "player2"),
            player1_name=_rel_name(rels, "player1"),
            player2_name=_rel_name(rels, "player2"),
            raw=resource,
        )
