"""Database backend abstraction for SQLite and Supabase."""

from __future__ import annotations

import logging
from typing import Any, Iterable, Sequence

import aiosqlite

from .dialect import SQLITE

log = logging.getLogger(__name__)


class Cursor:
    """What a call site gets back, on either engine."""

    def __init__(
        self,
        rows: Sequence[Any] = (),
        rowcount: int = 0,
        lastrowid: int | None = None,
    ) -> None:
        self._rows = list(rows)
        self.rowcount = rowcount
        self.lastrowid = lastrowid

    async def fetchone(self) -> Any | None:
        return self._rows[0] if self._rows else None

    async def fetchall(self) -> list[Any]:
        return list(self._rows)


class _Result:
    """Awaitable *and* an async context manager, exactly as aiosqlite is."""

    def __init__(self, run) -> None:
        self._run = run
        self._cursor: Cursor | None = None

    def __await__(self):
        return self._resolve().__await__()

    async def _resolve(self) -> Cursor:
        if self._cursor is None:
            self._cursor = await self._run()
        return self._cursor

    async def __aenter__(self) -> Cursor:
        return await self._resolve()

    async def __aexit__(self, *exc: object) -> None:
        return None


class SqliteBackend:
    """The development engine. A thin pass-through, so behaviour is unchanged."""

    dialect = SQLITE

    def __init__(self, path: str) -> None:
        self._path = path
        self._db: aiosqlite.Connection | None = None

    @property
    def raw(self) -> aiosqlite.Connection:
        if self._db is None:
            raise RuntimeError("connect() has not been awaited")
        return self._db

    async def connect(self) -> None:
        self._db = await aiosqlite.connect(self._path)
        self._db.row_factory = aiosqlite.Row
        await self._db.execute("PRAGMA foreign_keys = ON")

    async def close(self) -> None:
        if self._db is not None:
            await self._db.close()
            self._db = None

    def execute(self, sql: str, params: Sequence[Any] = ()):
        return self.raw.execute(sql, params)

    async def executemany(self, sql: str, rows: Iterable[Sequence[Any]]) -> None:
        await self.raw.executemany(sql, list(rows))

    async def executescript(self, sql: str) -> None:
        await self.raw.executescript(sql)

    async def commit(self) -> None:
        await self.raw.commit()

    async def table_columns(self, table: str) -> set[str]:
        async with self.raw.execute(f"PRAGMA table_info({table})") as cur:
            return {row["name"] for row in await cur.fetchall()}


def make_backend(
    *,
    path: str,
    supabase_url: str | None = None,
    supabase_key: str | None = None,
    schema: str = "tournamentbot",
):
    """Return Supabase backend if configured, otherwise SqliteBackend."""
    if supabase_url and supabase_key:
        from .supabase import SupabaseBackend

        return SupabaseBackend(supabase_url, supabase_key, schema)
    return SqliteBackend(path)
