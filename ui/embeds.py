"""Discord embed builders for tournament views."""

from __future__ import annotations

from datetime import datetime
from typing import Sequence

import aiosqlite
import discord

from challonge.budget import BudgetStatus
from cogs.common import API_MARK, FREE_MARK, discord_ts, match_title, round_label

ACCENT = discord.Colour(0x5865F2)
GOOD = discord.Colour(0x57F287)
WARN = discord.Colour(0xFEE75C)
BAD = discord.Colour(0xED4245)
MUTED = discord.Colour(0x4E5058)

STATUS_LABEL = {
    "escalated": "Needs Attention",
    "pending": "No Time Proposed",
    "proposed": "Awaiting Reply",
    "agreed": "Agreed",
    "handled": "Handled",
}
STATUS_ICON = {k: "" for k in STATUS_LABEL}

LIVE_DOT = "[Live]"


def _url(tournament: aiosqlite.Row) -> str | None:
    return tournament["full_url"] or None


def _when(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def _column(row: aiosqlite.Row, name: str):
    """Safely get column value from row."""
    try:
        return row[name]
    except (KeyError, IndexError):
        return None


def _truncate(lines: Sequence[str], limit: int, noun: str, max_chars: int = 1000) -> str:
    """Join lines, truncating with a summary if limit is reached."""
    if not lines:
        return ""
    shown: list[str] = []
    total = 0
    count = 0
    for line in lines[:limit]:
        rem = len(lines) - count
        tail = f"\n*... and {rem} more {noun}*"
        if total + len(line) + (len(tail) if rem > 1 else 0) > max_chars:
            break
        shown.append(line)
        total += len(line) + 1
        count += 1

    if count < len(lines):
        shown.append(f"*... and {len(lines) - count} more {noun}*")
    return "\n".join(shown)


async def panel_embed(bot, tournament: aiosqlite.Row) -> discord.Embed:
    """Build the tournament panel embed."""
    tournament_id = int(tournament["challonge_id"])
    participants = await bot.store.list_participants(tournament_id)
    signups = await bot.store.list_signups(tournament_id)
    linked = [p for p in participants if p["discord_user_id"]]
    matches = await bot.store.list_matches(tournament_id)
    open_matches = [m for m in matches if m["state"] == "open"]

    state = tournament["state"]
    embed = discord.Embed(
        title=tournament["name"],
        url=_url(tournament),
        colour={"complete": GOOD, "pending": WARN}.get(state, ACCENT),
        description=(
            f"-# {(tournament['tournament_type'] or 'Tournament').title()} · {state.title()}"
        ),
    )

    embed.add_field(name="On the bracket", value=str(len(participants)), inline=True)
    embed.add_field(name="Signed up here", value=str(len(signups)), inline=True)
    if matches:
        done = sum(1 for m in matches if m["state"] == "complete")
        embed.add_field(
            name="Progress", value=f"{done}/{len(matches)} played", inline=True
        )

    if linked:
        embed.add_field(
            name=f"Reachable on Discord · {len(linked)} of {len(participants)}",
            value=_truncate(
                [f"<@{int(p['discord_user_id'])}>" for p in linked], 20, "players"
            ),
            inline=False,
        )

    if open_matches:
        waiting = sum(1 for m in open_matches if not m["scheduled_at"])
        embed.set_footer(
            text=f"{len(open_matches)} match(es) in progress"
            + (f", {waiting} without a time" if waiting else "")
        )
    else:
        embed.set_footer(text="Match threads will open when the tournament begins.")
    return embed


async def entrants_embed(bot, tournament: aiosqlite.Row) -> discord.Embed:
    """Build the entrants overview embed."""
    tournament_id = int(tournament["challonge_id"])
    participants = await bot.store.list_participants(tournament_id)
    signups = await bot.store.list_signups(tournament_id)
    bracket_names = {p["name"].casefold() for p in participants}

    linked = [p for p in participants if p["discord_user_id"]]
    on_bracket_only = [p for p in participants if not p["discord_user_id"]]
    waiting = [s for s in signups if s["name"].casefold() not in bracket_names]

    embed = discord.Embed(
        title=tournament["name"],
        url=_url(tournament),
        colour=ACCENT,
        description="**Entrants**",
    )
    embed.add_field(
        name=f"Linked ({len(linked)})",
        value=_truncate(
            [f"<@{int(p['discord_user_id'])}> as **{p['name']}**" for p in linked],
            20,
            "players",
        )
        or "*nobody yet*",
        inline=False,
    )
    if on_bracket_only:
        embed.add_field(
            name=f"Challonge only ({len(on_bracket_only)})",
            value=_truncate([p["name"] for p in on_bracket_only], 20, "players"),
            inline=False,
        )
    if waiting:
        embed.add_field(
            name=f"Pending Challonge bracket ({len(waiting)})",
            value=_truncate(
                [
                    f"<@{int(row['discord_user_id'])}> wants **{row['name']}**"
                    for row in waiting
                ],
                20,
                "players",
            ),
            inline=False,
        )
        embed.set_footer(
            text="Add those names on Challonge and they link themselves at the "
            "next sync."
        )
    return embed


def settings_embed(
    guild: discord.Guild,
    config: aiosqlite.Row | None,
    budget: BudgetStatus,
    *,
    syncs_per_day: int,
) -> discord.Embed:
    """Build the tournament settings embed."""
    embed = discord.Embed(
        title="Settings",
        colour=ACCENT,
        description=f"How the bot behaves in **{guild.name}**",
    )

    hours = (config["deadline_hours"] if config else 24) or 0
    event_channel_id = config["event_channel_id"] if config else None
    role_id = config["to_role_id"] if config else None

    embed.add_field(
        name="Timezone",
        value=(config["timezone"] if config else None) or "UTC",
        inline=True,
    )
    embed.add_field(
        name="Time to agree a match",
        value=f"{hours}h" if hours else "*no deadline*",
        inline=True,
    )
    embed.add_field(
        name="Featured match length",
        value=f"{(config['event_duration'] if config else 60) or 60} min",
        inline=True,
    )

    first_day = _column(config, "first_match_day") if config else None
    custom = (_column(config, "round_days") if config else None) or ""
    per_round = int((_column(config, "days_per_round") if config else 0) or 0)
    embed.add_field(
        name="Match days",
        value=(
            (
                f"Round one **{first_day}**, then "
                + (
                    f"days **{custom}**"
                    if custom.strip()
                    else (
                        f"**{per_round}** day(s) apart"
                        if per_round
                        else "**all on that day**"
                    )
                )
            )
            if first_day
            else "*not set* · `/tournament round schedule`"
        ),
        inline=False,
    )
    embed.add_field(
        name="Featured matches happen",
        value=(
            f"<#{event_channel_id}>"
            if event_channel_id
            else (config["event_location"] if config else None)
            or "*wherever the thread says*"
        ),
        inline=True,
    )
    embed.add_field(
        name="Tournament admins",
        value=(
            f"<@&{role_id}> and anyone who can manage the server"
            if role_id
            else "*no role set*, so anyone who can manage the server"
        ),
        inline=False,
    )

    me = guild.me
    needed = {
        "Create Public Threads": me.guild_permissions.create_public_threads,
        "Send Messages in Threads": me.guild_permissions.send_messages_in_threads,
        "Manage Threads": me.guild_permissions.manage_threads,
        "Manage Events": me.guild_permissions.manage_events,
    }
    missing = [name for name, ok in needed.items() if not ok]
    embed.add_field(
        name="Permissions",
        value=(
            "All set."
            if not missing
            else "Missing: **" + "**, **".join(missing) + "**"
        ),
        inline=False,
    )
    embed.add_field(
        name="Challonge requests this month",
        value=(
            f"`{budget.bar()}` **{budget.used}** of {budget.limit}\n"
            "The bot only ever reads. Everything you do on challonge.com "
            "is free."
        ),
        inline=False,
    )
    return embed


def match_thread_embed(
    tournament: aiosqlite.Row, match: aiosqlite.Row, names: dict[int, str]
) -> discord.Embed:
    is_live = bool(match["live"])
    scheduled = _when(match["scheduled_at"])
    deadline = _when(match["deadline_at"])
    play_by = _when(_column(match, "play_by"))

    embed = discord.Embed(
        title=match_title(match, names),
        colour=GOOD if scheduled else (BAD if is_live else ACCENT),
        url=_url(tournament),
        description=(
            f"{round_label(match['round'])} · match "
            f"`{match['identifier'] or match['match_id']}`"
            + ("\n**Featured match**" if is_live else "")
        ),
    )

    if scheduled:
        embed.add_field(
            name="Agreed Time",
            value=f"{discord_ts(scheduled)}\n{discord_ts(scheduled, 'R')}",
            inline=False,
        )
    elif deadline:
        embed.add_field(
            name="Scheduling Deadline",
            value=(
                f"{discord_ts(deadline)} ({discord_ts(deadline, 'R')})\n"
                "If you both go quiet, an organiser decides for you."
            ),
            inline=False,
        )

    if play_by:
        late = scheduled is not None and scheduled > play_by
        embed.add_field(
            name=f"{round_label(match['round'])} closes",
            value=(
                f"{discord_ts(play_by)} ({discord_ts(play_by, 'R')})\n"
                + (
                    "**Your agreed time is after that.** Either move the match "
                    "or ask an organiser to move the round."
                    if late
                    else "Play by then. That is what keeps the tournament to "
                    "its length."
                )
            ),
            inline=False,
        )
        if late:
            embed.colour = WARN

    embed.add_field(
        name="What to do",
        value=(
            "**1.** Press **Propose time** and offer a slot.\n"
            "**2.** Your opponent accepts it or counters with their own. "
            "Nothing is booked until you both agree.\n"
            "**3.** Play, then post the score here. An organiser enters it on "
            "Challonge and the bracket moves on by itself."
        ),
        inline=False,
    )
    embed.set_footer(
        text="Public thread: the server can read along, but only you two "
        "and the organisers use the buttons."
        + (" Featured matches get a server event." if is_live else "")
    )
    return embed


def room_embed(
    match: aiosqlite.Row, names: dict[int, str], code: str
) -> discord.Embed:
    """Build the match room embed."""
    embed = discord.Embed(
        title="Match Room Open",
        colour=GOOD,
        description=f"**{match_title(match, names)}**",
    )
    embed.add_field(name="Code", value=f"# {code}", inline=False)
    embed.add_field(
        name="How to use it",
        value=(
            "Open **Null Rush**, choose **Tournament match**, and type this "
            "code. Your opponent types the same one.\n"
            "When the match ends the result comes back here on its own, so "
            "nobody has to write down a score."
        ),
        inline=False,
    )
    embed.set_footer(text="The code stays valid until the match is reported.")
    return embed


def reported_embed(
    match: aiosqlite.Row,
    names: dict[int, str],
    *,
    winner_id: int | None,
    scores: str,
) -> discord.Embed:
    """Build the reported match result embed."""
    winner = names.get(winner_id, "the winner") if winner_id else "somebody"
    embed = discord.Embed(
        title="Result Reported by Null Rush",
        colour=WARN,
        description=f"**{winner}** wins `{scores}`\n*{match_title(match, names)}*",
    )
    embed.add_field(
        name="Not on the bracket yet",
        value=(
            "The game reported this; an organiser decides whether it counts. "
            "**Enter on Challonge** publishes it and moves the bracket on. "
            "Anyone can also just type it on challonge.com."
        ),
        inline=False,
    )
    embed.set_footer(text="Publishing uses 2 Challonge requests.")
    return embed


def proposal_embed(
    match: aiosqlite.Row,
    names: dict[int, str],
    *,
    proposer_id: int,
    responder_id: int | None,
    when: datetime,
) -> discord.Embed:
    embed = discord.Embed(
        title="Time Proposed",
        colour=WARN,
        description=(
            f"{discord_ts(when)}\n"
            f"*{discord_ts(when, 'R')}, shown in your own timezone*"
        ),
    )
    embed.add_field(name="Offered by", value=f"<@{proposer_id}>", inline=True)
    embed.add_field(
        name="Waiting on",
        value=f"<@{responder_id}>" if responder_id else "the other player",
        inline=True,
    )
    embed.set_footer(text="Accept, counter with your own time, or decline.")
    return embed


def agreed_embed(
    match: aiosqlite.Row, names: dict[int, str], when: datetime
) -> discord.Embed:
    embed = discord.Embed(
        title="Match Time Agreed",
        colour=GOOD,
        description=(
            f"**{match_title(match, names)}**\n"
            f"{discord_ts(when)} ({discord_ts(when, 'R')})"
        ),
    )
    reminders = "You will both be pinged here 1 hour and 5 minutes before."
    if match["live"]:
        reminders += "\nA server event has been published for this match."
    embed.add_field(name="Next", value=reminders, inline=False)
    return embed


def nudge_embed(
    match: aiosqlite.Row,
    names: dict[int, str],
    *,
    deadline: datetime,
    awaiting_id: int | None,
    urgent: bool,
) -> discord.Embed:
    if awaiting_id:
        ask = f"<@{awaiting_id}>, there is a time waiting for your answer."
    else:
        ask = "Neither of you has proposed a time yet."

    embed = discord.Embed(
        title="Scheduling Deadline Approaching" if urgent else "Match Scheduling Reminder",
        colour=BAD if urgent else WARN,
        description=ask,
    )
    embed.add_field(
        name="Deadline",
        value=f"{discord_ts(deadline)} ({discord_ts(deadline, 'R')})",
        inline=False,
    )
    embed.set_footer(text="After that an organiser steps in and decides.")
    return embed


def escalation_embed(
    tournament: aiosqlite.Row,
    match: aiosqlite.Row,
    names: dict[int, str],
    proposals: Sequence[aiosqlite.Row],
) -> discord.Embed:
    embed = discord.Embed(
        title="Scheduling Deadline Passed",
        colour=BAD,
        url=_url(tournament),
        description=(
            f"**{match_title(match, names)}**\n"
            f"{round_label(match['round'])}, no agreed time."
        ),
    )
    if proposals:
        history = _truncate(
            [
                f"<@{p['proposer_id']}> offered "
                f"{discord_ts(datetime.fromisoformat(p['proposed_at']), 'f')} "
                f"*({p['status']})*"
                for p in proposals
            ],
            5,
            "offers",
        )
    else:
        history = "*Neither player proposed anything.*"
    embed.add_field(name="What happened", value=history, inline=False)

    if match["thread_id"]:
        embed.add_field(name="Thread", value=f"<#{match['thread_id']}>", inline=False)
    embed.set_footer(text="Set a time yourself, give them longer, or dismiss this.")
    return embed


def bracket_embed(
    tournament: aiosqlite.Row,
    matches: Sequence[aiosqlite.Row],
    names: dict[int, str],
) -> discord.Embed:
    embed = discord.Embed(
        title=tournament["name"],
        url=_url(tournament),
        colour=ACCENT,
        description=f"**Bracket** · {tournament['state'].title()}",
    )
    by_round: dict[int, list[aiosqlite.Row]] = {}
    for match in matches:
        by_round.setdefault(int(match["round"]), []).append(match)

    if not by_round:
        embed.description = (
            "*No matches yet. The organiser has not started the bracket "
            "on Challonge.*"
        )
        return embed

    for number in sorted(by_round, key=lambda r: (r < 0, abs(r))):
        lines = []
        for match in by_round[number]:
            title = match_title(match, names)
            if match["state"] == "complete":
                winner = names.get(match["winner_id"], "?")
                score = f" `{match['scores']}`" if match["scores"] else ""
                lines.append(f"**{winner}**{score} *(beat {title})*")
            elif match["state"] == "open":
                bits = [title]
                if match["live"]:
                    bits.append("[Live]")
                if match["scheduled_at"]:
                    bits.append(discord_ts(_when(match["scheduled_at"]), "f"))
                lines.append(" - ".join(bits))
            else:
                lines.append(f"{title}")
        embed.add_field(
            name=round_label(number),
            value=_truncate(lines, 12, "matches")[:1024] or " ",
            inline=False,
        )
    return embed


def standings_embed(
    tournament: aiosqlite.Row, participants: Sequence[aiosqlite.Row]
) -> discord.Embed:
    ranked = [p for p in participants if p["final_rank"] is not None]
    source = ranked or participants

    lines = []
    for idx, row in enumerate(source, start=1):
        rank = row["final_rank"] if ranked else idx
        prefix = f"`#{rank:>2}`"
        who = f"<@{row['discord_user_id']}>" if row["discord_user_id"] else row["name"]
        lines.append(f"{prefix} {who}")

    embed = discord.Embed(
        title=tournament["name"],
        url=_url(tournament),
        colour=GOOD if ranked else ACCENT,
        description="**Standings**"
        + ("" if ranked else " *(final places appear once the bracket ends)*"),
    )
    embed.add_field(
        name=f"{len(source)} players",
        value=_truncate(lines, 30, "players") or "*Nobody yet.*",
        inline=False,
    )
    return embed


def upcoming_embed(
    tournament: aiosqlite.Row,
    open_matches: Sequence[aiosqlite.Row],
    names: dict[int, str],
) -> discord.Embed:
    scheduled = sorted(
        (m for m in open_matches if m["scheduled_at"]),
        key=lambda m: m["scheduled_at"],
    )
    waiting = len(open_matches) - len(scheduled)

    embed = discord.Embed(
        title=tournament["name"],
        url=_url(tournament),
        colour=ACCENT,
        description="**Upcoming matches**",
    )
    if scheduled:
        lines = [
            f"{discord_ts(_when(m['scheduled_at']), 'f')}\n"
            f" {'[Live] ' if m['live'] else ''}**{match_title(m, names)}**"
            + (f" · <#{m['thread_id']}>" if m["thread_id"] else "")
            for m in scheduled
        ]
        embed.add_field(
            name=f"{len(scheduled)} scheduled",
            value=_truncate(lines, 10, "matches"),
            inline=False,
        )
    else:
        embed.add_field(
            name="Nothing scheduled yet",
            value="Players agree times in their own match threads.",
            inline=False,
        )
    if waiting:
        embed.set_footer(text=f"{waiting} open match(es) still have no agreed time.")
    return embed


def next_match_embed(
    tournament: aiosqlite.Row,
    match: aiosqlite.Row | None,
    names: dict[int, str],
    *,
    on_bracket: bool,
    signed_up: bool,
    signup_name: str | None = None,
) -> discord.Embed:
    if not signed_up:
        return discord.Embed(
            title="You have not signed up",
            colour=MUTED,
            description=(
                "Press **Sign up** on the panel and tell the bot the name you "
                "play under. That is the only way it can know which matches "
                "are yours."
            ),
        )
    if not on_bracket:
        return discord.Embed(
            title="Waiting for the bracket",
            colour=WARN,
            description=(
                f"You are signed up as **{signup_name}**, but that name is not "
                "on the bracket yet.\n"
                "Ask an organiser to add it on Challonge. You will be linked "
                "automatically once they do, with nothing more to press."
            ),
        )
    if match is None:
        return discord.Embed(
            title="No open match",
            colour=MUTED,
            description=(
                "Nothing to play right now. Either you are waiting on someone "
                "else's result, or your run is over."
            ),
        )

    scheduled = _when(match["scheduled_at"])
    deadline = _when(match["deadline_at"])
    embed = discord.Embed(
        title=match_title(match, names),
        url=_url(tournament),
        colour=GOOD if scheduled else WARN,
        description=f"**Your next match** · {round_label(match['round'])}"
        + ("\n[Live] Featured" if match["live"] else ""),
    )
    if scheduled:
        embed.add_field(
            name="Agreed Time",
            value=f"{discord_ts(scheduled)}\n{discord_ts(scheduled, 'R')}",
            inline=True,
        )
    elif deadline:
        embed.add_field(
            name="Deadline",
            value=f"Agree one by {discord_ts(deadline, 'R')}",
            inline=True,
        )
    if match["thread_id"]:
        embed.add_field(name="Thread", value=f"<#{match['thread_id']}>", inline=True)
    return embed


def round_announcement_embed(
    tournament: aiosqlite.Row,
    matches: Sequence[aiosqlite.Row],
    names: dict[int, str],
) -> discord.Embed:
    """Build the round announcement embed."""
    rounds = sorted({int(m["round"]) for m in matches}, key=lambda r: (r < 0, abs(r)))
    heading = " and ".join(round_label(r) for r in rounds) or "Next round"

    embed = discord.Embed(
        title=f"Round: {heading}",
        url=_url(tournament),
        colour=ACCENT,
        description=(
            f"**{len(matches)}** match(es) are live. Every one has its own "
            "thread below - open yours and agree a time."
        ),
    )
    for group, rows in _by_round(matches):
        embed.add_field(
            name=group,
            value=_round_value(rows, names) or "*none*",
            inline=False,
        )
    embed.set_footer(
        text="Threads are public: anyone can follow along, only the two "
        "players and the organisers act."
    )
    return embed


def _by_round(
    matches: Sequence[aiosqlite.Row],
) -> list[tuple[str, list[aiosqlite.Row]]]:
    """Group matches by round number."""
    buckets: dict[int, list[aiosqlite.Row]] = {}
    for match in matches:
        buckets.setdefault(int(match["round"] or 0), []).append(match)
    order = sorted(buckets, key=lambda r: (r < 0, abs(r)))
    return [(round_label(r), buckets[r]) for r in order]


def _match_line(match: aiosqlite.Row, names: dict[int, str]) -> str:
    live = f"{LIVE_DOT} " if match["live"] else ""
    thread = f" · <#{match['thread_id']}>" if match["thread_id"] else " · *no thread*"
    return f"{live}**{match_title(match, names)}**{thread}"


def _round_value(rows: Sequence[aiosqlite.Row], names: dict[int, str]) -> str:
    """Format match lines for a round."""
    play_by = _when(_column(rows[0], "play_by")) if rows else None
    header = f"-# Play by {discord_ts(play_by, 'D')}\n" if play_by else ""
    return header + _truncate([_match_line(m, names) for m in rows], 12, "matches")


async def round_schedule_embed(
    bot, tournament: aiosqlite.Row, *, restamped: int = 0
) -> discord.Embed:
    """Build the round schedule embed."""
    from services import (
        MAX_TOURNAMENT_DAYS,
        end_of_day,
        guild_timezone,
        late_matches,
        round_plan,
    )
    from timeparse import get_zone

    tournament_id = int(tournament["challonge_id"])
    matches = await bot.store.list_matches(tournament_id)
    plan = await round_plan(bot, tournament)

    embed = discord.Embed(
        title=tournament["name"],
        url=_url(tournament),
        colour=ACCENT,
        description="**Round calendar**",
    )
    if plan is None:
        embed.colour = MUTED
        embed.add_field(
            name="No match days set",
            value=(
                "Matches are only bounded by the agree-a-time deadline, so the "
                "event runs as long as the players take."
            ),
            inline=False,
        )
        embed.add_field(
            name="Set one",
            value=(
                "`/tournament round schedule first_day:2026-09-12 "
                "days_per_round:7`\nRound one on the 12th, every later round a "
                "week after the one before.\n"
                "Uneven gaps: `round_days:0,3,7,10`, counted in days from the "
                "first match day.\n"
                "Every match then has to be played by the end of its round's "
                "day, and the bot says so in the thread.\n"
                f"A tournament runs **{MAX_TOURNAMENT_DAYS} days at the most**, "
                "so anything later is pulled back onto the last day."
            ),
            inline=False,
        )
        return embed

    zone = get_zone(await guild_timezone(bot, int(tournament["guild_id"])))
    counts: dict[int, list[aiosqlite.Row]] = {}
    for match in matches:
        counts.setdefault(int(match["round"] or 0), []).append(match)

    embed.add_field(
        name="First match day",
        value=discord_ts(end_of_day(plan.first_day, zone), "D"),
        inline=True,
    )
    embed.add_field(
        name="Rounds", value=str(len(plan.order)), inline=True
    )
    embed.add_field(
        name="Whole event",
        value=f"**{plan.total_days}** of {MAX_TOURNAMENT_DAYS} day(s)",
        inline=True,
    )

    lines = []
    for round_number in plan.order:
        day = plan.days[round_number]
        rows = counts.get(round_number, [])
        played = sum(1 for m in rows if m["state"] == "complete")
        detail = (
            f"{played}/{len(rows)} played"
            if rows
            else "not drawn yet"
        )
        lines.append(
            f"**{round_label(round_number)}** · "
            f"{discord_ts(end_of_day(day, zone), 'D')} · {detail}"
        )
    embed.add_field(
        name="Play by",
        value=_truncate(lines, 15, "rounds", max_chars=1000),
        inline=False,
    )
    embed.add_field(
        name="Ends",
        value=(
            f"{discord_ts(end_of_day(plan.last_day, zone))} "
            f"({discord_ts(end_of_day(plan.last_day, zone), 'R')})"
        ),
        inline=False,
    )

    if plan.clamped:
        embed.colour = WARN
        embed.add_field(
            name="Held to two weeks",
            value=(
                "These rounds fell past the "
                f"{MAX_TOURNAMENT_DAYS}-day limit and now share the last day: "
                + ", ".join(round_label(r) for r in plan.clamped)
                + ".\nShorten the gap between rounds to spread them out."
            ),
            inline=False,
        )

    late = late_matches(m for m in matches if m["state"] == "open")
    if late:
        embed.colour = WARN
        names = await bot.store.participant_display(tournament_id)
        embed.add_field(
            name=f"Agreed after their round ({len(late)})",
            value=_truncate(
                [
                    f"**{match_title(m, names)}** · "
                    f"{discord_ts(_when(m['scheduled_at']), 'f')}"
                    for m in late
                ],
                6,
                "matches",
            ),
            inline=False,
        )

    embed.set_footer(
        text=(
            f"{restamped} match(es) re-stamped with the new days. "
            if restamped
            else ""
        )
        + "Deadlines never run past the end of a round, and no tournament "
        f"runs longer than {MAX_TOURNAMENT_DAYS} days."
    )
    return embed


def _bar(done: int, total: int, width: int = 12) -> str:
    """Generate a plain-text progress bar."""
    if total <= 0:
        return "-" * width
    filled = max(0, min(width, round(width * done / total)))
    return "#" * filled + "-" * (width - filled)


def entrants_sync_embed(
    tournament: aiosqlite.Row,
    result,
    *,
    budget: BudgetStatus,
    thread_joins: int = 0,
) -> discord.Embed:
    """Build the entrants sync result embed."""
    total = len(result.participants)
    linked = result.linked_total

    embed = discord.Embed(
        title=tournament["name"],
        url=_url(tournament),
        colour=GOOD if linked == total and total else ACCENT,
        description="**Entrants synced**",
    )
    embed.add_field(name="On the bracket", value=str(total), inline=True)
    embed.add_field(
        name="Signed up here", value=str(len(result.signups)), inline=True
    )
    embed.add_field(
        name="Newly linked",
        value=str(result.linked_now) if result.linked_now else "none",
        inline=True,
    )
    embed.add_field(
        name="Linked to Discord",
        value=f"`{_bar(linked, total)}` **{linked}** of {total}",
        inline=False,
    )

    if result.linked_now:
        note = (
            f"**{result.linked_now}** sign-up(s) just matched their bracket name."
        )
        if thread_joins:
            note += f" Added to **{thread_joins}** match thread(s)."
        embed.add_field(name="Linked this sync", value=note, inline=False)

    if result.waiting:
        embed.add_field(
            name=f"Waiting for the bracket ({len(result.waiting)})",
            value=_truncate(
                [
                    f"<@{int(row['discord_user_id'])}> wants **{row['name']}**"
                    for row in result.waiting
                ],
                15,
                "players",
            )
            + "\n*Add those names on Challonge, then sync again.*",
            inline=False,
        )

    if result.bracket_only:
        embed.add_field(
            name=f"Challonge only ({len(result.bracket_only)})",
            value=_truncate(
                [row["name"] for row in result.bracket_only], 15, "players"
            )
            + "\n*Never pinged. Their matches still get a thread.*",
            inline=False,
        )

    embed.set_footer(
        text=f"Challonge requests this month: {budget.used}/{budget.limit}. "
        "Entrants only - use /tournament round start for the matches."
    )
    return embed


def round_start_embed(
    tournament: aiosqlite.Row,
    result,
    *,
    names: dict[int, str],
    budget: BudgetStatus,
) -> discord.Embed:
    """Build the round start result embed."""
    report = result.report
    created, existing, failed = report.created, report.existing, report.failed
    live = result.open_matches

    embed = discord.Embed(
        title=tournament["name"],
        url=_url(tournament),
        colour=BAD if (failed or report.error) else GOOD,
        description="**Round started**",
    )
    embed.add_field(name="Matches live", value=str(len(live)), inline=True)
    embed.add_field(name="Threads opened", value=str(len(created)), inline=True)
    embed.add_field(name="Already had one", value=str(len(existing)), inline=True)

    if result.opened or result.completed:
        embed.add_field(
            name="Bracket moved",
            value=(
                f"**{len(result.opened)}** newly open, "
                f"**{len(result.completed)}** newly completed."
            ),
            inline=False,
        )

    if live:
        for group, rows in _by_round(live):
            embed.add_field(
                name=group, value=_round_value(rows, names), inline=False
            )
    else:
        embed.add_field(
            name="Nothing open",
            value=(
                "No match is open on Challonge. Start the tournament there, "
                "or enter the outstanding results, then run this again."
            ),
            inline=False,
        )

    if report.invited or report.unreachable:
        value = f"**{report.invited}** player(s) added to their thread."
        if report.unreachable:
            unique = sorted(set(report.unreachable))
            value += (
                f"\n**{len(unique)}** not on Discord: "
                + _truncate(unique, 10, "players", max_chars=400)
                + "\nTheir threads exist all the same; nobody is pinged."
            )
        embed.add_field(name="Players", value=value, inline=False)

    if failed:
        embed.add_field(
            name=f"Could not open ({len(failed)})",
            value=_truncate(
                [
                    f"**{match_title(m, names)}** - {why}"
                    for m, why in failed
                ],
                6,
                "matches",
            ),
            inline=False,
        )
    if report.error:
        embed.add_field(name="Fix this first", value=report.error, inline=False)

    embed.set_footer(
        text=f"Challonge requests this month: {budget.used}/{budget.limit}. "
        "Threads are public; who may see them is a channel permission."
    )
    return embed


def board_embed(
    tournament: aiosqlite.Row,
    open_matches: Sequence[aiosqlite.Row],
    names: dict[int, str],
    *,
    budget: BudgetStatus,
    syncs_per_day: int,
    participant_count: int,
    unclaimed_count: int,
    signup_count: int,
    total_matches: int,
    completed_matches: int,
    threadless: int,
    schedule: str | None = None,
) -> discord.Embed:
    """Build the organiser board embed."""
    embed = discord.Embed(
        title=tournament["name"],
        url=_url(tournament),
        colour=ACCENT,
        description=f"**Organiser board** · {tournament['state'].title()}",
    )

    embed.add_field(name="Players", value=str(participant_count), inline=True)
    embed.add_field(
        name="Progress",
        value=f"{completed_matches}/{total_matches} played",
        inline=True,
    )
    embed.add_field(name="Open now", value=str(len(open_matches)), inline=True)
    if schedule:
        embed.add_field(name="Match days", value=schedule, inline=False)

    grouped: dict[str, list[aiosqlite.Row]] = {}
    for match in open_matches:
        grouped.setdefault(match["scheduling_status"] or "pending", []).append(match)

    for status in ("escalated", "pending", "proposed", "agreed", "handled"):
        rows = grouped.get(status)
        if not rows:
            continue
        lines = []
        for match in rows:
            title = match_title(match, names)
            thread = f" <#{match['thread_id']}>" if match["thread_id"] else ""
            live = "[Live] " if match["live"] else ""
            if match["scheduled_at"]:
                detail = discord_ts(_when(match["scheduled_at"]), "f")
            elif match["deadline_at"]:
                detail = f"due {discord_ts(_when(match['deadline_at']), 'R')}"
            else:
                detail = "no thread yet - /tournament round start"
            lines.append(f"{live}**{title}**{thread}\n {detail}")
        embed.add_field(
            name=f"{STATUS_LABEL[status]} ({len(rows)})",
            value=_truncate(lines, 8, "matches")[:1024],
            inline=False,
        )

    if not open_matches:
        embed.add_field(
            name="No open matches",
            value="Every match is finished or waiting on an earlier one.",
            inline=False,
        )

    if threadless or unclaimed_count:
        embed.add_field(
            name="Not reachable on Discord",
            value=(
                f"{signup_count} sign-up(s) here, {unclaimed_count} bracket "
                f"entr(ies) with nobody attached, {threadless} open match(es) "
                "with no thread. Nothing is wrong: those players arrange their "
                "matches the way they always did. `/tournament entrants` shows "
                "who is who."
            ),
            inline=False,
        )

    embed.add_field(
        name="Challonge requests this month",
        value=(
            f"`{budget.bar()}` **{budget.used}** of {budget.limit}\n"
            f"{budget.remaining} left. The bot only reads; everything you do "
            "on challonge.com is free."
        ),
        inline=False,
    )
    if budget.fraction >= 0.95:
        embed.colour = BAD
    elif budget.fraction >= 0.8:
        embed.colour = WARN
    return embed


def command_help_embed() -> discord.Embed:
    embed = discord.Embed(
        title="How this bot works",
        colour=ACCENT,
        description=(
            "The bracket lives on challonge.com. Admins build it, seed it, "
            "start it and enter results there. The bot reads it and runs the "
            "Discord side."
        ),
    )
    embed.add_field(
        name="If you are playing",
        value=(
            "**Sign up** on the panel and give the name you play under on the "
            "bracket. That is the only thing you ever have to do.\n"
            "**Bracket** and **My match** show where things stand.\n"
            "When your match opens you are added to its thread. **Propose "
            "time**, your opponent accepts or counters, and you both get "
            "reminded before it starts. Threads are public, so the rest of "
            "the server can follow the match.\n"
            "If the organisers set match days, your thread says which day your "
            "round has to be played by, and you cannot book past it."
        ),
        inline=False,
    )
    embed.add_field(
        name="If you are a tournament admin",
        value=(
            "`/tournament post` panel for a bracket (3 requests)\n"
            "`/tournament sync entrants` re-read the entrant list (1 request)\n"
            "`/tournament round start` read the matches, open every thread, "
            "invite the players (1 request)\n"
            "`/tournament round schedule` set the match days, and see how long "
            "the whole event will take\n"
            "`/tournament settings` timezone, deadline, admin role, featured venue\n"
            "`/tournament board` progress and who is stalling\n"
            "`/tournament entrants` who signed up, who matched\n"
            "`/tournament repost` put the panel up again"
        ),
        inline=False,
    )
    embed.add_field(
        name="Not on Discord?",
        value=(
            "Fine. Players who never sign up here are still on the bracket, "
            "are shown by name, and are never pinged. Their matches get "
            "arranged the usual way."
        ),
        inline=False,
    )
    embed.set_footer(
        text="The bot uses the monthly Challonge request pool for reads. "
        "Entering results on challonge.com is always free."
    )
    return embed
