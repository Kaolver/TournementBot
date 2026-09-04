"""Shared helpers for the cogs: permission checks and error surfacing."""

from __future__ import annotations

import logging
from datetime import datetime, timezone

import aiosqlite
import discord
from discord import app_commands

from challonge.budget import BudgetExhausted
from challonge.client import ChallongeError, RateLimited

log = logging.getLogger(__name__)

# Every command description starts with one of these so it is obvious, in the
# Discord command picker, which commands eat the monthly Challonge allowance.
API_MARK = ""
FREE_MARK = ""


class BotError(Exception):
    """An expected, user-facing failure. Shown verbatim, no stack trace."""


async def respond(
    interaction: discord.Interaction,
    content: str | None = None,
    *,
    embed: discord.Embed | None = None,
    view: discord.ui.View | None = None,
    ephemeral: bool = True,
) -> None:
    """Reply whether or not the interaction was already deferred."""
    kwargs: dict[str, object] = {"ephemeral": ephemeral}
    if content is not None:
        kwargs["content"] = content
    if embed is not None:
        kwargs["embed"] = embed
    if view is not None:
        kwargs["view"] = view

    if interaction.response.is_done():
        await interaction.followup.send(**kwargs)  # type: ignore[arg-type]
    else:
        await interaction.response.send_message(**kwargs)  # type: ignore[arg-type]


async def report_error(interaction: discord.Interaction, error: Exception) -> None:
    """Turn an exception into something a tournament organiser can act on."""
    if isinstance(error, app_commands.CommandInvokeError):
        error = error.original  # type: ignore[assignment]

    if isinstance(error, BotError):
        message = str(error)
    elif isinstance(error, BudgetExhausted):
        message = f"Budget exhausted: {error}"
    elif isinstance(error, RateLimited):
        message = (
            "Challonge returned **429 Too Many Requests**. The free "
            "tier allows 500 requests/month. `/tournament board` shows your "
            "usage; the alternative is upgrading the Challonge plan."
        )
    elif isinstance(error, ChallongeError):
        message = f"Challonge error ({error.status}): " + "; ".join(
            error.details
        )
    elif isinstance(error, app_commands.CheckFailure):
        message = (
            "Permission required: That is a tournament admin command. You need the tournament "
            "admin role, or Manage Server."
        )
    elif isinstance(error, discord.Forbidden):
        message = (
            "Permission denied: Discord refused that. Check the bot has **Create Private "
            "Threads**, **Send Messages in Threads** and **Manage Threads** in "
            "this channel."
        )
    else:
        log.exception("unexpected command error", exc_info=error)
        message = f"Unexpected error: `{type(error).__name__}: {error}`"

    try:
        await respond(interaction, message)
    except discord.HTTPException:
        log.exception("failed to deliver error message")


async def is_organizer_user(store, guild_id: int, member: object) -> bool:
    """Check if member has tournament admin role or Manage Server permissions."""
    if not isinstance(member, discord.Member):
        return False
    if member.guild_permissions.manage_guild:
        return True
    config = await store.get_guild_config(guild_id)
    role_id = config["to_role_id"] if config else None
    return bool(role_id) and any(r.id == int(role_id) for r in member.roles)


def organizer_hint(config) -> str:
    """How to describe who may use the admin commands."""
    role_id = config["to_role_id"] if config else None
    if role_id:
        return f"<@&{int(role_id)}> and anyone who can manage the server"
    return "anyone who can manage the server"


def is_organizer():
    """Gate a command behind the tournament admin role, or Manage Server."""

    async def predicate(interaction: discord.Interaction) -> bool:
        store = interaction.client.store  # type: ignore[attr-defined]
        if await is_organizer_user(store, interaction.guild_id, interaction.user):
            return True
        raise app_commands.CheckFailure("not a tournament admin")

    return app_commands.check(predicate)


async def active_tournament(interaction: discord.Interaction) -> aiosqlite.Row:
    store = interaction.client.store  # type: ignore[attr-defined]
    row = await store.get_active_tournament(interaction.guild_id)
    if row is None:
        raise BotError(
            "No tournament is running in this server.\n"
            "A tournament admin points the bot at a Challonge bracket with "
            "`/tournament post url:<link>`."
        )
    return row


def discord_ts(dt: datetime, style: str = "F") -> str:
    """Render a Discord timestamp so every viewer sees their own timezone."""
    return f"<t:{int(dt.astimezone(timezone.utc).timestamp())}:{style}>"


def match_title(row: aiosqlite.Row, names: dict[int, str]) -> str:
    p1 = names.get(row["player1_id"], "TBD") if row["player1_id"] else "TBD"
    p2 = names.get(row["player2_id"], "TBD") if row["player2_id"] else "TBD"
    return f"{p1} vs {p2}"


def round_label(round_number: int) -> str:
    """Challonge uses negative rounds for the losers bracket."""
    if round_number < 0:
        return f"Losers R{abs(round_number)}"
    return f"Round {round_number}"
