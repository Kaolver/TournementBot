"""SQL dialect translation between SQLite and PostgreSQL."""

from __future__ import annotations

import re

SQLITE = "sqlite"
POSTGRES = "postgres"

# `col = ? COLLATE NOCASE` and `a.col = b.col COLLATE NOCASE`. SQLite attaches
# the collation to the comparison; Postgres wants the case folded on both sides.
_COLLATE_PARAM = re.compile(r"(\b[\w.]+)\s*=\s*\?\s+COLLATE\s+NOCASE", re.IGNORECASE)
_COLLATE_COLS = re.compile(
    r"(\b[\w.]+)\s*=\s*([\w.]+)\s+COLLATE\s+NOCASE", re.IGNORECASE
)


def to_postgres(sql: str) -> str:
    """Rewrite a SQLite-dialect statement for Postgres."""
    out = sql

    # Case-insensitive comparison, before placeholders are numbered so the
    # `?` in these patterns is still recognisable.
    out = _COLLATE_PARAM.sub(r"lower(\1) = lower(?)", out)
    out = _COLLATE_COLS.sub(r"lower(\1) = lower(\2)", out)

    # INSERT OR IGNORE has no Postgres spelling; it is an upsert that does
    # nothing. Only append the clause when the statement has not already got
    # its own ON CONFLICT.
    if re.search(r"INSERT\s+OR\s+IGNORE", out, re.IGNORECASE):
        out = re.sub(r"INSERT\s+OR\s+IGNORE\s+INTO", "INSERT INTO", out, flags=re.I)
        if "ON CONFLICT" not in out.upper():
            out = out.rstrip().rstrip(";") + " ON CONFLICT DO NOTHING"

    out = re.sub(r"datetime\(\s*'now'\s*\)", "(now() at time zone 'utc')", out, flags=re.I)

    # Placeholders last: everything above may have introduced or moved one.
    index = 0

    def number(_match: re.Match) -> str:
        nonlocal index
        index += 1
        return f"${index}"

    return re.sub(r"\?", number, out)


def translate(sql: str, dialect: str) -> str:
    return to_postgres(sql) if dialect == POSTGRES else sql


def rowcount_from_status(status: str) -> int:
    """asyncpg reports ``"UPDATE 3"``; aiosqlite has ``cursor.rowcount``."""
    try:
        return int(str(status).rsplit(" ", 1)[1])
    except (IndexError, ValueError):
        return 0
