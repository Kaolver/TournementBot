"""Private match thread management."""

from __future__ import annotations

import logging

import aiosqlite
import discord
from discord.ext import commands

from cogs.common import match_title, round_label
from services import start_scheduling_window

log = logging.getLogger(__name__)

THREAD_NAME_LIMIT = 100


class ThreadsCog(commands.Cog):
    def __init__(self, bot) -> None:
        self.bot = bot

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
        await self._open_threads(tournament, announce=True)

    @commands.Cog.listener()
    async def on_player_claimed(self, tournament: aiosqlite.Row) -> None:
        """Someone identified themselves, which may complete a pairing."""
        await self._open_threads(tournament, announce=False)

    async def _open_threads(
        self, tournament: aiosqlite.Row, *, announce: bool
    ) -> None:
        channel = self._channel(tournament)
        if channel is None:
            log.warning(
                "no channel for %s; cannot open match threads", tournament["name"]
            )
            return

        tournament_id = int(tournament["challonge_id"])
        names = await self.bot.store.participant_names(tournament_id)
        display = await self.bot.store.participant_display(tournament_id)
        created: list[aiosqlite.Row] = []

        for match in await self.bot.store.matches_needing_threads(tournament_id):
            players = await self.bot.store.discord_ids_for(
                tournament_id,
                [int(match["player1_id"]), int(match["player2_id"])],
            )
            # Both players must be linked on Discord.
            if len(players) < 2:
                continue
            try:
                await self._open_thread(
                    channel,
                    tournament,
                    match,
                    names,
                    display,
                    list(players.values()),
                )
                fresh = await self.bot.store.get_match(
                    tournament_id, int(match["match_id"])
                )
                if fresh is not None:
                    created.append(fresh)
            except discord.Forbidden:
                log.warning("missing thread permissions in #%s", channel.name)
                await channel.send(
                    "I need **Create Private Threads**, **Send Messages in "
                    "Threads** and **Manage Threads** here to open match threads."
                )
                return
            except discord.HTTPException:
                log.exception(
                    "could not create thread for match %s", match["match_id"]
                )

        if created and announce:
            await self._announce_round(channel, tournament, created, display)

    async def _announce_round(
        self,
        channel: discord.TextChannel,
        tournament: aiosqlite.Row,
        matches: list[aiosqlite.Row],
        names: dict[int, str],
    ) -> None:
        """Tell the channel a new round is live. Costs nothing."""
        from ui.embeds import round_announcement_embed

        try:
            await channel.send(
                embed=round_announcement_embed(tournament, matches, names)
            )
        except discord.HTTPException:
            log.warning("could not post round announcement")

    # -------------------------------------------------------------- creation

    async def _open_thread(
        self,
        channel: discord.TextChannel,
        tournament: aiosqlite.Row,
        match: aiosqlite.Row,
        names: dict[int, str],
        display: dict[int, str],
        player_ids: list[int],
    ) -> None:
        from ui.embeds import match_thread_embed
        from ui.views import MatchThreadView

        live = " [LIVE]" if match["live"] else ""
        title = f"{round_label(match['round'])} · {match_title(match, names)}{live}"
        thread = await channel.create_thread(
            name=title[:THREAD_NAME_LIMIT],
            type=discord.ChannelType.private_thread,
            invitable=False,  # players cannot pull outsiders in
            auto_archive_duration=10080,
            reason=f"Match {match['identifier']} in {tournament['name']}",
        )
        tournament_id = int(tournament["challonge_id"])
        await self.bot.store.set_match_thread(
            tournament_id, int(match["match_id"]), thread.id
        )
        # Start scheduling window and queue reminders.
        await start_scheduling_window(self.bot, tournament, match)
        match = (
            await self.bot.store.get_match(tournament_id, int(match["match_id"]))
            or match
        )

        message = await thread.send(
            content=" ".join(f"<@{uid}>" for uid in player_ids),
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
