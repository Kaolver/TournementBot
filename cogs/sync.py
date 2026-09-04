"""Automatic Challonge bracket sync loop."""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

import aiosqlite
import discord
from discord.ext import commands, tasks

from challonge.budget import BudgetExhausted
from challonge.client import ChallongeError, RateLimited
from db.store import _parse_iso
from services import (
    THREAD_ACTIVITY_DELAY,
    evaluate_refresh,
    plan_next_refresh,
    sync_allowance,
)

log = logging.getLogger(__name__)

BACKOFF_RATE_LIMIT = timedelta(hours=1)
BACKOFF_ERROR = timedelta(minutes=15)


class SyncCog(commands.Cog):
    def __init__(self, bot) -> None:
        self.bot = bot
        self.sync_loop.start()

    async def cog_unload(self) -> None:
        self.sync_loop.cancel()

    # ------------------------------------------------------------- planning

    async def reschedule(self, tournament: aiosqlite.Row) -> None:
        """Work out when this tournament should next be read, and record it."""
        tournament_id = int(tournament["challonge_id"])
        open_matches = await self.bot.store.list_matches(tournament_id, state="open")
        when, reason = plan_next_refresh(
            datetime.now(timezone.utc), open_matches=open_matches
        )
        if when is None:
            await self.bot.store.set_next_refresh(tournament_id, None)
            log.debug("no refresh planned for %s: %s", tournament["name"], reason)
        else:
            await self.bot.store.request_refresh_no_later_than(tournament_id, when)
            log.debug("next refresh for %s: %s (%s)", tournament["name"], when, reason)

    @commands.Cog.listener()
    async def on_tournament_completed(self, tournament: aiosqlite.Row) -> None:
        """Every match is done. Post the final standings and stop polling."""
        from ui.embeds import standings_embed

        tournament_id = int(tournament["challonge_id"])
        await self.bot.store.set_next_refresh(tournament_id, None)

        channel_id = tournament["channel_id"]
        channel = self.bot.get_channel(int(channel_id)) if channel_id else None
        if channel is None:
            return
        try:
            await channel.send(
                content=f"**{tournament['name']}** is finished.",
                embed=standings_embed(
                    tournament,
                    await self.bot.store.list_participants(tournament_id),
                ),
            )
        except discord.HTTPException:
            log.warning("could not post final standings")

    @commands.Cog.listener()
    async def on_matches_opened(
        self, tournament: aiosqlite.Row, matches: list[aiosqlite.Row]
    ) -> None:
        # New matches mean new things to watch for.
        await self.reschedule(tournament)

    @commands.Cog.listener()
    async def on_relay_result(self, code: str, payload: dict) -> None:
        """Record match result reported by Null Rush and post confirmation view."""
        from ui.embeds import reported_embed
        from ui.views import ResultView

        match = await self.bot.store.match_for_room_code(code)
        if match is None:
            log.warning("relay reported match code %s, which is not ours", code)
            return

        tournament_id = int(match["tournament_id"])
        tournament = await self.bot.store.get_tournament(tournament_id)
        if tournament is None:
            return

        # Seat 1 is player1, seat 2 is player2.
        seat = int(payload.get("winnerSeat") or 0)
        winner_id = match["player1_id"] if seat == 1 else match["player2_id"]
        scores = str(payload.get("scores") or "")
        await self.bot.store.record_reported_result(
            tournament_id, int(match["match_id"]), winner_id, scores
        )

        thread = self.bot.get_channel(int(match["thread_id"])) if match["thread_id"] else None
        if not isinstance(thread, discord.Thread):
            return

        refreshed = await self.bot.store.get_match(
            tournament_id, int(match["match_id"])
        )
        await thread.send(
            embed=reported_embed(
                refreshed or match,
                await self.bot.store.participant_display(tournament_id),
                winner_id=winner_id,
                scores=scores,
            ),
            view=ResultView(self.bot),
        )

    # ----------------------------------------------------------- the trigger

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message) -> None:
        """Schedule a refresh when thread activity is detected around match time."""
        if message.author.bot or not isinstance(message.channel, discord.Thread):
            return
        match = await self.bot.store.match_for_thread(message.channel.id)
        if match is None or match["state"] != "open":
            return

        scheduled = _parse_iso(match["scheduled_at"])
        now = datetime.now(timezone.utc)
        if scheduled is None or scheduled > now:
            return  # Chat before the match tells us nothing about the result.

        await self.bot.store.request_refresh_no_later_than(
            int(match["tournament_id"]), now + THREAD_ACTIVITY_DELAY
        )

    # ---------------------------------------------------------------- loop

    @tasks.loop(seconds=60.0)
    async def sync_loop(self) -> None:
        now = datetime.now(timezone.utc)
        try:
            due = await self.bot.store.tournaments_due_for_refresh(now)
        except Exception:  # noqa: BLE001 - the loop must not die
            log.exception("could not read due tournaments")
            return

        for tournament in due:
            try:
                await self._sync_one(tournament, now)
            except Exception:  # noqa: BLE001
                log.exception("sync failed for %s", tournament["name"])
                await self.bot.store.set_next_refresh(
                    int(tournament["challonge_id"]), now + BACKOFF_ERROR
                )

    @sync_loop.before_loop
    async def before_sync_loop(self) -> None:
        await self.bot.wait_until_ready()

    async def _sync_one(self, tournament: aiosqlite.Row, now: datetime) -> None:
        tournament_id = int(tournament["challonge_id"])

        decision = evaluate_refresh(
            now,
            window_start=_parse_iso(tournament["refresh_window_start"]),
            window_count=int(tournament["refresh_window_count"] or 0),
            day=tournament["sync_day"],
            day_count=int(tournament["sync_day_count"] or 0),
            max_per_day=await sync_allowance(
                self.bot, int(tournament["guild_id"])
            ),
        )
        if not decision.allowed:
            log.info("deferring sync for %s: %s", tournament["name"], decision.reason)
            await self.bot.store.set_next_refresh(tournament_id, decision.retry_at)
            return

        try:
            opened, completed = await self.bot.refresh_matches(
                tournament, reason="autosync"
            )
        except (BudgetExhausted, RateLimited) as exc:
            # Out of quota. Stop trying for a while and tell the organisers.
            log.warning("sync paused for %s: %s", tournament["name"], exc)
            await self.bot.store.record_refresh(
                tournament_id,
                now,
                window_start=decision.window_start,
                window_count=decision.window_count,
                day=decision.day,
                day_count=decision.day_count,
            )
            await self.bot.store.set_next_refresh(
                tournament_id, now + BACKOFF_RATE_LIMIT
            )
            await self._warn_channel(tournament, str(exc))
            return
        except ChallongeError:
            log.exception("Challonge rejected a sync for %s", tournament["name"])
            await self.bot.store.record_refresh(
                tournament_id,
                now,
                window_start=decision.window_start,
                window_count=decision.window_count,
                day=decision.day,
                day_count=decision.day_count,
            )
            await self.bot.store.set_next_refresh(tournament_id, now + BACKOFF_ERROR)
            return

        await self.bot.store.record_refresh(
            tournament_id,
            now,
            window_start=decision.window_start,
            window_count=decision.window_count,
            day=decision.day,
            day_count=decision.day_count,
        )
        if opened or completed:
            log.info(
                "%s: %d opened, %d completed",
                tournament["name"],
                len(opened),
                len(completed),
            )
        fresh = await self.bot.store.get_tournament(tournament_id)
        await self.reschedule(fresh)

        # Update tournament panel.
        from ui.views import refresh_panel

        guild = self.bot.get_guild(int(tournament["guild_id"]))
        if guild is not None and fresh is not None:
            await refresh_panel(self.bot, guild, fresh)

    async def _warn_channel(self, tournament: aiosqlite.Row, detail: str) -> None:
        channel_id = tournament["channel_id"]
        channel = self.bot.get_channel(int(channel_id)) if channel_id else None
        if channel is None:
            return
        try:
            await channel.send(
                "**Automatic bracket syncing is paused.**\n"
                f"{detail}\n\nThreads for new matches will not open until this "
                "clears. `/tournament sync` still works if quota frees up."
            )
        except discord.HTTPException:
            pass

    @sync_loop.before_loop
    async def before_sync_loop(self) -> None:
        await self.bot.wait_until_ready()


async def setup(bot) -> None:
    await bot.add_cog(SyncCog(bot))
