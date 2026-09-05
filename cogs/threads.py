"""Match thread management.

Every match gets a thread. Threads are **public**: anyone who can see the
tournament channel can read them, and access is managed with ordinary channel
permissions rather than by Discord's private-thread membership. Players are
added to their own thread so it turns up in their sidebar, but a match whose
players never signed up on Discord still gets a thread - the organisers use it
just the same.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass, field

import aiosqlite
import discord
from discord.ext import commands

from cogs.common import match_title, round_label
from services import start_scheduling_window

log = logging.getLogger(__name__)

THREAD_NAME_LIMIT = 100
PERMISSION_HINT = (
    "I need **Create Public Threads**, **Send Messages in Threads** and "
    "**Manage Threads** in the tournament channel to open match threads."
)


@dataclass
class ThreadReport:
    """What one pass of thread-opening actually did."""

    created: list[aiosqlite.Row] = field(default_factory=list)
    existing: list[aiosqlite.Row] = field(default_factory=list)
    failed: list[tuple[aiosqlite.Row, str]] = field(default_factory=list)
    invited: int = 0
    unreachable: list[str] = field(default_factory=list)
    channel: discord.TextChannel | None = None
    error: str | None = None

    @property
    def total(self) -> int:
        return len(self.created) + len(self.existing) + len(self.failed)


class ThreadsCog(commands.Cog):
    def __init__(self, bot) -> None:
        self.bot = bot
        self._locks: dict[int, asyncio.Lock] = {}
        self._manual: set[int] = set()

    def _lock(self, tournament_id: int) -> asyncio.Lock:
        return self._locks.setdefault(tournament_id, asyncio.Lock())

    @contextlib.contextmanager
    def manual(self, tournament_id: int):
        """Pause automatic thread creation for a tournament."""
        self._manual.add(tournament_id)
        try:
            yield
        finally:
            self._manual.discard(tournament_id)

    def _channel(self, tournament: aiosqlite.Row) -> discord.TextChannel | None:
        guild = self.bot.get_guild(int(tournament["guild_id"]))
        if guild is None:
            return None
        channel_id = tournament["channel_id"]
        channel = guild.get_channel(int(channel_id)) if channel_id else None
        return channel if isinstance(channel, discord.TextChannel) else None

    # ------------------------------------------------------------- triggers

    @commands.Cog.listener()
    async def on_matches_opened(
        self, tournament: aiosqlite.Row, matches: list[aiosqlite.Row]
    ) -> None:
        if int(tournament["challonge_id"]) in self._manual:
            return
        await self.open_threads(tournament, announce=True)

    @commands.Cog.listener()
    async def on_player_claimed(self, tournament: aiosqlite.Row) -> None:
        await self.sync_thread_members(tournament)

    # -------------------------------------------------------------- opening

    async def open_threads(
        self,
        tournament: aiosqlite.Row,
        *,
        announce: bool,
        include_pending: bool = False,
    ) -> ThreadReport:
        """Create threads for all matches without one."""
        async with self._lock(int(tournament["challonge_id"])):
            return await self._open_threads(
                tournament, announce=announce, include_pending=include_pending
            )

    async def _open_threads(
        self,
        tournament: aiosqlite.Row,
        *,
        announce: bool,
        include_pending: bool = False,
    ) -> ThreadReport:
        report = ThreadReport()
        channel = self._channel(tournament)
        if channel is None:
            report.error = (
                "I cannot see the tournament channel any more. Re-post the "
                "panel with `/tournament repost` in the channel you want."
            )
            log.warning(
                "no channel for %s; cannot open match threads", tournament["name"]
            )
            return report
        report.channel = channel

        tournament_id = int(tournament["challonge_id"])
        names = await self.bot.store.participant_names(tournament_id)
        display = await self.bot.store.participant_display(tournament_id)

        pending = await self.bot.store.matches_needing_threads(
            tournament_id, include_pending=include_pending
        )
        for match in pending:
            wanted = [
                int(pid)
                for pid in (match["player1_id"], match["player2_id"])
                if pid is not None
            ]
            players = await self.bot.store.discord_ids_for(tournament_id, wanted)
            missing = [names.get(pid, "TBD") for pid in wanted if pid not in players]
            try:
                await self._open_thread(
                    channel,
                    tournament,
                    match,
                    names,
                    display,
                    list(players.values()),
                    missing,
                )
            except discord.Forbidden:
                log.warning("missing thread permissions in #%s", channel.name)
                report.error = PERMISSION_HINT
                report.failed.append((match, "missing permissions"))
                break
            except discord.HTTPException as exc:
                log.exception(
                    "could not create thread for match %s", match["match_id"]
                )
                report.failed.append((match, str(exc)))
                continue

            report.invited += len(players)
            report.unreachable.extend(missing)
            fresh = await self.bot.store.get_match(
                tournament_id, int(match["match_id"])
            )
            report.created.append(fresh or match)

        made = {int(row["match_id"]) for row in report.created}
        report.existing = [
            row
            for row in await self.bot.store.list_matches(tournament_id, state="open")
            if row["thread_id"] and int(row["match_id"]) not in made
        ]

        if report.created and announce:
            await self.announce_round(channel, tournament, report.created, display)
        return report

    async def announce_round(
        self,
        channel: discord.abc.Messageable,
        tournament: aiosqlite.Row,
        matches: list[aiosqlite.Row],
        names: dict[int, str],
    ) -> discord.Message | None:
        """Announce open matches in the tournament channel."""
        from ui.embeds import round_announcement_embed

        try:
            return await channel.send(
                embed=round_announcement_embed(tournament, matches, names)
            )
        except discord.HTTPException:
            log.warning("could not post round announcement")
            return None

    # -------------------------------------------------------------- creation

    async def _open_thread(
        self,
        channel: discord.TextChannel,
        tournament: aiosqlite.Row,
        match: aiosqlite.Row,
        names: dict[int, str],
        display: dict[int, str],
        player_ids: list[int],
        unlinked: list[str],
    ) -> None:
        from ui.embeds import match_thread_embed
        from ui.views import MatchThreadView

        live = " [LIVE]" if match["live"] else ""
        title = f"{round_label(match['round'])} - {match_title(match, names)}{live}"
        thread = await channel.create_thread(
            name=title[:THREAD_NAME_LIMIT],
            type=discord.ChannelType.public_thread,
            auto_archive_duration=10080,
            reason=f"Match {match['identifier']} in {tournament['name']}",
        )
        tournament_id = int(tournament["challonge_id"])
        await self.bot.store.set_match_thread(
            tournament_id, int(match["match_id"]), thread.id
        )
        await start_scheduling_window(self.bot, tournament, match)
        match = (
            await self.bot.store.get_match(tournament_id, int(match["match_id"]))
            or match
        )

        content = " ".join(f"<@{uid}>" for uid in player_ids)
        if unlinked:
            note = ", ".join(f"**{name}**" for name in unlinked)
            tail = (
                f"{note} has not signed up on Discord yet, so nobody is pinged "
                "for them. An organiser can relay times here."
            )
            content = f"{content}\n-# {tail}".strip()

        message = await thread.send(
            content=content or None,
            embed=match_thread_embed(tournament, match, display),
            view=MatchThreadView(self.bot),
        )
        try:
            await message.pin()
        except discord.HTTPException:
            pass

        for user_id in player_ids:
            try:
                await thread.add_user(discord.Object(id=user_id))
            except discord.HTTPException:
                log.warning("could not add %s to thread %s", user_id, thread.id)

    async def sync_thread_members(self, tournament: aiosqlite.Row) -> int:
        """Add newly linked players to existing match threads."""
        tournament_id = int(tournament["challonge_id"])
        guild = self.bot.get_guild(int(tournament["guild_id"]))
        if guild is None:
            return 0

        added = 0
        for match in await self.bot.store.list_matches(tournament_id, state="open"):
            if not match["thread_id"]:
                continue
            thread = guild.get_thread(int(match["thread_id"]))
            if thread is None or thread.archived:
                continue
            wanted = [
                int(pid)
                for pid in (match["player1_id"], match["player2_id"])
                if pid is not None
            ]
            players = await self.bot.store.discord_ids_for(tournament_id, wanted)
            for user_id in players.values():
                try:
                    await thread.add_user(discord.Object(id=user_id))
                    added += 1
                except discord.HTTPException:
                    continue
        return added

    # ------------------------------------------------------------ completion

    @commands.Cog.listener()
    async def on_match_completed(
        self, tournament: aiosqlite.Row, match: aiosqlite.Row
    ) -> None:
        """Wrap up the thread once a result lands.

        Kept, not deleted: it is the record if the result is ever questioned.
        """
        thread_id = match["thread_id"]
        if not thread_id:
            return
        guild = self.bot.get_guild(int(tournament["guild_id"]))
        if guild is None:
            return

        thread = guild.get_thread(int(thread_id))
        if thread is None:
            try:
                thread = await guild.fetch_channel(int(thread_id))
            except discord.HTTPException:
                return
        if not isinstance(thread, discord.Thread):
            return

        display = await self.bot.store.participant_display(
            int(tournament["challonge_id"])
        )
        winner = display.get(match["winner_id"], "the winner")
        try:
            await thread.send(
                f"**{winner}** takes it `{match['scores'] or ''}`. Archiving "
                "this thread; an organiser can unarchive it if the result needs "
                "revisiting."
            )
            await thread.edit(archived=True)
        except discord.HTTPException:
            log.warning("could not archive thread %s", thread_id)


async def setup(bot) -> None:
    await bot.add_cog(ThreadsCog(bot))
