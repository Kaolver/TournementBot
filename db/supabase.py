"""Storage backend over Supabase REST API."""

from __future__ import annotations

import base64
import json
import logging
import re
from typing import Any, Iterable, Sequence

import httpx

from .backend import Cursor, _Result
from .dialect import POSTGRES, translate

log = logging.getLogger(__name__)


def normalise_url(raw: str) -> str:
    """Normalise Supabase URL, stripping trailing slashes and /rest/v1."""
    url = (raw or "").strip()
    if not url:
        return ""
    if not re.match(r"^https?://", url):
        url = "https://" + url
    url = url.rstrip("/")
    return re.sub(r"/rest/v1$", "", url)


def _is_jwt(key: str) -> bool:
    """A legacy anon/service_role key, as opposed to a new sb_secret_ one."""
    parts = (key or "").split(".")
    if len(parts) != 3:
        return False
    try:
        padded = parts[1].replace("-", "+").replace("_", "/")
        padded += "=" * (-len(padded) % 4)
        json.loads(base64.b64decode(padded))
        return True
    except Exception:  # noqa: BLE001 - not a JWT is the answer, not an error
        return False


class SupabaseError(RuntimeError):
    pass


class SupabaseBackend:
    """Imitates aiosqlite's surface, over HTTPS, through one RPC."""

    dialect = POSTGRES

    def __init__(
        self,
        url: str,
        key: str,
        schema: str = "tournamentbot",
        *,
        timeout: float = 30.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._url = normalise_url(url)
        self._key = (key or "").strip()
        self._schema = re.sub(r"[^A-Za-z0-9_]", "", schema) or "public"

        headers = {
            "apikey": self._key,
            "content-type": "application/json",
            "Content-Profile": self._schema,
            "Accept-Profile": self._schema,
        }
        if _is_jwt(self._key):
            headers["authorization"] = f"Bearer {self._key}"
        self._client = httpx.AsyncClient(
            base_url=self._url or "https://unconfigured",
            headers=headers,
            timeout=timeout,
            transport=transport,
        )

    @property
    def schema(self) -> str:
        return self._schema

    async def connect(self) -> None:
        if not self._url or not self._key:
            raise SupabaseError(
                "SUPABASE_URL and SUPABASE_SECRET_KEY are both required to use "
                "Supabase storage. Leave them blank to use SQLite instead."
            )
        # One round trip that fails loudly and early if the setup SQL has not
        # been run, rather than at the first command an admin types.
        try:
            await self._rpc("SELECT 1 AS ok", [])
        except SupabaseError as exc:
            raise SupabaseError(
                f"{exc}\n\nIf this says the function does not exist, run "
                "db/supabase_setup.sql in the Supabase SQL Editor first."
            ) from None
        log.info("connected to Supabase, schema %s", self._schema)

    async def close(self) -> None:
        await self._client.aclose()

    # ------------------------------------------------------------------ core

    async def _rpc(self, statement: str, params: Sequence[Any]) -> Any:
        payload = {
            "q": statement,
            "args": [None if p is None else str(p) for p in params],
        }
        try:
            response = await self._client.post("/rest/v1/rpc/bot_sql", json=payload)
        except httpx.HTTPError as exc:
            raise SupabaseError(f"could not reach Supabase: {exc}") from exc

        if response.status_code >= 400:
            raise SupabaseError(
                f"Supabase {response.status_code}: {response.text[:300]}"
            )
        return response.json()

    # Tables with a generated id; append RETURNING id for lastrowid support.
    _ID_TABLES = frozenset({"proposals", "reminders"})
    _INSERT_INTO = re.compile(r"^\s*INSERT\s+INTO\s+([A-Za-z_][A-Za-z0-9_]*)", re.I)

    def _with_returning(self, statement: str) -> str:
        if "RETURNING" in statement.upper():
            return statement
        match = self._INSERT_INTO.match(statement)
        if match and match.group(1).lower() in self._ID_TABLES:
            return statement.rstrip().rstrip(";") + " RETURNING id"
        return statement

    def execute(self, sql: str, params: Sequence[Any] = ()):
        async def run() -> Cursor:
            statement = self._with_returning(translate(sql, POSTGRES))
            result = await self._rpc(statement, params)

            if isinstance(result, list):
                return Cursor(result, rowcount=len(result))
            if isinstance(result, dict):
                rows = result.get("rows")
                if isinstance(rows, list):
                    first = rows[0] if rows else None
                    return Cursor(
                        rows,
                        rowcount=int(result.get("rowcount", len(rows))),
                        lastrowid=first.get("id") if isinstance(first, dict) else None,
                    )
                return Cursor(rowcount=int(result.get("rowcount", 0)))
            return Cursor()

        return _Result(run)

    async def executemany(self, sql: str, rows: Iterable[Sequence[Any]]) -> None:
        for row in rows:
            await self.execute(sql, row)

    async def executescript(self, sql: str) -> None:
        """No-op; schema is managed by db/supabase_setup.sql."""
        log.debug("schema is managed by db/supabase_setup.sql; nothing to run")

    async def commit(self) -> None:
        return None  # every RPC call is its own transaction

    async def table_columns(self, table: str) -> set[str]:
        rows = await self._rpc(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_name = $1 AND table_schema = $2",
            [table, self._schema],
        )
        if isinstance(rows, dict):
            rows = rows.get("rows") or []
        return {row["column_name"] for row in rows}
