"""Tournament administrative slash commands."""

from __future__ import annotations

import logging

import discord
from discord import app_commands
from discord.ext import commands

from challonge.client import NotFound, slug_from
from cogs.common import (
    FREE_MARK,
    BotError,
    active_tournament,
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
            ),
        )

    @tournament_group.command(name="sync")
    @is_organizer()
    async def sync(self, interaction: discord.Interaction) -> None:
        """Read the bracket and entrants right now. Normally automatic."""
        await interaction.response.defer(ephemeral=True)
        tournament = await active_tournament(interaction)
        tournament_id = int(tournament["challonge_id"])

        # Fetch latest participants from Challonge so new entrants link to signups
        participants = await self.bot.challonge.list_participants(
            tournament_id, reason="admin:sync"
        )
        await self.bot.store.replace_participants(tournament_id, participants)
        linked = await self.bot.store.link_signups_to_bracket(tournament_id)

        opened, completed = await self.bot.refresh_matches(
            tournament, reason="admin:sync"
        )
        if opened and tournament["state"] == "pending":
            await self.bot.store.set_tournament_state(tournament_id, "underway")

        budget = await self.bot.budget.status()

        from ui.views import refresh_panel

        fresh = await self.bot.store.get_tournament(tournament_id)
        await refresh_panel(self.bot, interaction.guild, fresh)

        lines = [f"Synced **{len(participants)}** entrant(s) from Challonge."]
        if linked:
            lines.append(f"**{linked}** Discord sign-up(s) linked to the bracket!")
        if opened or completed:
            lines.append(f"**{len(opened)}** newly open, **{len(completed)}** newly completed.")
        await respond(interaction, "\n".join(lines))

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


async def setup(bot) -> None:
    await bot.add_cog(TournamentCog(bot))
