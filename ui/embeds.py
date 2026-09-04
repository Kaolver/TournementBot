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


def _truncate(lines: Sequence[str], limit: int, noun: str, max_chars: int = 1000) -> str:
    """Join lines, replacing the tail with a count once they stop fitting."""
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


# ----------------------------------------------------------------- the panel


async def panel_embed(bot, tournament: aiosqlite.Row) -> discord.Embed:
    """The one message players interact with, posted by /tournament post."""
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
    """Admin view of the two lists and how well they line up."""
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
    """Shown by /tournament settings: what is configured, what is missing."""
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
        "Create Private Threads": me.guild_permissions.create_private_threads,
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


# ------------------------------------------------------------ match thread


def match_thread_embed(
    tournament: aiosqlite.Row, match: aiosqlite.Row, names: dict[int, str]
) -> discord.Embed:
    is_live = bool(match["live"])
    scheduled = _when(match["scheduled_at"])
    deadline = _when(match["deadline_at"])

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
        text="Only you two and the organisers can see this thread."
        + (" Featured matches get a server event." if is_live else "")
    )
    return embed


def room_embed(
    match: aiosqlite.Row, names: dict[int, str], code: str
) -> discord.Embed:
    """The room code, big enough to read off a second monitor."""
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
    """What Null Rush said happened. Not yet on the bracket."""
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


# ------------------------------------------------------------ public views


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
    rounds = sorted({int(m["round"]) for m in matches}, key=lambda r: (r < 0, abs(r)))
    heading = " and ".join(round_label(r) for r in rounds)

    embed = discord.Embed(
        title=f"Round: {heading}",
        url=_url(tournament),
        colour=ACCENT,
        description=f"{len(matches)} match(es) just opened.",
    )
    embed.add_field(
        name="Matches",
        value=_truncate(
            [
                f"{'[Live] ' if m['live'] else ''}**{match_title(m, names)}**"
                + (f" · <#{m['thread_id']}>" if m["thread_id"] else "")
                for m in matches
            ],
            15,
            "matches",
        ),
        inline=False,
    )
    embed.set_footer(text="Players: open your thread and agree a time.")
    return embed


# ----------------------------------------------------------------- board


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
) -> discord.Embed:
    """The organiser board: progress, who is stalling, and quota left."""
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
                detail = "no thread, players not linked"
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
            "When your match opens you get a private thread with your "
            "opponent. **Propose time**, they accept or counter, and you both "
            "get reminded before it starts."
        ),
        inline=False,
    )
    embed.add_field(
        name="If you are a tournament admin",
        value=(
            "`/tournament post` panel for a bracket (3 requests)\n"
            "`/tournament sync` read it right now (1 request)\n"
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
