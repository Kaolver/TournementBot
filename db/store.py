"""SQLite persistence. Thin async helpers over aiosqlite, no ORM."""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Sequence

import aiosqlite

from challonge.models import Match, Participant, Tournament
from db.backend import make_backend
from db.dialect import POSTGRES

SCHEMA_PATH = Path(__file__).with_name("schema.sql")
PG_TYPES = (("INTEGER", "BIGINT"),)
INDEX_MARKER = "-- @@INDEXES@@"

MIGRATIONS: dict[str, dict[str, str]] = {
    "guilds": {
        "deadline_hours": "INTEGER NOT NULL DEFAULT 24",
        "auto_sync": "INTEGER NOT NULL DEFAULT 1",
        "syncs_per_day": "INTEGER NOT NULL DEFAULT 12",
        "event_channel_id": "INTEGER",
        "event_location": "TEXT",
        "event_duration": "INTEGER NOT NULL DEFAULT 60",
        "first_match_day": "TEXT",
        "days_per_round": "INTEGER NOT NULL DEFAULT 0",
        "round_days": "TEXT",
    },
    "tournaments": {
        "bracket_message_id": "INTEGER",
        "next_refresh_at": "TEXT",
        "last_refresh_at": "TEXT",
        "refresh_window_start": "TEXT",
        "refresh_window_count": "INTEGER NOT NULL DEFAULT 0",
        "sync_day": "TEXT",
        "sync_day_count": "INTEGER NOT NULL DEFAULT 0",
    },
    "matches": {
        "agreed_at": "TEXT",
        "deadline_at": "TEXT",
        "scheduling_status": "TEXT NOT NULL DEFAULT 'pending'",
        "live": "INTEGER NOT NULL DEFAULT 0",
        "event_id": "INTEGER",
        "escalation_message_id": "INTEGER",
        "room_code": "TEXT",
        "play_by": "TEXT",
    },
}


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


class Store:
    """Owns the single aiosqlite connection for the process."""

    def __init__(
        self,
        path: str,
        supabase_url: str | None = None,
        supabase_key: str | None = None,
        schema: str = "tournamentbot",
    ) -> None:
        self._backend = make_backend(
            path=path,
            supabase_url=supabase_url,
            supabase_key=supabase_key,
            schema=schema,
        )

    @property
    def db(self):
        """The engine, imitating aiosqlite whichever one it actually is."""
        return self._backend

    @property
    def dialect(self) -> str:
        return self._backend.dialect

    async def connect(self) -> None:
        await self._backend.connect()
        schema = SCHEMA_PATH.read_text(encoding="utf-8")
        tables, _, indexes = schema.partition(INDEX_MARKER)
        # Both are no-ops on the hosted backend, which has its schema already.
        await self._backend.executescript(tables)
        await self._migrate()
        if indexes.strip():
            await self._backend.executescript(indexes)
        await self._backend.commit()

    async def _migrate(self) -> None:
        """Add any columns a database built by an older version is missing."""
        for table, columns in MIGRATIONS.items():
            existing = await self._backend.table_columns(table)
            for name, definition in columns.items():
                if name in existing:
                    continue
                if self.dialect == POSTGRES:
                    for sqlite_type, pg_type in PG_TYPES:
                        definition = definition.replace(sqlite_type, pg_type)
                await self._backend.execute(
                    f"ALTER TABLE {table} ADD COLUMN {name} {definition}"
                )
        await self._backend.commit()

    async def close(self) -> None:
        await self._backend.close()

    # ------------------------------------------------------------ api usage

    async def get_usage(self, month: str) -> int:
        async with self.db.execute(
            "SELECT count FROM api_usage WHERE month = ?", (month,)
        ) as cur:
            row = await cur.fetchone()
        return int(row["count"]) if row else 0

    async def bump_usage(self, month: str, amount: int = 1) -> int:
        await self.db.execute(
            """
            INSERT INTO api_usage (month, count) VALUES (?, ?)
            ON CONFLICT(month) DO UPDATE SET count = api_usage.count + excluded.count
            """,
            (month, amount),
        )
        await self.db.commit()
        return await self.get_usage(month)

    async def bump_usage_day(self, day: str, reason: str) -> None:
        await self.db.execute(
            """
            INSERT INTO api_usage_day (day, reason, count) VALUES (?, ?, 1)
            ON CONFLICT(day, reason) DO UPDATE SET count = api_usage_day.count + 1
            """,
            (day, reason),
        )
        await self.db.commit()

    async def usage_by_reason(self, day: str) -> dict[str, int]:
        """Today's spend, split by what caused it."""
        async with self.db.execute(
            "SELECT reason, count FROM api_usage_day WHERE day = ?", (day,)
        ) as cur:
            return {row["reason"]: int(row["count"]) for row in await cur.fetchall()}

    # --------------------------------------------------------------- guilds

    async def get_guild_config(self, guild_id: int) -> aiosqlite.Row | None:
        async with self.db.execute(
            "SELECT * FROM guilds WHERE guild_id = ?", (guild_id,)
        ) as cur:
            return await cur.fetchone()

    async def set_guild_config(
        self,
        guild_id: int,
        *,
        tournament_channel_id: int | None = None,
        to_role_id: int | None = None,
        tz: str | None = None,
        deadline_hours: int | None = None,
        auto_sync: bool | None = None,
        syncs_per_day: int | None = None,
        event_channel_id: int | None = None,
        event_location: str | None = None,
        event_duration: int | None = None,
        clear_event_channel: bool = False,
        first_match_day: str | None = None,
        days_per_round: int | None = None,
        round_days: str | None = None,
        clear_schedule: bool = False,
    ) -> None:
        await self.db.execute(
            "INSERT OR IGNORE INTO guilds (guild_id) VALUES (?)", (guild_id,)
        )
        updates: list[str] = []
        values: list[Any] = []
        if tournament_channel_id is not None:
            updates.append("tournament_channel_id = ?")
            values.append(tournament_channel_id)
        if to_role_id is not None:
            updates.append("to_role_id = ?")
            values.append(to_role_id)
        if tz is not None:
            updates.append("timezone = ?")
            values.append(tz)
        if deadline_hours is not None:
            updates.append("deadline_hours = ?")
            values.append(deadline_hours)
        if auto_sync is not None:
            updates.append("auto_sync = ?")
            values.append(1 if auto_sync else 0)
        if syncs_per_day is not None:
            updates.append("syncs_per_day = ?")
            values.append(syncs_per_day)
        if event_location is not None:
            updates.append("event_location = ?")
            values.append(event_location)
        if event_duration is not None:
            updates.append("event_duration = ?")
            values.append(event_duration)
        if event_channel_id is not None:
            updates.append("event_channel_id = ?")
            values.append(event_channel_id)
        elif clear_event_channel:
            updates.append("event_channel_id = NULL")
        if clear_schedule:
            updates.append("first_match_day = NULL")
            updates.append("days_per_round = 0")
            updates.append("round_days = NULL")
        else:
            if first_match_day is not None:
                updates.append("first_match_day = ?")
                values.append(first_match_day)
            if days_per_round is not None:
                updates.append("days_per_round = ?")
                values.append(days_per_round)
            if round_days is not None:
                updates.append("round_days = ?")
                values.append(round_days or None)
        if updates:
            values.append(guild_id)
            await self.db.execute(
                f"UPDATE guilds SET {', '.join(updates)} WHERE guild_id = ?", values
            )
        await self.db.commit()

    # ---------------------------------------------------------- tournaments

    async def save_tournament(
        self,
        tournament: Tournament,
        *,
        guild_id: int,
        channel_id: int | None = None,
    ) -> None:
        await self.db.execute(
            """
            INSERT INTO tournaments
                (challonge_id, guild_id, name, url, full_url, tournament_type,
                 state, channel_id)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(challonge_id) DO UPDATE SET
                name = excluded.name,
                url = excluded.url,
                full_url = excluded.full_url,
                tournament_type = excluded.tournament_type,
                state = excluded.state,
                channel_id = COALESCE(excluded.channel_id, tournaments.channel_id),
                -- Saving a tournament means we are running it again. Without
                -- this, re-adopting one the bot had archived leaves it hidden
                -- from get_active_tournament and every command reports "no
                -- active tournament" even though the adopt just succeeded.
                archived = 0
            """,
            (
                tournament.id,
                guild_id,
                tournament.name,
                tournament.url,
                tournament.full_challonge_url,
                tournament.tournament_type,
                tournament.state,
                channel_id,
            ),
        )
        await self.db.commit()

    async def set_tournament_state(self, tournament_id: int, state: str) -> None:
        await self.db.execute(
            "UPDATE tournaments SET state = ? WHERE challonge_id = ?",
            (state, tournament_id),
        )
        await self.db.commit()

    async def set_signup_message(
        self, tournament_id: int, channel_id: int, message_id: int
    ) -> None:
        await self.db.execute(
            "UPDATE tournaments SET channel_id = ?, signup_message_id = ? "
            "WHERE challonge_id = ?",
            (channel_id, message_id, tournament_id),
        )
        await self.db.commit()

    async def archive_others(self, guild_id: int, keep_id: int) -> int:
        """Only one tournament is live per guild; posting a panel picks it."""
        cur = await self.db.execute(
            "UPDATE tournaments SET archived = 1 WHERE guild_id = ? "
            "AND challonge_id != ? AND archived = 0",
            (guild_id, keep_id),
        )
        await self.db.commit()
        return cur.rowcount

    async def archive_tournament(self, tournament_id: int) -> None:
        await self.db.execute(
            "UPDATE tournaments SET archived = 1 WHERE challonge_id = ?",
            (tournament_id,),
        )
        await self.db.commit()

    async def get_tournament(self, tournament_id: int) -> aiosqlite.Row | None:
        async with self.db.execute(
            "SELECT * FROM tournaments WHERE challonge_id = ?", (tournament_id,)
        ) as cur:
            return await cur.fetchone()

    async def get_active_tournament(self, guild_id: int) -> aiosqlite.Row | None:
        """The one tournament a guild is currently running, if any."""
        async with self.db.execute(
            """
            SELECT * FROM tournaments
            WHERE guild_id = ? AND archived = 0
            ORDER BY created_at DESC LIMIT 1
            """,
            (guild_id,),
        ) as cur:
            return await cur.fetchone()

    async def tournament_for_thread(self, thread_id: int) -> aiosqlite.Row | None:
        async with self.db.execute(
            """
            SELECT t.*, m.match_id AS match_id
            FROM matches m JOIN tournaments t ON t.challonge_id = m.tournament_id
            WHERE m.thread_id = ?
            """,
            (thread_id,),
        ) as cur:
            return await cur.fetchone()

    # ------------------------------------------------------------- signups

    async def add_signup(
        self, tournament_id: int, discord_user_id: int, name: str
    ) -> bool:
        cur = await self.db.execute(
            """
            INSERT OR IGNORE INTO signups (tournament_id, discord_user_id, name)
            VALUES (?, ?, ?)
            """,
            (tournament_id, discord_user_id, name),
        )
        await self.db.commit()
        return cur.rowcount > 0

    async def remove_signup(self, tournament_id: int, discord_user_id: int) -> bool:
        cur = await self.db.execute(
            "DELETE FROM signups WHERE tournament_id = ? AND discord_user_id = ?",
            (tournament_id, discord_user_id),
        )
        await self.db.commit()
        return cur.rowcount > 0

    async def list_signups(self, tournament_id: int) -> list[aiosqlite.Row]:
        async with self.db.execute(
            "SELECT * FROM signups WHERE tournament_id = ? ORDER BY signed_up_at",
            (tournament_id,),
        ) as cur:
            return list(await cur.fetchall())

    async def set_signup_seed(
        self, tournament_id: int, discord_user_id: int, seed: int | None
    ) -> bool:
        cur = await self.db.execute(
            "UPDATE signups SET seed = ? WHERE tournament_id = ? "
            "AND discord_user_id = ?",
            (seed, tournament_id, discord_user_id),
        )
        await self.db.commit()
        return cur.rowcount > 0

    # --------------------------------------------------------- participants

    async def replace_participants(
        self, tournament_id: int, participants: Iterable[Participant]
    ) -> None:
        rows = [
            (
                tournament_id,
                p.id,
                p.discord_id,
                p.name,
                p.seed,
                p.final_rank,
            )
            for p in participants
        ]
        await self.db.execute(
            "DELETE FROM participants WHERE tournament_id = ?", (tournament_id,)
        )
        await self.db.executemany(
            """
            INSERT INTO participants
                (tournament_id, participant_id, discord_user_id, name, seed,
                 final_rank)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            rows,
        )
        await self.db.commit()

    async def list_participants(self, tournament_id: int) -> list[aiosqlite.Row]:
        async with self.db.execute(
            "SELECT * FROM participants WHERE tournament_id = ? "
            "ORDER BY COALESCE(final_rank, 9999), COALESCE(seed, 9999)",
            (tournament_id,),
        ) as cur:
            return list(await cur.fetchall())

    async def participant_for_discord(
        self, tournament_id: int, discord_user_id: int
    ) -> aiosqlite.Row | None:
        async with self.db.execute(
            "SELECT * FROM participants WHERE tournament_id = ? "
            "AND discord_user_id = ?",
            (tournament_id, discord_user_id),
        ) as cur:
            return await cur.fetchone()

    async def link_participant(
        self, tournament_id: int, participant_id: int, discord_user_id: int
    ) -> bool:
        """Attach a Discord account to a participant that came from the website.

        Kept local. Writing it back to Challonge's ``misc`` field would cost one
        API request per player, and the mapping is only needed here.
        """
        cur = await self.db.execute(
            "UPDATE participants SET discord_user_id = ? WHERE tournament_id = ? "
            "AND participant_id = ?",
            (discord_user_id, tournament_id, participant_id),
        )
        await self.db.commit()
        return cur.rowcount > 0

    async def unlink_participant(
        self, tournament_id: int, participant_id: int
    ) -> None:
        """Release a seat, so a player can move their claim to another name."""
        await self.db.execute(
            "UPDATE participants SET discord_user_id = NULL "
            "WHERE tournament_id = ? AND participant_id = ?",
            (tournament_id, participant_id),
        )
        await self.db.commit()

    async def unlinked_participants(self, tournament_id: int) -> list[aiosqlite.Row]:
        async with self.db.execute(
            "SELECT * FROM participants WHERE tournament_id = ? "
            "AND discord_user_id IS NULL ORDER BY name",
            (tournament_id,),
        ) as cur:
            return list(await cur.fetchall())

    async def participant_by_name(
        self, tournament_id: int, name: str
    ) -> aiosqlite.Row | None:
        async with self.db.execute(
            "SELECT * FROM participants WHERE tournament_id = ? "
            "AND name = ? COLLATE NOCASE",
            (tournament_id, name),
        ) as cur:
            return await cur.fetchone()

    async def link_signups_to_bracket(self, tournament_id: int) -> int:
        """Attach Discord sign-ups to bracket entries with matching names."""
        cur = await self.db.execute(
            """
            UPDATE participants
            SET discord_user_id = (
                SELECT s.discord_user_id FROM signups s
                WHERE s.tournament_id = participants.tournament_id
                  AND s.name = participants.name COLLATE NOCASE
            )
            WHERE tournament_id = ?
              AND discord_user_id IS NULL
              AND EXISTS (
                SELECT 1 FROM signups s
                WHERE s.tournament_id = participants.tournament_id
                  AND s.name = participants.name COLLATE NOCASE
              )
            """,
            (tournament_id,),
        )
        await self.db.commit()
        return cur.rowcount

    async def signup_for_discord(
        self, tournament_id: int, discord_user_id: int
    ) -> aiosqlite.Row | None:
        async with self.db.execute(
            "SELECT * FROM signups WHERE tournament_id = ? AND discord_user_id = ?",
            (tournament_id, discord_user_id),
        ) as cur:
            return await cur.fetchone()

    async def signup_name_taken(
        self, tournament_id: int, name: str, discord_user_id: int
    ) -> bool:
        async with self.db.execute(
            "SELECT 1 FROM signups WHERE tournament_id = ? AND name = ? "
            "COLLATE NOCASE AND discord_user_id != ?",
            (tournament_id, name, discord_user_id),
        ) as cur:
            return await cur.fetchone() is not None

    async def upsert_signup(
        self, tournament_id: int, discord_user_id: int, name: str
    ) -> None:
        await self.db.execute(
            """
            INSERT INTO signups (tournament_id, discord_user_id, name)
            VALUES (?, ?, ?)
            ON CONFLICT(tournament_id, discord_user_id)
            DO UPDATE SET name = excluded.name
            """,
            (tournament_id, discord_user_id, name),
        )
        await self.db.commit()

    async def participant_display(self, tournament_id: int) -> dict[int, str]:
        """Names for embeds: a mention when we know them, plain text otherwise.

        Players who never signed up on Discord are shown by their bracket name
        and never pinged.
        """
        return {
            int(row["participant_id"]): (
                f"<@{int(row['discord_user_id'])}>"
                if row["discord_user_id"]
                else row["name"]
            )
            for row in await self.list_participants(tournament_id)
        }

    async def participant_names(self, tournament_id: int) -> dict[int, str]:
        rows = await self.list_participants(tournament_id)
        return {int(r["participant_id"]): r["name"] for r in rows}

    async def discord_ids_for(
        self, tournament_id: int, participant_ids: Sequence[int]
    ) -> dict[int, int]:
        """Map Challonge participant id -> Discord user id, skipping unknowns."""
        out: dict[int, int] = {}
        for row in await self.list_participants(tournament_id):
            pid = int(row["participant_id"])
            if pid in participant_ids and row["discord_user_id"] is not None:
                out[pid] = int(row["discord_user_id"])
        return out

    # -------------------------------------------------------------- matches

    async def upsert_matches(
        self, tournament_id: int, matches: Iterable[Match]
    ) -> None:
        """Write the fresh Challonge view without clobbering bot-local columns.

        ``thread_id`` and ``scheduled_at`` exist only here, so they are
        preserved across refreshes.
        """
        await self.db.executemany(
            """
            INSERT INTO matches
                (tournament_id, match_id, identifier, round, play_order,
                 player1_id, player2_id, state, winner_id, scores)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(tournament_id, match_id) DO UPDATE SET
                identifier = excluded.identifier,
                round = excluded.round,
                play_order = excluded.play_order,
                player1_id = excluded.player1_id,
                player2_id = excluded.player2_id,
                state = excluded.state,
                winner_id = excluded.winner_id,
                scores = excluded.scores
            """,
            [
                (
                    tournament_id,
                    m.id,
                    m.identifier,
                    m.round,
                    m.suggested_play_order,
                    m.player1_id,
                    m.player2_id,
                    m.state,
                    m.winner_id,
                    m.scores,
                )
                for m in matches
            ],
        )
        await self.db.commit()

    async def list_matches(
        self, tournament_id: int, *, state: str | None = None
    ) -> list[aiosqlite.Row]:
        sql = "SELECT * FROM matches WHERE tournament_id = ?"
        args: list[Any] = [tournament_id]
        if state:
            sql += " AND state = ?"
            args.append(state)
        sql += " ORDER BY round, COALESCE(play_order, match_id)"
        async with self.db.execute(sql, args) as cur:
            return list(await cur.fetchall())

    async def get_match(
        self, tournament_id: int, match_id: int
    ) -> aiosqlite.Row | None:
        async with self.db.execute(
            "SELECT * FROM matches WHERE tournament_id = ? AND match_id = ?",
            (tournament_id, match_id),
        ) as cur:
            return await cur.fetchone()

    async def match_for_thread(self, thread_id: int) -> aiosqlite.Row | None:
        async with self.db.execute(
            "SELECT * FROM matches WHERE thread_id = ?", (thread_id,)
        ) as cur:
            return await cur.fetchone()

    async def record_reported_result(
        self, tournament_id: int, match_id: int, winner_id: int | None, scores: str
    ) -> None:
        """What Null Rush said happened. Deliberately not `state = complete`.

        The match stays open until Challonge says otherwise, because Challonge
        is still the bracket. This is evidence waiting for an admin.
        """
        await self.db.execute(
            "UPDATE matches SET winner_id = ?, scores = ? WHERE tournament_id = ? "
            "AND match_id = ?",
            (winner_id, scores, tournament_id, match_id),
        )
        await self.db.commit()

    async def set_room_code(
        self, tournament_id: int, match_id: int, code: str | None
    ) -> None:
        await self.db.execute(
            "UPDATE matches SET room_code = ? WHERE tournament_id = ? "
            "AND match_id = ?",
            (code, tournament_id, match_id),
        )
        await self.db.commit()

    async def match_for_room_code(self, code: str) -> Any | None:
        """Find the match a relay result belongs to, by its room code."""
        async with self.db.execute(
            "SELECT * FROM matches WHERE room_code = ? COLLATE NOCASE", (code,)
        ) as cur:
            return await cur.fetchone()

    async def set_match_thread(
        self, tournament_id: int, match_id: int, thread_id: int
    ) -> None:
        await self.db.execute(
            "UPDATE matches SET thread_id = ? WHERE tournament_id = ? "
            "AND match_id = ?",
            (thread_id, tournament_id, match_id),
        )
        await self.db.commit()

    async def set_match_schedule(
        self, tournament_id: int, match_id: int, when: datetime | None
    ) -> None:
        await self.db.execute(
            "UPDATE matches SET scheduled_at = ? WHERE tournament_id = ? "
            "AND match_id = ?",
            (_iso(when) if when else None, tournament_id, match_id),
        )
        await self.db.commit()

    async def matches_needing_threads(
        self, tournament_id: int, *, include_pending: bool = False
    ) -> list[aiosqlite.Row]:
        """Matches with both players known and no thread yet."""
        state_clause = (
            "state <> 'complete'" if include_pending else "state = 'open'"
        )
        async with self.db.execute(
            f"""
            SELECT * FROM matches
            WHERE tournament_id = ? AND {state_clause} AND thread_id IS NULL
              AND player1_id IS NOT NULL AND player2_id IS NOT NULL
            ORDER BY round, COALESCE(play_order, match_id)
            """,
            (tournament_id,),
        ) as cur:
            return list(await cur.fetchall())

    # ------------------------------------------------------------ reminders

    async def schedule_reminder(
        self, tournament_id: int, match_id: int, kind: str, fire_at: datetime
    ) -> None:
        await self.db.execute(
            """
            INSERT INTO reminders (tournament_id, match_id, kind, fire_at, sent)
            VALUES (?, ?, ?, ?, 0)
            ON CONFLICT(tournament_id, match_id, kind) DO UPDATE SET
                fire_at = excluded.fire_at, sent = 0
            """,
            (tournament_id, match_id, kind, _iso(fire_at)),
        )
        await self.db.commit()

    async def cancel_reminder(
        self, tournament_id: int, match_id: int, kind: str
    ) -> None:
        await self.db.execute(
            "DELETE FROM reminders WHERE tournament_id = ? AND match_id = ? "
            "AND kind = ?",
            (tournament_id, match_id, kind),
        )
        await self.db.commit()

    async def clear_reminders(self, tournament_id: int, match_id: int) -> None:
        await self.db.execute(
            "DELETE FROM reminders WHERE tournament_id = ? AND match_id = ?",
            (tournament_id, match_id),
        )
        await self.db.commit()

    async def due_reminders(self, now: datetime) -> list[aiosqlite.Row]:
        async with self.db.execute(
            "SELECT * FROM reminders WHERE sent = 0 AND fire_at <= ? "
            "ORDER BY fire_at",
            (_iso(now),),
        ) as cur:
            return list(await cur.fetchall())

    async def mark_reminder_sent(self, reminder_id: int) -> None:
        await self.db.execute(
            "UPDATE reminders SET sent = 1 WHERE id = ?", (reminder_id,)
        )
        await self.db.commit()


    # -------------------------------------------------------- refresh policy

    async def set_next_refresh(
        self, tournament_id: int, when: datetime | None
    ) -> None:
        """Ask the sync loop to read Challonge at (or after) ``when``."""
        await self.db.execute(
            "UPDATE tournaments SET next_refresh_at = ? WHERE challonge_id = ?",
            (_iso(when) if when else None, tournament_id),
        )
        await self.db.commit()

    async def request_refresh_no_later_than(
        self, tournament_id: int, when: datetime
    ) -> None:
        """Schedule a refresh for when if earlier than the current scheduled time."""
        row = await self.get_tournament(tournament_id)
        if row is None:
            return
        current = _parse_iso(row["next_refresh_at"])
        if current is None or when < current:
            await self.set_next_refresh(tournament_id, when)

    async def tournaments_due_for_refresh(self, now: datetime) -> list[aiosqlite.Row]:
        async with self.db.execute(
            """
            SELECT * FROM tournaments
            WHERE archived = 0
              AND next_refresh_at IS NOT NULL
              AND next_refresh_at <= ?
            ORDER BY next_refresh_at
            """,
            (_iso(now),),
        ) as cur:
            return list(await cur.fetchall())

    async def record_refresh(
        self,
        tournament_id: int,
        now: datetime,
        *,
        window_start: datetime,
        window_count: int,
        day: str,
        day_count: int,
    ) -> None:
        await self.db.execute(
            """
            UPDATE tournaments
            SET last_refresh_at = ?, next_refresh_at = NULL,
                refresh_window_start = ?, refresh_window_count = ?,
                sync_day = ?, sync_day_count = ?
            WHERE challonge_id = ?
            """,
            (
                _iso(now),
                _iso(window_start),
                window_count,
                day,
                day_count,
                tournament_id,
            ),
        )
        await self.db.commit()

    async def active_tournaments(self) -> list[aiosqlite.Row]:
        async with self.db.execute(
            "SELECT * FROM tournaments WHERE archived = 0"
        ) as cur:
            return list(await cur.fetchall())

    async def set_bracket_message(self, tournament_id: int, message_id: int) -> None:
        await self.db.execute(
            "UPDATE tournaments SET bracket_message_id = ? WHERE challonge_id = ?",
            (message_id, tournament_id),
        )
        await self.db.commit()

    # ----------------------------------------------------- match scheduling

    async def set_match_play_by(
        self, tournament_id: int, match_id: int, when: datetime | None
    ) -> None:
        await self.db.execute(
            "UPDATE matches SET play_by = ? WHERE tournament_id = ? "
            "AND match_id = ?",
            (_iso(when) if when else None, tournament_id, match_id),
        )
        await self.db.commit()

    async def set_match_deadline(
        self, tournament_id: int, match_id: int, when: datetime | None
    ) -> None:
        await self.db.execute(
            "UPDATE matches SET deadline_at = ? WHERE tournament_id = ? "
            "AND match_id = ?",
            (_iso(when) if when else None, tournament_id, match_id),
        )
        await self.db.commit()

    async def set_scheduling_status(
        self, tournament_id: int, match_id: int, status: str
    ) -> None:
        await self.db.execute(
            "UPDATE matches SET scheduling_status = ? WHERE tournament_id = ? "
            "AND match_id = ?",
            (status, tournament_id, match_id),
        )
        await self.db.commit()

    async def confirm_match_time(
        self, tournament_id: int, match_id: int, when: datetime
    ) -> None:
        """Lock in a time both players agreed to."""
        now = datetime.now(timezone.utc)
        await self.db.execute(
            """
            UPDATE matches
            SET scheduled_at = ?, agreed_at = ?, scheduling_status = 'agreed'
            WHERE tournament_id = ? AND match_id = ?
            """,
            (_iso(when), _iso(now), tournament_id, match_id),
        )
        await self.db.commit()

    async def set_match_live(
        self, tournament_id: int, match_id: int, live: bool
    ) -> None:
        await self.db.execute(
            "UPDATE matches SET live = ? WHERE tournament_id = ? AND match_id = ?",
            (1 if live else 0, tournament_id, match_id),
        )
        await self.db.commit()

    async def set_match_event(
        self, tournament_id: int, match_id: int, event_id: int | None
    ) -> None:
        await self.db.execute(
            "UPDATE matches SET event_id = ? WHERE tournament_id = ? "
            "AND match_id = ?",
            (event_id, tournament_id, match_id),
        )
        await self.db.commit()

    async def set_escalation_message(
        self, tournament_id: int, match_id: int, message_id: int
    ) -> None:
        await self.db.execute(
            "UPDATE matches SET escalation_message_id = ? WHERE tournament_id = ? "
            "AND match_id = ?",
            (message_id, tournament_id, match_id),
        )
        await self.db.commit()

    async def match_for_escalation_message(
        self, message_id: int
    ) -> aiosqlite.Row | None:
        async with self.db.execute(
            "SELECT * FROM matches WHERE escalation_message_id = ?", (message_id,)
        ) as cur:
            return await cur.fetchone()

    async def matches_with_events(self, tournament_id: int) -> list[aiosqlite.Row]:
        async with self.db.execute(
            "SELECT * FROM matches WHERE tournament_id = ? AND event_id IS NOT NULL",
            (tournament_id,),
        ) as cur:
            return list(await cur.fetchall())

    async def next_match_for(
        self, tournament_id: int, discord_user_id: int
    ) -> aiosqlite.Row | None:
        """The caller's open match, soonest scheduled first."""
        participant = await self.participant_for_discord(
            tournament_id, discord_user_id
        )
        if participant is None:
            return None
        pid = int(participant["participant_id"])
        matches = [
            row
            for row in await self.list_matches(tournament_id, state="open")
            if pid in (row["player1_id"], row["player2_id"])
        ]
        if not matches:
            return None
        matches.sort(key=lambda r: (r["scheduled_at"] is None, r["scheduled_at"] or ""))
        return matches[0]

    # -------------------------------------------------------------- proposals

    async def create_proposal(
        self,
        tournament_id: int,
        match_id: int,
        *,
        proposer_id: int,
        responder_id: int | None,
        proposed_at: datetime,
    ) -> int:
        """Record a time offer. Any earlier pending offer is superseded."""
        await self.supersede_proposals(tournament_id, match_id)
        cur = await self.db.execute(
            """
            INSERT INTO proposals
                (tournament_id, match_id, proposer_id, responder_id, proposed_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (tournament_id, match_id, proposer_id, responder_id, _iso(proposed_at)),
        )
        await self.db.commit()
        return int(cur.lastrowid)

    async def supersede_proposals(self, tournament_id: int, match_id: int) -> None:
        await self.db.execute(
            "UPDATE proposals SET status = 'superseded' WHERE tournament_id = ? "
            "AND match_id = ? AND status = 'pending'",
            (tournament_id, match_id),
        )
        await self.db.commit()

    async def pending_proposal(
        self, tournament_id: int, match_id: int
    ) -> aiosqlite.Row | None:
        async with self.db.execute(
            """
            SELECT * FROM proposals
            WHERE tournament_id = ? AND match_id = ? AND status = 'pending'
            ORDER BY id DESC LIMIT 1
            """,
            (tournament_id, match_id),
        ) as cur:
            return await cur.fetchone()

    async def proposal_for_message(self, message_id: int) -> aiosqlite.Row | None:
        """Resolve the proposal a button belongs to, so views can be persistent."""
        async with self.db.execute(
            "SELECT * FROM proposals WHERE message_id = ? ORDER BY id DESC LIMIT 1",
            (message_id,),
        ) as cur:
            return await cur.fetchone()

    async def get_proposal(self, proposal_id: int) -> aiosqlite.Row | None:
        async with self.db.execute(
            "SELECT * FROM proposals WHERE id = ?", (proposal_id,)
        ) as cur:
            return await cur.fetchone()

    async def set_proposal_status(self, proposal_id: int, status: str) -> None:
        await self.db.execute(
            "UPDATE proposals SET status = ? WHERE id = ?", (status, proposal_id)
        )
        await self.db.commit()

    async def set_proposal_message(self, proposal_id: int, message_id: int) -> None:
        await self.db.execute(
            "UPDATE proposals SET message_id = ? WHERE id = ?",
            (message_id, proposal_id),
        )
        await self.db.commit()

    async def list_proposals(
        self, tournament_id: int, match_id: int
    ) -> list[aiosqlite.Row]:
        async with self.db.execute(
            "SELECT * FROM proposals WHERE tournament_id = ? AND match_id = ? "
            "ORDER BY id",
            (tournament_id, match_id),
        ) as cur:
            return list(await cur.fetchall())


__all__ = ["Store", "_parse_iso"]
