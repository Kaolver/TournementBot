"""Match scheduling reminders and deadline tracking."""

from __future__ import annotations

import logging
from datetime import datetime, timezone

import aiosqlite
import discord
from discord.ext import commands, tasks

from cogs.common import discord_ts
from db.store import _parse_iso
from services import DEADLINE_KINDS

log = logging.getLogger(__name__)

REMINDER_TEXT = {
    "1h": "in about an hour",
    "5m": "in 5 minutes",
}


class SchedulingCog(commands.Cog):
    def __init__(self, bot) -> None:
        self.bot = bot
        self.reminder_loop.start()

    async def cog_unload(self) -> None:
        self.reminder_loop.cancel()

    @tasks.loop(seconds=60.0)
    async def reminder_loop(self) -> None:
        now = datetime.now(timezone.utc)
        try:
            due = await self.bot.store.due_reminders(now)
        except Exception:  # noqa: BLE001 - the loop must not die
            log.exception("could not read due reminders")
            return

        for reminder in due:
            try:
                await self._fire(reminder)
            except Exception:  # noqa: BLE001
                log.exception("reminder %s failed", reminder["id"])
            finally:
                # Mark sent so failing reminders are not retried indefinitely.
                await self.bot.store.mark_reminder_sent(int(reminder["id"]))

    @reminder_loop.before_loop
    async def before_reminder_loop(self) -> None:
        await self.bot.wait_until_ready()

    async def _fire(self, reminder: aiosqlite.Row) -> None:
        tournament_id = int(reminder["tournament_id"])
        match = await self.bot.store.get_match(
            tournament_id, int(reminder["match_id"])
        )
        if match is None or match["state"] != "open":
            return
        tournament = await self.bot.store.get_tournament(tournament_id)
        if tournament is None:
            return

        kind = reminder["kind"]
        if kind in DEADLINE_KINDS:
            # Ignore if time was already agreed.
            if match["scheduled_at"]:
                return
            if kind == "sched_deadline":
                await self._escalate(tournament, match)
            else:
                await self._nudge(tournament, match, urgent=kind.endswith("2"))
            return

        await self._match_reminder(tournament, match, kind)

    # ------------------------------------------------------------- helpers

    async def _thread_for(self, match: aiosqlite.Row) -> discord.Thread | None:
        thread_id = match["thread_id"]
        if not thread_id:
            return None
        thread = self.bot.get_channel(int(thread_id))
        if thread is None:
            try:
                thread = await self.bot.fetch_channel(int(thread_id))
            except discord.HTTPException:
                return None
        return thread if isinstance(thread, discord.Thread) else None

    async def _mentions(self, tournament_id: int, match: aiosqlite.Row) -> str:
        ids = await self.bot.store.discord_ids_for(
            tournament_id,
            [int(p) for p in (match["player1_id"], match["player2_id"]) if p],
        )
        return " ".join(f"<@{uid}>" for uid in ids.values())

    # -------------------------------------------------------------- senders

    async def _match_reminder(
        self, tournament: aiosqlite.Row, match: aiosqlite.Row, kind: str
    ) -> None:
        thread = await self._thread_for(match)
        if thread is None:
            return
        mentions = await self._mentions(int(tournament["challonge_id"]), match)
        when = _parse_iso(match["scheduled_at"])
        phrase = REMINDER_TEXT.get(kind, "soon")
        await thread.send(
            f"{mentions} your match is {phrase}"
            + (f", at {discord_ts(when, 't')}." if when else ".")
        )

    async def _nudge(
        self, tournament: aiosqlite.Row, match: aiosqlite.Row, *, urgent: bool
    ) -> None:
        from ui.embeds import nudge_embed

        thread = await self._thread_for(match)
        if thread is None:
            return
        tournament_id = int(tournament["challonge_id"])
        deadline = _parse_iso(match["deadline_at"])
        if deadline is None:
            return

        # Check for unanswered proposal.
        pending = await self.bot.store.pending_proposal(
            tournament_id, int(match["match_id"])
        )
        awaiting_id = (
            int(pending["responder_id"])
            if pending and pending["responder_id"]
            else None
        )

        from ui.views import MatchThreadView

        await thread.send(
            content=await self._mentions(tournament_id, match) or None,
            embed=nudge_embed(
                match,
                await self.bot.store.participant_names(tournament_id),
                deadline=deadline,
                awaiting_id=awaiting_id,
                urgent=urgent,
            ),
            view=MatchThreadView(self.bot),
        )

    async def _escalate(
        self, tournament: aiosqlite.Row, match: aiosqlite.Row
    ) -> None:
        """Deadline blown. Hand it to the organisers, never auto-drop a player."""
        from ui.embeds import escalation_embed
        from ui.views import EscalationView

        tournament_id = int(tournament["challonge_id"])
        match_id = int(match["match_id"])
        await self.bot.store.set_scheduling_status(
            tournament_id, match_id, "escalated"
        )

        thread = await self._thread_for(match)
        if thread is not None:
            await thread.send(
                "The deadline to agree a time has passed, so an organiser "
                "has been asked to step in. You can still agree a time "
                "yourselves in the meantime."
            )

        channel_id = tournament["channel_id"]
        channel = self.bot.get_channel(int(channel_id)) if channel_id else None
        if channel is None:
            return

        config = await self.bot.store.get_guild_config(int(tournament["guild_id"]))
        role_id = config["to_role_id"] if config else None

        message = await channel.send(
            content=f"<@&{int(role_id)}>" if role_id else None,
            embed=escalation_embed(
                tournament,
                match,
                await self.bot.store.participant_names(tournament_id),
                await self.bot.store.list_proposals(tournament_id, match_id),
            ),
            view=EscalationView(self.bot),
        )
        await self.bot.store.set_escalation_message(tournament_id, match_id, message.id)

    @reminder_loop.before_loop
    async def before_reminder_loop(self) -> None:
        await self.bot.wait_until_ready()


async def setup(bot) -> None:
    await bot.add_cog(SchedulingCog(bot))
