"""Tournament administrative slash commands."""

from __future__ import annotations

import logging

import discord
from discord import app_commands
from discord.ext import commands

from challonge.client import NotFound, slug_from
from cogs.common import (
    BotError,
    active_tournament,
    discord_ts,
    is_organizer,
    respond,
)
from services import sync_allowance
from timeparse import get_zone

log = logging.getLogger(__name__)


class TournamentCog(commands.Cog):
    def __init__(self, bot) -> None:
        self.bot = bot

    tournament_group = app_commands.Group(
        name="tournament",
        description="Tournament admin. Requires the tournament admin role.",
        guild_only=True,
        default_permissions=discord.Permissions(manage_guild=True),
    )
    sync_group = app_commands.Group(
        name="sync",
        description="Read things back off Challonge.",
        parent=tournament_group,
    )
    round_group = app_commands.Group(
        name="round",
        description="Run the round: threads, players, announcement.",
        parent=tournament_group,
    )

    # ------------------------------------------------------------------ post

    @tournament_group.command(name="post")
    @app_commands.describe(
        url="The Challonge bracket: paste its link, or just the id"
    )
    @is_organizer()
    async def post(self, interaction: discord.Interaction, url: str) -> None:
        """Post the sign-up panel for an existing Challonge bracket. 3 requests."""
        await interaction.response.defer(ephemeral=True)

        slug = slug_from(url)
        try:
            fetched = await self.bot.challonge.get_tournament(
                slug, reason="admin:post"
            )
        except NotFound:
            raise BotError(
                f"Challonge has no bracket called `{slug}`.\n"
                "Paste the full link, and check the bracket sits on the account "
                "whose API key this bot uses. Private brackets are fine, "
                "someone else's are not."
            ) from None

        await self.bot.store.save_tournament(
            fetched,
            guild_id=interaction.guild_id,
            channel_id=interaction.channel_id,
        )
        replaced = await self.bot.store.archive_others(
            interaction.guild_id, fetched.id
        )

        await self.bot.store.replace_participants(
            fetched.id,
            await self.bot.challonge.list_participants(
                fetched.id, reason="admin:post"
            ),
        )
        # Link existing sign-ups.
        await self.bot.store.link_signups_to_bracket(fetched.id)

        row = await self.bot.store.get_tournament(fetched.id)
        opened, _ = await self.bot.refresh_matches(row, reason="admin:post")
        message = await self.post_panel(interaction.channel, fetched.id)

        lines = [f"Panel posted: {message.jump_url}"]
        if replaced:
            lines.append("The previous tournament has been archived.")
        if opened:
            lines.append(f"{len(opened)} match thread(s) opened.")
        await respond(interaction, "\n".join(lines))

    async def post_panel(self, channel, tournament_id: int):
        """Post (or re-post) the panel players interact with."""
        from ui.embeds import panel_embed
        from ui.views import PanelView

        tournament = await self.bot.store.get_tournament(tournament_id)
        message = await channel.send(
            embed=await panel_embed(self.bot, tournament),
            view=PanelView(self.bot),
        )
        await self.bot.store.set_signup_message(
            tournament_id, channel.id, message.id
        )
        return message

    @tournament_group.command(name="repost")
    @is_organizer()
    async def repost(self, interaction: discord.Interaction) -> None:
        """Post the panel again, in this channel. Free."""
        await interaction.response.defer(ephemeral=True)
        tournament = await active_tournament(interaction)
        message = await self.post_panel(
            interaction.channel, int(tournament["challonge_id"])
        )
        await respond(interaction, f"Panel posted: {message.jump_url}")

    # -------------------------------------------------------------- settings

    @tournament_group.command(name="settings")
    @app_commands.describe(
        admin_role="Role allowed to run these commands",
        timezone="IANA name, used to read the times players type, e.g. Europe/Berlin",
        hours_to_agree="Hours players get to agree a match time. 0 turns it off",
        auto_sync="Let the bot read the bracket by itself. Off means you sync by hand",
        syncs_per_day="Most reads the bot may spend in a day, on its own. 0 is manual only",
        featured_venue="Voice or stage channel for featured matches",
        featured_link="Or a stream link, if there is no voice channel",
        featured_minutes="How long a featured match usually runs",
    )
    @is_organizer()
    async def settings(
        self,
        interaction: discord.Interaction,
        admin_role: discord.Role | None = None,
        timezone: str | None = None,
        hours_to_agree: int | None = None,
        auto_sync: bool | None = None,
        syncs_per_day: int | None = None,
        featured_venue: discord.VoiceChannel | discord.StageChannel | None = None,
        featured_link: str | None = None,
        featured_minutes: int | None = None,
    ) -> None:
        """Configure the bot, or show the current settings. Free."""
        await interaction.response.defer(ephemeral=True)
        from ui.embeds import settings_embed

        changed: list[str] = []

        if timezone is not None:
            zone = get_zone(timezone)
            if getattr(zone, "key", "UTC") != timezone and timezone.upper() != "UTC":
                raise BotError(
                    f"`{timezone}` is not a timezone I know. Use an IANA name "
                    "such as `Europe/Berlin`."
                )
            changed.append(f"timezone **{timezone}**")

        if hours_to_agree is not None:
            if not 0 <= hours_to_agree <= 336:
                raise BotError("Pick between 0 and 336 hours (two weeks).")
            changed.append(
                f"deadline **{hours_to_agree}h**"
                if hours_to_agree
                else "deadline **off**"
            )

        if auto_sync is not None:
            changed.append(
                "automatic syncing **on**" if auto_sync else "automatic syncing **off**"
            )
        if syncs_per_day is not None:
            if not 0 <= syncs_per_day <= 200:
                raise BotError("Pick between 0 and 200 syncs a day.")
            changed.append(
                f"allowance **{syncs_per_day} reads/day**"
                if syncs_per_day
                else "allowance **manual only**"
            )

        if featured_minutes is not None:
            if not 5 <= featured_minutes <= 1440:
                raise BotError("Featured match length must be 5 to 1440 minutes.")
            changed.append(f"featured length **{featured_minutes} min**")

        if admin_role is not None:
            changed.append(f"admin role {admin_role.mention}")
        if featured_venue is not None:
            changed.append(f"featured venue {featured_venue.mention}")
        if featured_link is not None:
            changed.append(f"featured link **{featured_link}**")

        if changed:
            await self.bot.store.set_guild_config(
                interaction.guild_id,
                to_role_id=admin_role.id if admin_role else None,
                tz=timezone,
                deadline_hours=hours_to_agree,
                auto_sync=auto_sync,
                syncs_per_day=syncs_per_day,
                event_channel_id=featured_venue.id if featured_venue else None,
                event_location=featured_link,
                event_duration=featured_minutes,
                clear_event_channel=(
                    featured_link is not None and featured_venue is None
                ),
            )

        config = await self.bot.store.get_guild_config(interaction.guild_id)
        await respond(
            interaction,
            f"Updated: {', '.join(changed)}." if changed else None,
            embed=settings_embed(
                interaction.guild,
                config,
                await self.bot.budget.status(),
                syncs_per_day=await sync_allowance(self.bot, interaction.guild_id),
            ),
        )

    # ------------------------------------------------------------ monitoring

    @tournament_group.command(name="board")
    @is_organizer()
    async def board(self, interaction: discord.Interaction) -> None:
        """Progress, who is stalling, and how much quota is left. Free."""
        await interaction.response.defer(ephemeral=True)
        from ui.embeds import board_embed

        tournament = await active_tournament(interaction)
        tournament_id = int(tournament["challonge_id"])
        matches = await self.bot.store.list_matches(tournament_id)
        open_matches = [m for m in matches if m["state"] == "open"]
        participants = await self.bot.store.list_participants(tournament_id)
        schedule = await schedule_line(self.bot, tournament)

        await respond(
            interaction,
            embed=board_embed(
                tournament,
                open_matches,
                await self.bot.store.participant_display(tournament_id),
                budget=await self.bot.budget.status(),
                syncs_per_day=await sync_allowance(self.bot, interaction.guild_id),
                participant_count=len(participants),
                unclaimed_count=sum(
                    1 for p in participants if not p["discord_user_id"]
                ),
                signup_count=len(await self.bot.store.list_signups(tournament_id)),
                total_matches=len(matches),
                completed_matches=sum(
                    1 for m in matches if m["state"] == "complete"
                ),
                threadless=sum(1 for m in open_matches if not m["thread_id"]),
                schedule=schedule,
            ),
        )

    # ------------------------------------------------------ sync / round

    @sync_group.command(name="entrants")
    @is_organizer()
    async def sync_entrants_command(self, interaction: discord.Interaction) -> None:
        """Read the entrant list off Challonge and link sign-ups. 1 request."""
        await interaction.response.defer(ephemeral=True)
        tournament = await active_tournament(interaction)
        await run_entrant_sync(self.bot, interaction, tournament)

    @round_group.command(name="start")
    @is_organizer()
    async def round_start(self, interaction: discord.Interaction) -> None:
        """Sync matches, open a thread for every one, invite the players."""
        await interaction.response.defer(ephemeral=True)
        tournament = await active_tournament(interaction)
        await run_round_start(self.bot, interaction, tournament)

    @round_group.command(name="schedule")
    @app_commands.describe(
        first_day="Day round one is played: YYYY-MM-DD. 'off' clears the calendar",
        days_per_round="Days from each round to the next. 0 puts them all on day one",
        round_days="Or the exact days after the first, e.g. 0,3,7,10 (max day 13)",
    )
    @is_organizer()
    async def round_schedule(
        self,
        interaction: discord.Interaction,
        first_day: str | None = None,
        days_per_round: int | None = None,
        round_days: str | None = None,
    ) -> None:
        """Set the match days and round schedule."""
        await interaction.response.defer(ephemeral=True)
        tournament = await active_tournament(interaction)
        await run_round_schedule(
            self.bot,
            interaction,
            tournament,
            first_day=first_day,
            days_per_round=days_per_round,
            round_days=round_days,
        )

    @tournament_group.command(name="entrants")
    @app_commands.describe(
        player="Remove this player's Discord sign-up",
    )
    @is_organizer()
    async def entrants(
        self,
        interaction: discord.Interaction,
        player: discord.Member | None = None,
    ) -> None:
        """See who signed up on Discord, or remove one. Free."""
        await interaction.response.defer(ephemeral=True)
        from ui.embeds import entrants_embed

        tournament = await active_tournament(interaction)
        tournament_id = int(tournament["challonge_id"])

        note = None
        if player is not None:
            removed = await self.bot.store.remove_signup(tournament_id, player.id)
            existing = await self.bot.store.participant_for_discord(
                tournament_id, player.id
            )
            if existing is not None:
                await self.bot.store.unlink_participant(
                    tournament_id, int(existing["participant_id"])
                )
            note = (
                f"Removed {player.mention}'s sign-up."
                if removed
                else f"{player.mention} had not signed up."
            )
            await refresh_panel_safe(self.bot, interaction.guild, tournament)

        await respond(
            interaction,
            note,
            embed=await entrants_embed(self.bot, tournament),
        )


async def refresh_panel_safe(bot, guild, tournament) -> None:
    from ui.views import refresh_panel

    await refresh_panel(bot, guild, tournament)


async def run_entrant_sync(bot, interaction: discord.Interaction, tournament) -> None:
    """Sync entrants and update thread memberships."""
    from services import sync_entrants
    from ui.embeds import entrants_sync_embed
    from ui.views import EntrantsSyncView

    result = await sync_entrants(bot, tournament)

    joined = 0
    threads = bot.get_cog("ThreadsCog")
    if threads is not None and result.linked_now:
        joined = await threads.sync_thread_members(tournament)

    fresh = await bot.store.get_tournament(int(tournament["challonge_id"]))
    await refresh_panel_safe(bot, interaction.guild, fresh or tournament)

    await respond(
        interaction,
        embed=entrants_sync_embed(
            fresh or tournament,
            result,
            budget=await bot.budget.status(),
            thread_joins=joined,
        ),
        view=EntrantsSyncView(bot),
    )


OFF_WORDS = {"off", "none", "clear", "never", "-"}


async def schedule_line(bot, tournament) -> str | None:
    """Format round schedule status line."""
    from services import end_of_day, guild_timezone, late_matches, round_plan
    from timeparse import get_zone

    plan = await round_plan(bot, tournament)
    if plan is None:
        return None

    zone = get_zone(await guild_timezone(bot, int(tournament["guild_id"])))
    closes = end_of_day(plan.last_day, zone)
    line = (
        f"**{plan.total_days}** day(s) in all, ending {discord_ts(closes, 'D')} "
        f"({discord_ts(closes, 'R')}).\n`/tournament round schedule` to move it."
    )
    late = late_matches(
        await bot.store.list_matches(int(tournament["challonge_id"]), state="open")
    )
    if late:
        line += f"\n**{len(late)}** match(es) are booked past their round."
    return line


async def run_round_schedule(
    bot,
    interaction: discord.Interaction,
    tournament,
    *,
    first_day: str | None = None,
    days_per_round: int | None = None,
    round_days: str | None = None,
) -> None:
    """View or update round schedule settings."""
    from services import (
        MAX_ROUND_DAYS,
        MAX_TOURNAMENT_DAYS,
        apply_round_plan,
        parse_day,
        parse_round_days,
    )
    from ui.embeds import round_schedule_embed
    from ui.views import RoundScheduleView

    guild_id = interaction.guild_id
    changed: list[str] = []
    clearing = first_day is not None and first_day.strip().lower() in OFF_WORDS

    if not clearing and first_day is not None:
        try:
            parse_day(first_day)
        except ValueError:
            raise BotError(
                f"`{first_day}` is not a date I can read. Write it as "
                "`YYYY-MM-DD`, for example `2026-09-12`. `off` clears the "
                "calendar."
            ) from None
        changed.append(f"first match day **{first_day.strip()}**")

    if days_per_round is not None and not 0 <= days_per_round <= MAX_ROUND_DAYS:
        raise BotError(
            f"Pick between 0 and {MAX_ROUND_DAYS} days between rounds. A "
            f"tournament runs {MAX_TOURNAMENT_DAYS} days at the most, so a "
            "bigger gap could not fit even two rounds."
        )
    if days_per_round is not None:
        changed.append(
            f"**{days_per_round} day(s)** between rounds"
            if days_per_round
            else "every round on the **same day**"
        )

    if round_days is not None and round_days.strip():
        try:
            parsed = parse_round_days(round_days)
        except ValueError as exc:
            raise BotError(
                f"I cannot read `{round_days}` as round days: {exc}. Give days "
                "counted from the first match day, lowest first, like `0,3,7`."
            ) from None
        changed.append("round days **" + ",".join(str(d) for d in parsed) + "**")

    config = await bot.store.get_guild_config(guild_id)
    existing_day = config["first_match_day"] if config else None
    if changed and not clearing and not (first_day or existing_day):
        raise BotError(
            "Give me the day round one is played first: "
            "`/tournament round schedule first_day:YYYY-MM-DD`."
        )

    applied = 0
    if clearing:
        await bot.store.set_guild_config(guild_id, clear_schedule=True)
        changed = ["calendar **cleared**"]
        applied = await apply_round_plan(bot, tournament)
    elif changed:
        await bot.store.set_guild_config(
            guild_id,
            first_match_day=first_day.strip() if first_day else None,
            days_per_round=days_per_round,
            round_days=round_days.strip() if round_days is not None else None,
        )
        applied = await apply_round_plan(bot, tournament)

    await respond(
        interaction,
        f"Updated: {', '.join(changed)}." if changed else None,
        embed=await round_schedule_embed(bot, tournament, restamped=applied),
        view=RoundScheduleView(bot),
    )


async def run_round_start(bot, interaction: discord.Interaction, tournament) -> None:
    """Sync bracket and open missing match threads."""
    from services import start_round
    from ui.embeds import round_start_embed
    from ui.views import RoundStartView

    result = await start_round(bot, tournament)

    tournament_id = int(tournament["challonge_id"])
    fresh = await bot.store.get_tournament(tournament_id) or tournament
    await refresh_panel_safe(bot, interaction.guild, fresh)

    await respond(
        interaction,
        embed=round_start_embed(
            fresh,
            result,
            names=await bot.store.participant_display(tournament_id),
            budget=await bot.budget.status(),
        ),
        view=RoundStartView(bot),
    )


async def setup(bot) -> None:
    await bot.add_cog(TournamentCog(bot))
