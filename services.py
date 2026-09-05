"""Shared tournament management and sync services."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from typing import Iterable, Sequence

import aiosqlite
import discord

from db.store import _parse_iso
from timeparse import get_zone

log = logging.getLogger(__name__)

REMINDER_OFFSETS: tuple[tuple[str, timedelta], ...] = (
    ("1h", timedelta(hours=1)),
    ("5m", timedelta(minutes=5)),
)

DEADLINE_NUDGES: tuple[tuple[str, float], ...] = (
    ("sched_nudge_1", 0.5),
    ("sched_nudge_2", 0.833),
    ("sched_deadline", 1.0),
)
DEADLINE_KINDS = tuple(kind for kind, _ in DEADLINE_NUDGES)

MAX_REFRESHES_PER_HOUR = 3
DEFAULT_SYNCS_PER_DAY = 12
PROBE_OFFSETS_MINUTES = (10, 30, 90, 150, 210)
THREAD_ACTIVITY_DELAY = timedelta(minutes=2)
IDLE_BACKSTOP = timedelta(hours=12)


@dataclass(frozen=True)
class RefreshDecision:
    """Refresh rate-limiting decision."""

    allowed: bool
    reason: str
    window_start: datetime
    window_count: int
    day: str
    day_count: int
    retry_at: datetime | None = None


def evaluate_refresh(
    now: datetime,
    *,
    window_start: datetime | None,
    window_count: int,
    day: str | None = None,
    day_count: int = 0,
    max_per_hour: int = MAX_REFRESHES_PER_HOUR,
    max_per_day: int = DEFAULT_SYNCS_PER_DAY,
) -> RefreshDecision:
    """Decide whether a tournament may be refreshed at now."""
    today = now.strftime("%Y-%m-%d")
    used_today = day_count if day == today else 0

    if max_per_day <= 0:
        return RefreshDecision(
            False,
            "automatic syncing is off; an admin syncs by hand",
            window_start or now,
            window_count,
            today,
            used_today,
        )

    if used_today >= max_per_day:
        midnight = (now + timedelta(days=1)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        return RefreshDecision(
            False,
            f"daily allowance spent ({used_today}/{max_per_day})",
            window_start or now,
            window_count,
            today,
            used_today,
            retry_at=midnight,
        )

    if window_start is None or now - window_start >= timedelta(hours=1):
        return RefreshDecision(
            True, "new window", now, 1, today, used_today + 1
        )

    if window_count < max_per_hour:
        return RefreshDecision(
            True,
            "within window",
            window_start,
            window_count + 1,
            today,
            used_today + 1,
        )

    return RefreshDecision(
        False,
        f"rate limited ({window_count}/{max_per_hour} this hour)",
        window_start,
        window_count,
        today,
        used_today,
        retry_at=window_start + timedelta(hours=1),
    )


async def sync_allowance(bot, guild_id: int) -> int:
    config = await guild_config(bot, guild_id)
    if config is None:
        return DEFAULT_SYNCS_PER_DAY
    try:
        if not int(config["auto_sync"]):
            return 0
        return int(config["syncs_per_day"])
    except (TypeError, ValueError, KeyError):
        return DEFAULT_SYNCS_PER_DAY


def next_probe_at(scheduled_at: datetime, now: datetime) -> datetime | None:
    for minutes in PROBE_OFFSETS_MINUTES:
        moment = scheduled_at + timedelta(minutes=minutes)
        if moment > now:
            return moment
    return None


def plan_next_refresh(
    now: datetime,
    *,
    open_matches: list[aiosqlite.Row],
    has_open_matches: bool | None = None,
) -> tuple[datetime | None, str]:
    candidates: list[tuple[datetime, str]] = []

    for match in open_matches:
        scheduled = _parse_iso(match["scheduled_at"])
        if scheduled is None:
            continue
        probe = next_probe_at(scheduled, now)
        if probe is not None:
            candidates.append((probe, f"match {match['identifier'] or match['match_id']}"))

    any_open = has_open_matches if has_open_matches is not None else bool(open_matches)
    if any_open:
        candidates.append((now + IDLE_BACKSTOP, "idle backstop"))

    if not candidates:
        return None, "nothing open"
    return min(candidates, key=lambda pair: pair[0])


async def refresh_matches(bot, tournament: aiosqlite.Row, *, reason: str):
    """Read the bracket from Challonge and update local database cache."""
    tournament_id = int(tournament["challonge_id"])

    before = {
        int(row["match_id"]): row["state"]
        for row in await bot.store.list_matches(tournament_id)
    }
    matches = await bot.challonge.list_matches(tournament_id, reason=reason)
    await bot.store.upsert_matches(tournament_id, matches)
    await ensure_participants(bot, tournament_id, matches, reason=reason)

    opened_ids = [m.id for m in matches if m.is_open and before.get(m.id) != "open"]
    completed_ids = [
        m.id for m in matches if m.is_complete and before.get(m.id) != "complete"
    ]

    after = {
        int(row["match_id"]): row
        for row in await bot.store.list_matches(tournament_id)
    }
    opened = [after[i] for i in opened_ids if i in after]
    completed = [after[i] for i in completed_ids if i in after]

    for row in completed:
        await bot.store.clear_reminders(tournament_id, int(row["match_id"]))
        if row["event_id"]:
            await cancel_event(bot, tournament, row)

    if opened:
        bot.dispatch("matches_opened", tournament, opened)
    for row in completed:
        bot.dispatch("match_completed", tournament, row)

    await check_finished(bot, tournament, matches)
    return opened, completed


async def ensure_participants(
    bot, tournament_id: int, matches, *, reason: str
) -> None:
    known = set(await bot.store.participant_names(tournament_id))
    referenced = {
        int(pid)
        for match in matches
        for pid in (match.player1_id, match.player2_id)
        if pid is not None
    }
    if not referenced - known:
        return

    log.info("bracket mentions unknown players; refreshing the participant list")
    await bot.store.replace_participants(
        tournament_id,
        await bot.challonge.list_participants(tournament_id, reason=reason),
    )
    await bot.store.link_signups_to_bracket(tournament_id)


async def check_finished(bot, tournament: aiosqlite.Row, matches) -> None:
    """When every match is done, fetch final places once and announce them."""
    tournament_id = int(tournament["challonge_id"])
    if not matches or any(not m.is_complete for m in matches):
        return
    if tournament["state"] == "complete":
        return  # Already handled.

    await bot.store.set_tournament_state(tournament_id, "complete")
    await bot.store.replace_participants(
        tournament_id,
        await bot.challonge.list_participants(tournament_id, reason="standings"),
    )
    await bot.store.link_signups_to_bracket(tournament_id)
    bot.dispatch(
        "tournament_completed", await bot.store.get_tournament(tournament_id)
    )


MAX_TOURNAMENT_DAYS = 14
MAX_ROUND_DAYS = MAX_TOURNAMENT_DAYS - 1


def round_sequence(rounds: Iterable[int]) -> list[int]:
    """Order round numbers as played (interleaving losers rounds)."""
    return sorted({int(r) for r in rounds}, key=lambda r: (abs(r), r < 0))


def parse_round_days(text: str | None) -> list[int]:
    """Parse comma-separated day offsets."""
    if not text or not text.strip():
        return []
    offsets: list[int] = []
    for chunk in text.replace(" ", "").split(","):
        if not chunk:
            continue
        value = int(chunk)
        if not 0 <= value <= MAX_ROUND_DAYS:
            raise ValueError(f"{value} is not between 0 and {MAX_ROUND_DAYS}")
        offsets.append(value)
    if offsets != sorted(offsets):
        raise ValueError("the days have to go forwards, not backwards")
    return offsets


def round_offsets(
    count: int, *, days_per_round: int = 0, custom: Sequence[int] = ()
) -> list[int]:
    """Calculate day offsets for each round, clamped to MAX_ROUND_DAYS."""
    return [
        min(max(offset, 0), MAX_ROUND_DAYS)
        for offset in round_offsets_unclamped(
            count, days_per_round=days_per_round, custom=custom
        )
    ]


@dataclass(frozen=True)
class RoundPlan:
    first_day: date
    order: list[int]
    days: dict[int, date]
    clamped: list[int] = field(default_factory=list)

    @property
    def last_day(self) -> date:
        return max(self.days.values()) if self.days else self.first_day

    @property
    def total_days(self) -> int:
        return (self.last_day - self.first_day).days + 1

    def day_for(self, round_number: int) -> date | None:
        return self.days.get(int(round_number))


def parse_day(text: str) -> date:
    return date.fromisoformat(text.strip())


def build_round_plan(
    rounds: Iterable[int],
    *,
    first_day: date,
    days_per_round: int = 0,
    custom: Sequence[int] = (),
) -> RoundPlan:
    order = round_sequence(rounds)
    offsets = round_offsets(
        len(order), days_per_round=days_per_round, custom=custom
    )
    wanted = round_offsets_unclamped(
        len(order), days_per_round=days_per_round, custom=custom
    )
    return RoundPlan(
        first_day=first_day,
        order=order,
        days={r: first_day + timedelta(days=o) for r, o in zip(order, offsets)},
        clamped=[
            r for r, want, got in zip(order, wanted, offsets) if want != got
        ],
    )


def round_offsets_unclamped(
    count: int, *, days_per_round: int = 0, custom: Sequence[int] = ()
) -> list[int]:
    if count <= 0:
        return []
    if custom:
        offsets = list(custom[:count])
        gap = (custom[-1] - custom[-2]) if len(custom) >= 2 else max(days_per_round, 0)
        while len(offsets) < count:
            offsets.append(offsets[-1] + gap)
        return offsets
    step = max(int(days_per_round or 0), 0)
    return [i * step for i in range(count)]


def end_of_day(day: date, tz) -> datetime:
    return datetime.combine(
        day, time(23, 59), tzinfo=tz
    ).astimezone(timezone.utc)


async def round_plan(bot, tournament: aiosqlite.Row) -> RoundPlan | None:
    """The calendar for this bracket, or None when nobody set one."""
    config = await guild_config(bot, int(tournament["guild_id"]))
    if config is None:
        return None
    try:
        raw = config["first_match_day"]
    except (KeyError, IndexError):
        return None
    if not raw:
        return None

    try:
        first_day = parse_day(str(raw))
        custom = parse_round_days(config["round_days"])
    except (ValueError, KeyError, IndexError):
        log.warning("guild %s has an unreadable round plan", tournament["guild_id"])
        return None

    matches = await bot.store.list_matches(int(tournament["challonge_id"]))
    rounds = [int(m["round"] or 0) for m in matches]
    if not rounds:
        rounds = [1]
    try:
        days_per_round = int(config["days_per_round"] or 0)
    except (TypeError, ValueError, KeyError, IndexError):
        days_per_round = 0
    return build_round_plan(
        rounds,
        first_day=first_day,
        days_per_round=days_per_round,
        custom=custom,
    )


async def play_by_for(
    bot, tournament: aiosqlite.Row, match: aiosqlite.Row, plan: RoundPlan | None = None
) -> datetime | None:
    """Return the play-by deadline for a match."""
    plan = plan if plan is not None else await round_plan(bot, tournament)
    if plan is None:
        return None
    day = plan.day_for(int(match["round"] or 0))
    if day is None:
        return None
    zone = get_zone(await guild_timezone(bot, int(tournament["guild_id"])))
    return end_of_day(day, zone)


async def apply_round_plan(bot, tournament: aiosqlite.Row) -> int:
    """Update play-by timestamps for unplayed matches."""
    tournament_id = int(tournament["challonge_id"])
    plan = await round_plan(bot, tournament)
    touched = 0
    for match in await bot.store.list_matches(tournament_id):
        if match["state"] == "complete":
            continue
        play_by = await play_by_for(bot, tournament, match, plan)
        await bot.store.set_match_play_by(
            tournament_id, int(match["match_id"]), play_by
        )
        touched += 1
        if match["state"] == "open" and not match["scheduled_at"]:
            await start_scheduling_window(
                bot, tournament, match, plan=plan, restart=True
            )
    return touched


def late_matches(matches: Iterable[aiosqlite.Row]) -> list[aiosqlite.Row]:
    """Return open matches scheduled after their round play-by date."""
    late = []
    for match in matches:
        play_by = _parse_iso(match["play_by"]) if "play_by" in match.keys() else None
        scheduled = _parse_iso(match["scheduled_at"])
        if play_by and scheduled and scheduled > play_by:
            late.append(match)
    return late


async def guild_config(bot, guild_id: int) -> aiosqlite.Row | None:
    return await bot.store.get_guild_config(guild_id)


async def guild_timezone(bot, guild_id: int) -> str:
    config = await guild_config(bot, guild_id)
    return (config["timezone"] if config else None) or "UTC"


async def deadline_hours(bot, guild_id: int) -> int:
    config = await guild_config(bot, guild_id)
    try:
        return int(config["deadline_hours"]) if config else 24
    except (TypeError, ValueError):
        return 24


async def start_scheduling_window(
    bot,
    tournament: aiosqlite.Row,
    match: aiosqlite.Row,
    *,
    plan: RoundPlan | None = None,
    restart: bool = False,
) -> datetime | None:
    """Initialize match play-by and scheduling deadlines."""
    hours = await deadline_hours(bot, int(tournament["guild_id"]))
    tournament_id = int(tournament["challonge_id"])
    match_id = int(match["match_id"])
    now = datetime.now(timezone.utc)

    play_by = await play_by_for(bot, tournament, match, plan)
    await bot.store.set_match_play_by(tournament_id, match_id, play_by)

    deadline = now + timedelta(hours=hours) if hours > 0 else None
    if play_by is not None and (deadline is None or play_by < deadline):
        deadline = play_by

    if deadline is None:
        await bot.store.set_match_deadline(tournament_id, match_id, None)
        if restart:
            await clear_deadline_reminders(bot, tournament_id, match_id)
        return None

    if deadline <= now:
        await bot.store.set_match_deadline(tournament_id, match_id, deadline)
        if restart:
            await clear_deadline_reminders(bot, tournament_id, match_id)
        await bot.store.schedule_reminder(
            tournament_id, match_id, "sched_deadline", now
        )
        return deadline

    await bot.store.set_match_deadline(tournament_id, match_id, deadline)
    await bot.store.set_scheduling_status(tournament_id, match_id, "pending")

    if restart:
        await clear_deadline_reminders(bot, tournament_id, match_id)
    window = deadline - now
    for kind, fraction in DEADLINE_NUDGES:
        fire_at = now + window * fraction
        if fire_at > now:
            await bot.store.schedule_reminder(tournament_id, match_id, kind, fire_at)
    return deadline


async def clear_deadline_reminders(
    bot, tournament_id: int, match_id: int
) -> None:
    for kind in DEADLINE_KINDS:
        await bot.store.cancel_reminder(tournament_id, match_id, kind)


async def propose_time(
    bot,
    tournament: aiosqlite.Row,
    match: aiosqlite.Row,
    *,
    proposer_id: int,
    responder_id: int | None,
    when: datetime,
) -> int:
    tournament_id = int(tournament["challonge_id"])
    match_id = int(match["match_id"])
    proposal_id = await bot.store.create_proposal(
        tournament_id,
        match_id,
        proposer_id=proposer_id,
        responder_id=responder_id,
        proposed_at=when,
    )
    await bot.store.set_scheduling_status(tournament_id, match_id, "proposed")
    return proposal_id


async def accept_proposal(
    bot,
    tournament: aiosqlite.Row,
    match: aiosqlite.Row,
    proposal: aiosqlite.Row,
) -> datetime:
    tournament_id = int(tournament["challonge_id"])
    match_id = int(match["match_id"])
    when = _parse_iso(proposal["proposed_at"])
    if when is None:
        raise ValueError("proposal has no time")

    await bot.store.set_proposal_status(int(proposal["id"]), "accepted")
    await bot.store.confirm_match_time(tournament_id, match_id, when)
    await clear_deadline_reminders(bot, tournament_id, match_id)
    await queue_match_reminders(bot, tournament_id, match_id, when)

    refreshed = await bot.store.get_match(tournament_id, match_id)
    if refreshed is not None:
        await sync_event(bot, tournament, refreshed)
        probe = next_probe_at(when, datetime.now(timezone.utc))
        if probe is not None:
            await bot.store.request_refresh_no_later_than(tournament_id, probe)
    return when


async def force_time(
    bot, tournament: aiosqlite.Row, match: aiosqlite.Row, when: datetime
) -> None:
    """Set match time directly without handshake."""
    tournament_id = int(tournament["challonge_id"])
    match_id = int(match["match_id"])

    await bot.store.supersede_proposals(tournament_id, match_id)
    await bot.store.confirm_match_time(tournament_id, match_id, when)
    await clear_deadline_reminders(bot, tournament_id, match_id)
    await queue_match_reminders(bot, tournament_id, match_id, when)

    refreshed = await bot.store.get_match(tournament_id, match_id)
    if refreshed is not None:
        await sync_event(bot, tournament, refreshed)


async def queue_match_reminders(
    bot, tournament_id: int, match_id: int, when: datetime
) -> None:
    """Queue reminders before the match."""
    now = datetime.now(timezone.utc)
    for kind, offset in REMINDER_OFFSETS:
        fire_at = when - offset
        if fire_at > now:
            await bot.store.schedule_reminder(tournament_id, match_id, kind, fire_at)
        else:
            await bot.store.cancel_reminder(tournament_id, match_id, kind)


async def extend_deadline(
    bot, tournament: aiosqlite.Row, match: aiosqlite.Row, hours: int
) -> datetime:
    """Extend match deadline and reschedule reminders."""
    tournament_id = int(tournament["challonge_id"])
    match_id = int(match["match_id"])
    now = datetime.now(timezone.utc)
    deadline = now + timedelta(hours=hours)

    await bot.store.set_match_deadline(tournament_id, match_id, deadline)
    await bot.store.set_scheduling_status(tournament_id, match_id, "pending")
    await clear_deadline_reminders(bot, tournament_id, match_id)
    for kind, fraction in DEADLINE_NUDGES:
        fire_at = now + timedelta(hours=hours * fraction)
        if fire_at > now:
            await bot.store.schedule_reminder(tournament_id, match_id, kind, fire_at)
    return deadline


async def sync_event(bot, tournament: aiosqlite.Row, match: aiosqlite.Row) -> None:
    """Create, update, or remove Discord scheduled event for a live match."""
    tournament_id = int(tournament["challonge_id"])
    match_id = int(match["match_id"])
    when = _parse_iso(match["scheduled_at"])

    wants_event = bool(match["live"]) and when is not None and match["state"] == "open"
    if not wants_event:
        if match["event_id"]:
            await cancel_event(bot, tournament, match)
        return

    guild = bot.get_guild(int(tournament["guild_id"]))
    if guild is None:
        return

    config = await guild_config(bot, int(tournament["guild_id"]))
    duration = int(config["event_duration"]) if config else 60
    names = await bot.store.participant_names(tournament_id)
    p1 = names.get(match["player1_id"], "TBD")
    p2 = names.get(match["player2_id"], "TBD")

    name = f"{p1} vs {p2}, {tournament['name']}"[:100]
    description = (
        f"Match `{match['identifier'] or match_id}`.\n"
        f"Bracket: {tournament['full_url'] or 'n/a'}"
    )[:1000]
    end_time = when + timedelta(minutes=duration)

    kwargs: dict = {
        "name": name,
        "start_time": when,
        "description": description,
        "privacy_level": discord.PrivacyLevel.guild_only,
    }
    channel_id = config["event_channel_id"] if config else None
    channel = guild.get_channel(int(channel_id)) if channel_id else None
    if isinstance(channel, (discord.VoiceChannel, discord.StageChannel)):
        kwargs["channel"] = channel
        kwargs["entity_type"] = (
            discord.EntityType.stage_instance
            if isinstance(channel, discord.StageChannel)
            else discord.EntityType.voice
        )
    else:
        kwargs["entity_type"] = discord.EntityType.external
        kwargs["location"] = (
            (config["event_location"] if config else None) or "See the match thread"
        )
        kwargs["end_time"] = end_time

    existing = await _fetch_event(guild, match["event_id"])
    try:
        if existing is not None:
            await existing.edit(**kwargs)
        else:
            created = await guild.create_scheduled_event(**kwargs)
            await bot.store.set_match_event(tournament_id, match_id, created.id)
    except discord.Forbidden:
        log.warning("missing Manage Events in guild %s", guild.id)
    except discord.HTTPException:
        log.exception("could not sync scheduled event for match %s", match_id)


async def cancel_event(bot, tournament: aiosqlite.Row, match: aiosqlite.Row) -> None:
    guild = bot.get_guild(int(tournament["guild_id"]))
    event = await _fetch_event(guild, match["event_id"]) if guild else None
    if event is not None:
        try:
            await event.delete()
        except discord.HTTPException:
            log.warning("could not delete scheduled event %s", match["event_id"])
    await bot.store.set_match_event(
        int(tournament["challonge_id"]), int(match["match_id"]), None
    )


async def _fetch_event(guild, event_id) -> discord.ScheduledEvent | None:
    if guild is None or not event_id:
        return None
    event = guild.get_scheduled_event(int(event_id))
    if event is not None:
        return event
    try:
        return await guild.fetch_scheduled_event(int(event_id))
    except discord.HTTPException:
        return None


async def open_room(bot, tournament: aiosqlite.Row, match: aiosqlite.Row) -> str:
    """Reserve a Null Rush room for a match and record its code."""
    tournament_id = int(tournament["challonge_id"])
    match_id = int(match["match_id"])

    if match["room_code"]:
        return str(match["room_code"])

    names = await bot.store.participant_names(tournament_id)
    room = await bot.relay.create_match(
        match_id=str(match_id),
        tournament=str(tournament_id),
        players=[
            names.get(match["player1_id"], "Player 1"),
            names.get(match["player2_id"], "Player 2"),
        ],
    )
    await bot.store.set_room_code(tournament_id, match_id, room.code)
    return room.code


async def push_result_to_challonge(
    bot, tournament: aiosqlite.Row, match: aiosqlite.Row, *, winner_id: int, scores: str
) -> None:
    """Report match result to Challonge and refresh tournament state."""
    tournament_id = int(tournament["challonge_id"])
    await bot.challonge.report_match(
        tournament_id,
        int(match["match_id"]),
        winner_id=winner_id,
        scores_csv=scores,
        reason="admin:report",
    )
    await refresh_matches(bot, tournament, reason="admin:report")


async def warn_budget_if_needed(bot, channel) -> None:
    """Tell the organisers once per month when usage crosses the warn line."""
    status = await bot.budget.take_warning()
    if status is None or channel is None:
        return
    try:
        await channel.send(
            f"**Heads up:** Challonge API usage is at **{status.used}/{status.limit}** "
            "requests this month. Above the cap Challonge returns 429s until "
            "the plan is upgraded. Entering results on challonge.com never "
            "counts against this, only bracket syncs do."
        )
    except Exception:
        log.exception("could not deliver budget warning")


@dataclass
class EntrantSync:
    """The outcome of reading the entrant list off Challonge."""

    participants: list
    signups: list
    linked_now: int
    linked_total: int
    waiting: list
    bracket_only: list


async def sync_entrants(
    bot, tournament: aiosqlite.Row, *, reason: str = "admin:entrants"
) -> EntrantSync:
    """Sync tournament participants from Challonge and match to Discord sign-ups."""
    tournament_id = int(tournament["challonge_id"])
    participants = await bot.challonge.list_participants(
        tournament_id, reason=reason
    )
    await bot.store.replace_participants(tournament_id, participants)
    linked_now = await bot.store.link_signups_to_bracket(tournament_id)

    rows = await bot.store.list_participants(tournament_id)
    signups = await bot.store.list_signups(tournament_id)
    bracket_names = {row["name"].casefold() for row in rows}

    return EntrantSync(
        participants=rows,
        signups=signups,
        linked_now=linked_now,
        linked_total=sum(1 for row in rows if row["discord_user_id"]),
        waiting=[s for s in signups if s["name"].casefold() not in bracket_names],
        bracket_only=[row for row in rows if not row["discord_user_id"]],
    )


@dataclass
class RoundStart:
    opened: list
    completed: list
    report: object
    open_matches: list


async def start_round(
    bot,
    tournament: aiosqlite.Row,
    *,
    announce: bool = True,
    reason: str = "admin:round-start",
):
    """Refresh tournament bracket and create threads for open matches."""
    from cogs.threads import ThreadReport

    tournament_id = int(tournament["challonge_id"])
    cog = bot.get_cog("ThreadsCog")
    if cog is None:
        opened, completed = await refresh_matches(bot, tournament, reason=reason)
        fresh = await bot.store.get_tournament(tournament_id) or tournament
        return RoundStart(
            opened=opened,
            completed=completed,
            report=ThreadReport(error="The thread manager is not loaded."),
            open_matches=await bot.store.list_matches(tournament_id, state="open"),
        )

    with cog.manual(tournament_id):
        opened, completed = await refresh_matches(bot, tournament, reason=reason)
        if opened and tournament["state"] == "pending":
            await bot.store.set_tournament_state(tournament_id, "underway")

        fresh = await bot.store.get_tournament(tournament_id) or tournament
        report = await cog.open_threads(fresh, announce=False)

    open_matches = await bot.store.list_matches(tournament_id, state="open")
    if announce and (report.created or opened) and report.channel is not None:
        names = await bot.store.participant_display(tournament_id)
        await cog.announce_round(report.channel, fresh, open_matches, names)

    return RoundStart(
        opened=opened,
        completed=completed,
        report=report,
        open_matches=open_matches,
    )
