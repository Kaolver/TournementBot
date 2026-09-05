"""Offline end-to-end self-test suite."""

from __future__ import annotations

import asyncio
import json
import re
import sqlite3
import sys
import tempfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import discord
import httpx

from challonge.budget import (
    ADMIN_REASONS,
    REASONS,
    Budget,
    BudgetExhausted,
    UnknownReason,
)
from challonge.client import ChallongeClient, NotFound, slug_from
from db.dialect import rowcount_from_status, to_postgres
from nullrush import RelayClient, RelayError
from db.store import Store
from db.supabase import (
    SupabaseBackend,
    SupabaseError,
    normalise_url,
)
from services import (
    DEADLINE_KINDS,
    MAX_ROUND_DAYS,
    MAX_TOURNAMENT_DAYS,
    accept_proposal,
    build_round_plan,
    end_of_day,
    parse_round_days,
    round_offsets,
    round_sequence,
    evaluate_refresh,
    extend_deadline,
    force_time,
    next_probe_at,
    open_room,
    plan_next_refresh,
    propose_time,
    refresh_matches,
    start_scheduling_window,
    sync_event,
)
from timeparse import TimeParseError, parse_when

PASSED = 0
FAILED = 0


def check(label: str, condition: bool, detail: str = "") -> None:
    global PASSED, FAILED
    if condition:
        PASSED += 1
        print(f"  ok   {label}")
    else:
        FAILED += 1
        print(f"  FAIL {label} {detail}")


# --------------------------------------------------------------- fake server


class FakeChallonge:
    """Mock Challonge server simulating bracket progression."""

    def __init__(self) -> None:
        self.tournament = {
            "id": "9001",
            "type": "tournament",
            "attributes": {
                "name": "Selftest Cup",
                "url": "selftest_cup",
                "state": "pending",
                "tournament_type": "single elimination",
                "full_challonge_url": "https://challonge.com/selftest_cup",
            },
        }
        self.participants: list[dict] = []
        self.matches: list[dict] = []
        self.requests: list[tuple[str, str]] = []
        # Requests made by scenarios that are not part of a normal event (a
        # deliberate 404, a player appearing mid-bracket) are booked separately
        # so the headline cost figure stays honest.
        self.diagnostics: list[tuple[str, str]] = []
        self.diagnostic_mode = False

    # -- website-side actions, free ----------------------------------------

    def add_players(self, names: list[str]) -> None:
        for name in names:
            self.participants.append(
                {
                    "id": str(100 + len(self.participants)),
                    "type": "participant",
                    "attributes": {
                        "name": name,
                        "seed": len(self.participants) + 1,
                        "misc": None,
                        "active": True,
                        "final_rank": None,
                    },
                }
            )

    def _match(self, mid: int, rnd: int, p1, p2, state: str) -> dict:
        return {
            "id": str(mid),
            "type": "match",
            "attributes": {
                "state": state,
                "round": rnd,
                "identifier": chr(64 + mid),
                "suggested_play_order": mid,
                "scores": None,
                "winner_id": None,
            },
            "relationships": {
                "player1": {"data": {"id": str(p1), "type": "participant"}}
                if p1
                else {"data": None},
                "player2": {"data": {"id": str(p2), "type": "participant"}}
                if p2
                else {"data": None},
            },
        }

    def start(self) -> None:
        ids = [int(p["id"]) for p in self.participants]
        self.matches = [
            self._match(1, 1, ids[0], ids[1], "open"),
            self._match(2, 1, ids[2], ids[3], "open"),
            self._match(3, 2, None, None, "pending"),
        ]
        self.tournament["attributes"]["state"] = "underway"

    def report(self, match_id: int, scores: str, winner_id: int) -> None:
        match = next(m for m in self.matches if int(m["id"]) == match_id)
        match["attributes"].update(
            {"state": "complete", "scores": scores, "winner_id": winner_id}
        )
        if match_id == 3:
            self.tournament["attributes"]["state"] = "complete"
            for rank, participant in enumerate(self.participants, start=1):
                participant["attributes"]["final_rank"] = rank
            return

        final = self.matches[2]
        slot = "player1" if match_id == 1 else "player2"
        final["relationships"][slot] = {
            "data": {"id": str(winner_id), "type": "participant"}
        }
        if all(final["relationships"][s].get("data") for s in ("player1", "player2")):
            final["attributes"]["state"] = "open"

    # -- the only three endpoints the bot uses ------------------------------

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        target = self.diagnostics if self.diagnostic_mode else self.requests
        target.append((request.method, path))

        # Challonge accepts either the numeric id or the url slug.
        identifiers = (
            self.tournament["id"],
            self.tournament["attributes"]["url"],
        )
        if any(path.endswith(f"/tournaments/{ref}.json") for ref in identifiers):
            return httpx.Response(200, json={"data": self.tournament})
        if path.endswith("/participants.json"):
            return httpx.Response(200, json={"data": self.participants})
        if path.endswith("/matches.json"):
            return httpx.Response(200, json={"data": self.matches})

        return httpx.Response(404, json={"errors": [{"detail": f"no route {path}"}]})


class FakeEvent:
    """Stands in for discord.ScheduledEvent."""

    def __init__(self, event_id: int, **kwargs) -> None:
        self.id = event_id
        self.kwargs = kwargs
        self.deleted = False

    async def edit(self, **kwargs) -> None:
        self.kwargs.update(kwargs)

    async def delete(self) -> None:
        self.deleted = True


class FakeGuild:
    def __init__(self, guild_id: int) -> None:
        self.id = guild_id
        self.events: dict[int, FakeEvent] = {}
        self.created: list[dict] = []
        self._next_id = 7000

    def get_channel(self, channel_id):  # no voice channel configured
        return None

    def get_scheduled_event(self, event_id):
        return self.events.get(int(event_id))

    async def fetch_scheduled_event(self, event_id):
        return self.events.get(int(event_id))

    async def create_scheduled_event(self, **kwargs):
        self._next_id += 1
        event = FakeEvent(self._next_id, **kwargs)
        self.events[event.id] = event
        self.created.append(kwargs)
        return event


class FakeMessage:
    def __init__(self, **kwargs) -> None:
        self.kwargs = kwargs
        self.pinned = False
        self.jump_url = "https://discord.test/message"

    async def pin(self) -> None:
        self.pinned = True


class FakeThread:
    """Stands in for discord.Thread."""

    def __init__(self, thread_id: int, **kwargs) -> None:
        self.id = thread_id
        self.kwargs = kwargs
        self.archived = False
        self.messages: list[FakeMessage] = []
        self.members: set[int] = set()

    async def send(self, **kwargs) -> FakeMessage:
        message = FakeMessage(**kwargs)
        self.messages.append(message)
        return message

    async def add_user(self, user) -> None:
        self.members.add(int(user.id))


class FakeTextChannel:
    """Stands in for discord.TextChannel."""

    def __init__(self, channel_id: int) -> None:
        self.id = channel_id
        self.name = "tournament"
        self.threads: list[FakeThread] = []
        self.messages: list[FakeMessage] = []
        self._next_id = 8000

    async def create_thread(self, **kwargs) -> FakeThread:
        self._next_id += 1
        thread = FakeThread(self._next_id, **kwargs)
        self.threads.append(thread)
        return thread

    async def send(self, **kwargs) -> FakeMessage:
        message = FakeMessage(**kwargs)
        self.messages.append(message)
        return message


async def round_start_flow(tmp: str) -> None:
    """The two organiser commands, end to end, on their own bracket."""
    from cogs.threads import ThreadsCog
    from services import start_round, sync_entrants

    server = FakeChallonge()
    server.add_players(["Ana", "Bo", "Cy", "Dee"])
    store = Store(str(Path(tmp) / "round.db"))
    await store.connect()
    budget = Budget(store, limit=500)
    client = ChallongeClient(
        "fake-key", budget, transport=httpx.MockTransport(server.handle)
    )
    channel = FakeTextChannel(77)
    guild = FakeGuild(42)
    bot = SimpleNamespace(
        store=store,
        challonge=client,
        budget=budget,
        dispatch=lambda name, *args: None,
        get_guild=lambda gid: guild if int(gid) == guild.id else None,
        get_channel=lambda cid: channel if int(cid) == channel.id else None,
    )
    threads = ThreadsCog(bot)
    # The real one insists on a discord.TextChannel.
    threads._channel = lambda tournament: channel  # type: ignore[assignment]
    bot.get_cog = lambda name: threads if name == "ThreadsCog" else None
    bot.refresh_matches = lambda t, reason="autosync": refresh_matches(
        bot, t, reason=reason
    )

    fetched = await client.get_tournament("selftest_cup", reason="admin:post")
    await store.save_tournament(fetched, guild_id=42, channel_id=channel.id)
    tournament = await store.get_tournament(9001)

    # Three of the four are on Discord; Dee never signed up.
    for user_id, name in ((500, "Ana"), (501, "Bo"), (502, "Cy")):
        await store.upsert_signup(9001, user_id, name)

    entrants = await sync_entrants(bot, tournament)
    check("sync entrants reads the bracket once", len(server.requests) == 2)
    check("it finds every entrant", len(entrants.participants) == 4)
    check("and links the three sign-ups", entrants.linked_now == 3, str(entrants.linked_now))
    check("leaving one on the bracket only", len(entrants.bracket_only) == 1)
    check("it opens no threads at all", not channel.threads)

    first_day = (datetime.now(timezone.utc) + timedelta(days=7)).date()
    await store.set_guild_config(
        42,
        tz="UTC",
        deadline_hours=240,
        first_match_day=first_day.isoformat(),
        days_per_round=2,
    )

    server.start()
    before = len(server.requests)
    result = await start_round(bot, tournament)
    check("round start reads the bracket once", len(server.requests) - before == 1)
    check("both matches are live", len(result.open_matches) == 2)
    check(
        "every match got a thread, linked or not",
        len(result.report.created) == 2,
        str(len(result.report.created)),
    )
    check(
        "the threads are public",
        all(
            t.kwargs["type"] is discord.ChannelType.public_thread
            for t in channel.threads
        ),
    )
    check(
        "no thread is created invite-only",
        all("invitable" not in t.kwargs for t in channel.threads),
    )
    check(
        "every player we know of was added",
        sorted(m for t in channel.threads for m in t.members) == [500, 501, 502],
    )
    check(
        "the half-linked match says who is missing",
        any(
            "Dee" in (t.messages[0].kwargs.get("content") or "")
            for t in channel.threads
        ),
    )
    check("the round is announced once, in the channel", len(channel.messages) == 1)
    check(
        "the bracket is marked underway",
        (await store.get_tournament(9001))["state"] == "underway",
    )

    from db.store import _parse_iso as parse_stamp
    from services import apply_round_plan, round_plan

    plan = await round_plan(bot, tournament)
    check("the calendar covers both rounds", len(plan.order) == 2)
    check(
        "and fixes the event at three days",
        plan.total_days == 3,
        str(plan.total_days),
    )
    opener = await store.get_match(9001, 1)
    play_by = parse_stamp(opener["play_by"])
    check("round one is stamped on its matches", play_by is not None)
    check(
        "it closes at the end of the first match day",
        play_by == end_of_day(first_day, timezone.utc),
        str(play_by),
    )
    check(
        "a 10-day deadline is cut back to the end of the round",
        parse_stamp(opener["deadline_at"]) == play_by,
        str(opener["deadline_at"]),
    )

    touched = await apply_round_plan(bot, tournament)
    final = await store.get_match(9001, 3)
    check("re-stamping covers every unplayed match", touched == 3, str(touched))
    check(
        "the final is two days after the first round",
        parse_stamp(final["play_by"])
        == end_of_day(first_day + timedelta(days=2), timezone.utc),
    )

    from cogs.common import BotError
    from ui.views import _check_within_round

    player = SimpleNamespace(guild_id=42, user=SimpleNamespace(id=500))
    await _check_within_round(
        bot, tournament, opener, play_by - timedelta(hours=2), player
    )
    check("a time inside the round is allowed", True)
    try:
        await _check_within_round(
            bot, tournament, opener, play_by + timedelta(hours=2), player
        )
        check("a time after the round is refused", False)
    except BotError:
        check("a time after the round is refused", True)

    await store.set_guild_config(42, clear_schedule=True)
    await apply_round_plan(bot, tournament)
    check(
        "clearing the calendar unstamps the matches",
        (await store.get_match(9001, 1))["play_by"] is None,
    )
    check(
        "and the deadline goes back to the configured hours",
        parse_stamp((await store.get_match(9001, 1))["deadline_at"]) > play_by,
    )
    await store.set_guild_config(
        42, first_match_day=first_day.isoformat(), days_per_round=2
    )
    await apply_round_plan(bot, tournament)

    from ui.embeds import (
        entrants_sync_embed,
        round_schedule_embed,
        round_start_embed,
    )

    status = await budget.status()
    panels = [
        entrants_sync_embed(tournament, entrants, budget=status, thread_joins=3),
        await round_schedule_embed(bot, tournament, restamped=3),
        round_start_embed(
            tournament,
            result,
            names=await store.participant_display(9001),
            budget=status,
        ),
    ]
    check(
        "every result panel builds with fields",
        all(len(p.fields) >= 4 for p in panels),
    )
    check(
        "no field is empty or over length",
        all(
            f.value and len(f.value) <= 1024 and f.name
            for p in panels
            for f in p.fields
        ),
    )
    check(
        "the round panel links the threads",
        any("<#" in (f.value or "") for f in panels[-1].fields),
    )

    again = await start_round(bot, tournament)
    check("running it again opens nothing new", not again.report.created)
    check("and finds the existing threads", len(again.report.existing) == 2)
    check("still only two threads", len(channel.threads) == 2)

    await client.aclose()
    await store.close()


# ------------------------------------------------------------------- harness


async def run() -> None:
    server = FakeChallonge()
    server.add_players(["Ana", "Bo", "Cy", "Dee"])
    events: list[tuple] = []

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
        store = Store(str(Path(tmp) / "test.db"))
        await store.connect()

        budget = Budget(store, limit=500)
        client = ChallongeClient(
            "fake-key", budget, transport=httpx.MockTransport(server.handle)
        )
        guild = FakeGuild(42)
        bot = SimpleNamespace(
            store=store,
            challonge=client,
            budget=budget,
            dispatch=lambda name, *args: events.append((name, args)),
            get_guild=lambda gid: guild if int(gid) == guild.id else None,
        )
        bot.refresh_matches = lambda t, reason="autosync": refresh_matches(
            bot, t, reason=reason
        )

        print("\n1. reading what the organiser pasted")
        check(
            "full url",
            slug_from("https://challonge.com/selftest_cup") == "selftest_cup",
        )
        check(
            "url with cruft",
            slug_from("https://challonge.com/selftest_cup/?x=1") == "selftest_cup",
        )
        check("bare id", slug_from(" 9001 ") == "9001")

        print("\n2. time parsing")
        anchor = datetime(2026, 9, 3, 14, 0, tzinfo=timezone.utc)  # a Thursday
        check("relative", parse_when("in 2h", "UTC", now=anchor).hour == 16)
        check(
            "tomorrow + clock",
            parse_when("tomorrow 20:00", "UTC", now=anchor).day == 4,
        )
        check(
            "named weekday rolls forward",
            parse_when("friday 19:30", "UTC", now=anchor).day == 4,
        )
        check(
            "same weekday already past rolls a week",
            parse_when("thursday 10am", "UTC", now=anchor).day == 10,
        )
        try:
            parse_when("whenever", "UTC", now=anchor)
            check("garbage rejected", False)
        except TimeParseError:
            check("garbage rejected", True)

        print("\n3. /tournament post")
        fetched = await client.get_tournament("selftest_cup", reason="admin:post")
        await store.save_tournament(fetched, guild_id=42, channel_id=7)
        await store.replace_participants(
            fetched.id, await client.list_participants(fetched.id, reason="admin:post")
        )
        tournament = await store.get_tournament(9001)
        opened, _ = await refresh_matches(bot, tournament, reason="autosync")

        check("parsed the tournament", fetched.id == 9001, str(fetched.id))
        check(
            "it is the active one", (await store.get_active_tournament(42)) is not None
        )
        check("four players cached", len(await store.list_participants(9001)) == 4)
        check("nothing open before the bracket starts", opened == [])
        check(
            "posting cost exactly 3 requests",
            len(server.requests) == 3,
            str(len(server.requests)),
        )

        print("\n4. a bad url")
        server.diagnostic_mode = True
        try:
            await client.get_tournament("no-such-bracket", reason="admin:post")
            check("missing bracket raises NotFound", False)
        except NotFound:
            check("missing bracket raises NotFound", True)
        server.diagnostic_mode = False

        print("\n5. signing up on Discord")
        # Two separate lists: who signed up here, and who the organiser typed
        # into Challonge. They are joined by name.
        await store.upsert_signup(9001, 500, "Ana")
        check(
            "a sign-up finds its bracket entry",
            await store.link_signups_to_bracket(9001) == 1,
        )
        ana = await store.participant_by_name(9001, "ana")
        check("lookup ignores case", ana is not None)
        check("and it is the right person", int(ana["discord_user_id"]) == 500)

        await store.upsert_signup(9001, 999, "Late Arrival")
        check(
            "a sign-up with no bracket entry links nothing yet",
            await store.link_signups_to_bracket(9001) == 0,
        )
        server.add_players(["Late Arrival"])
        server.diagnostic_mode = True
        await store.replace_participants(9001, await client.list_participants(9001, reason="admin:post"))
        server.diagnostic_mode = False
        # replace_participants rewrites the table from Challonge, which drops
        # every local link, so the re-link afterwards has to restore all of
        # them, not just the new arrival.
        relinked = await store.link_signups_to_bracket(9001)
        late = await store.participant_by_name(9001, "Late Arrival")
        check(
            "and links itself once that name appears on the bracket",
            late is not None and int(late["discord_user_id"]) == 999,
        )
        check(
            "the earlier link is restored at the same time",
            relinked == 2,
            f"{relinked} relinked",
        )
        # Take the stray back out so the bracket stays a clean four.
        await store.remove_signup(9001, 999)
        server.participants.pop()
        server.diagnostic_mode = True
        await store.replace_participants(9001, await client.list_participants(9001, reason="admin:post"))
        server.diagnostic_mode = False
        await store.link_signups_to_bracket(9001)

        check(
            "duplicate names are refused",
            await store.signup_name_taken(9001, "ana", 501),
        )
        check(
            "your own name is not a duplicate of itself",
            not await store.signup_name_taken(9001, "Ana", 500),
        )

        for index, name in enumerate(["Bo", "Cy"]):
            await store.upsert_signup(9001, 501 + index, name)
        await store.link_signups_to_bracket(9001)
        check(
            "three of four reachable, Dee never signed up",
            len(await store.unlinked_participants(9001)) == 1,
        )

        display = await store.participant_display(9001)
        ana = await store.participant_by_name(9001, "Ana")
        dee = await store.participant_by_name(9001, "Dee")
        check(
            "signed-up players are shown as mentions",
            display[int(ana["participant_id"])] == "<@500>",
        )
        check(
            "everyone else is shown by name and never pinged",
            display[int(dee["participant_id"])] == "Dee",
        )

        print("\n6. the organiser starts the bracket on the website")
        before = len(server.requests)
        server.start()
        check("starting it cost the bot nothing", len(server.requests) == before)
        events.clear()
        opened, completed = await refresh_matches(bot, tournament, reason="autosync")
        check("one sync noticed both matches", len(opened) == 2, str(len(opened)))
        check("sync cost one request", len(server.requests) - before == 1)
        check(
            "matches_opened dispatched", any(n == "matches_opened" for n, _ in events)
        )

        needing = await store.matches_needing_threads(9001)
        check("both first-round matches want threads", len(needing) == 2)
        pairs = []
        for match in needing:
            reachable = await store.discord_ids_for(
                9001, [int(match["player1_id"]), int(match["player2_id"])]
            )
            pairs.append(len(reachable))
        pairs.sort()
        check(
            "a half-claimed pairing wants one all the same",
            pairs == [1, 2],
            str(pairs),
        )

        print("\n7. scheduling handshake and deadline")
        await store.set_guild_config(42, deadline_hours=24, event_duration=60)
        match1 = await store.get_match(9001, 1)
        deadline = await start_scheduling_window(bot, tournament, match1)
        check("deadline stamped", deadline is not None)
        queued = await store.due_reminders(deadline + timedelta(minutes=1))
        check(
            "three nudges queued",
            len([r for r in queued if r["kind"] in DEADLINE_KINDS]) == 3,
        )

        first = datetime.now(timezone.utc) + timedelta(hours=3)
        first_id = await propose_time(
            bot, tournament, match1, proposer_id=500, responder_id=501, when=first
        )
        state = await store.get_match(9001, 1)
        check("status becomes proposed", state["scheduling_status"] == "proposed")
        check("a proposal alone books nothing", state["scheduled_at"] is None)

        when = datetime.now(timezone.utc) + timedelta(hours=5)
        await propose_time(
            bot, tournament, match1, proposer_id=501, responder_id=500, when=when
        )
        check(
            "counter supersedes the first offer",
            (await store.get_proposal(first_id))["status"] == "superseded",
        )
        pending = await store.pending_proposal(9001, 1)
        agreed_at = await accept_proposal(bot, tournament, match1, pending)
        booked = await store.get_match(9001, 1)
        check("accept books the counter-offer", agreed_at == when)
        check("status becomes agreed", booked["scheduling_status"] == "agreed")

        remaining = await store.due_reminders(when + timedelta(hours=1))
        check(
            "deadline nudges cancelled on agreement",
            all(r["kind"] not in DEADLINE_KINDS for r in remaining),
        )
        check(
            "T-1h and T-5m queued",
            sorted(r["kind"] for r in remaining) == ["1h", "5m"],
        )
        check(
            "agreeing also schedules a probe",
            (await store.get_tournament(9001))["next_refresh_at"] is not None,
        )

        match2 = await store.get_match(9001, 2)
        await start_scheduling_window(bot, tournament, match2)
        longer = await extend_deadline(bot, tournament, match2, 24)
        check("extension re-arms the deadline", longer > datetime.now(timezone.utc))
        await force_time(
            bot, tournament, match2, datetime.now(timezone.utc) + timedelta(hours=2)
        )
        check(
            "an organiser can set a time outright",
            (await store.get_match(9001, 2))["scheduling_status"] == "agreed",
        )

        print("\n8. results are entered on the website")
        before = len(server.requests)
        server.report(1, "2-1", 100)
        check("entering a score cost the bot nothing", len(server.requests) == before)
        check(
            "the bot has not noticed yet",
            (await store.get_match(9001, 1))["state"] == "open",
        )

        await store.set_match_thread(9001, 1, 111111111)
        events.clear()
        opened, completed = await refresh_matches(bot, tournament, reason="autosync")
        check("one request to catch up", len(server.requests) - before == 1)
        check(
            "match 1 complete",
            (await store.get_match(9001, 1))["state"] == "complete",
        )
        check("one completion reported", len(completed) == 1)
        check(
            "match_completed dispatched",
            any(n == "match_completed" for n, _ in events),
        )
        check(
            "this match's reminders cleared",
            all(
                int(r["match_id"]) != 1
                for r in await store.due_reminders(when + timedelta(hours=2))
            ),
        )
        check(
            "the thread link survives the refresh",
            (await store.get_match(9001, 1))["thread_id"] == 111111111,
        )

        server.report(2, "2-0", 102)
        events.clear()
        opened, _ = await refresh_matches(bot, tournament, reason="autosync")
        final = await store.get_match(9001, 3)
        check("the final opened", final["state"] == "open")
        check(
            "with both winners in it",
            final["player1_id"] == 100 and final["player2_id"] == 102,
        )
        check("reported as newly open", [int(r["match_id"]) for r in opened] == [3])

        print("\n9. a player added on the website mid-tournament")
        server.diagnostic_mode = True
        before = len(server.diagnostics)
        server.add_players(["Eve"])
        # Point the final at the newcomer so the bracket names somebody the bot
        # has never seen.
        server.matches[2]["relationships"]["player2"] = {
            "data": {"id": "104", "type": "participant"}
        }
        await refresh_matches(bot, tournament, reason="autosync")
        check(
            "an unknown player triggers exactly one extra read",
            len(server.diagnostics) - before == 2,
            f"{len(server.diagnostics) - before} requests",
        )
        check(
            "and they land in the cache",
            (await store.participant_by_name(9001, "Eve")) is not None,
        )
        server.matches[2]["relationships"]["player2"] = {
            "data": {"id": "102", "type": "participant"}
        }
        await refresh_matches(bot, tournament, reason="autosync")
        server.diagnostic_mode = False

        print("\n10. the tournament finishes")
        events.clear()
        before = len(server.requests)
        server.report(3, "3-1", 100)
        await refresh_matches(bot, tournament, reason="autosync")
        check(
            "finishing costs one sync plus one standings read",
            len(server.requests) - before == 2,
            f"{len(server.requests) - before} requests",
        )
        check(
            "tournament_completed dispatched",
            any(n == "tournament_completed" for n, _ in events),
        )
        check(
            "state recorded as complete",
            (await store.get_tournament(9001))["state"] == "complete",
        )
        check(
            "final places stored",
            (await store.list_participants(9001))[0]["final_rank"] == 1,
        )

        print("\n11. auto-refresh policy")
        base = datetime(2026, 9, 3, 12, 0, tzinfo=timezone.utc)
        opening = evaluate_refresh(base, window_start=None, window_count=0)
        check(
            "first refresh opens a window",
            opening.allowed and opening.window_count == 1,
        )
        check(
            "third in the hour still allowed",
            evaluate_refresh(
                base + timedelta(minutes=5), window_start=base, window_count=2
            ).allowed,
        )
        today = base.strftime("%Y-%m-%d")
        spent = evaluate_refresh(
            base, window_start=None, window_count=0,
            day=today, day_count=12, max_per_day=12,
        )
        check("the daily allowance is a hard stop", not spent.allowed, spent.reason)
        check(
            "and it retries tomorrow, not in an hour",
            spent.retry_at is not None and spent.retry_at.day == base.day + 1,
        )
        off = evaluate_refresh(
            base, window_start=None, window_count=0, max_per_day=0
        )
        check("allowance 0 means the loop never spends", not off.allowed, off.reason)
        yesterday = evaluate_refresh(
            base, window_start=None, window_count=0,
            day="2026-09-02", day_count=99, max_per_day=12,
        )
        check("yesterday's spend does not count against today", yesterday.allowed)
        capped = evaluate_refresh(
            base + timedelta(minutes=10), window_start=base, window_count=3
        )
        check("fourth is refused", not capped.allowed, capped.reason)
        check(
            "refusal says when to retry", capped.retry_at == base + timedelta(hours=1)
        )
        check(
            "window rolls over after an hour",
            evaluate_refresh(
                base + timedelta(hours=1, minutes=1),
                window_start=base,
                window_count=3,
            ).allowed,
        )
        check(
            "first probe is 10 minutes after the match time",
            next_probe_at(base, base) == base + timedelta(minutes=10),
        )
        check(
            "probes taper, then stop",
            next_probe_at(base, base + timedelta(hours=4)) is None,
        )
        planned, reason = plan_next_refresh(
            base, open_matches=await store.list_matches(9001, state="open")
        )
        check("a finished bracket is not polled", planned is None, reason)

        print("\n12. featured matches become Discord events")
        guild.created.clear()
        await store.db.execute(
            "UPDATE matches SET state = 'open' WHERE tournament_id = 9001 "
            "AND match_id = 3"
        )
        await store.db.commit()
        await store.set_match_live(9001, 3, True)
        live_when = datetime.now(timezone.utc) + timedelta(days=1)
        await store.confirm_match_time(9001, 3, live_when)
        await sync_event(bot, tournament, await store.get_match(9001, 3))
        check("event published", len(guild.created) == 1)
        event_id = (await store.get_match(9001, 3))["event_id"]
        check("event id stored", event_id is not None)
        check(
            "external events carry an end time",
            guild.events[int(event_id)].kwargs.get("end_time") is not None,
        )

        moved = live_when + timedelta(hours=2)
        await store.confirm_match_time(9001, 3, moved)
        await sync_event(bot, tournament, await store.get_match(9001, 3))
        check("rescheduling moves it, no duplicate", len(guild.created) == 1)
        check(
            "start follows the new time",
            guild.events[int(event_id)].kwargs["start_time"] == moved,
        )

        await store.set_match_live(9001, 3, False)
        await sync_event(bot, tournament, await store.get_match(9001, 3))
        check("un-featuring deletes it", guild.events[int(event_id)].deleted)
        check(
            "event id cleared", (await store.get_match(9001, 3))["event_id"] is None
        )
        guild.created.clear()
        await sync_event(bot, tournament, await store.get_match(9001, 1))
        check("a normal match publishes nothing", guild.created == [])

        print("\n13. request budget")
        status = await budget.status()
        total = len(server.requests) + len(server.diagnostics)
        check(
            "every request was counted, diagnostics included",
            status.used == total,
            f"{status.used} vs {total}",
        )
        tight = Budget(store, limit=max(1, status.used))
        try:
            await tight.reserve("autosync")
            check("reads refused near the cap", False)
        except BudgetExhausted:
            check("reads refused near the cap", True)

        print("\n14. re-posting an archived tournament")
        server.diagnostic_mode = True
        # Regression: save_tournament used to leave archived = 1, so re-posting
        # a bracket the bot had finished made every command report "no active
        # tournament" even though the post had just succeeded.
        await store.archive_tournament(9001)
        check("archived", (await store.get_active_tournament(42)) is None)
        await store.save_tournament(
            await client.get_tournament(9001, reason="admin:post"), guild_id=42, channel_id=7
        )
        back = await store.get_active_tournament(42)
        check("re-posting un-archives it", back is not None)
        check(
            "same tournament", back is not None and int(back["challonge_id"]) == 9001
        )
        server.diagnostic_mode = False

        print("\n15. upgrading an old database")
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as old_dir:
            old_path = str(Path(old_dir) / "old.db")
            legacy = sqlite3.connect(old_path)
            legacy.executescript(
                """
                CREATE TABLE guilds (
                    guild_id INTEGER PRIMARY KEY,
                    tournament_channel_id INTEGER,
                    to_role_id INTEGER,
                    timezone TEXT NOT NULL DEFAULT 'UTC');
                CREATE TABLE tournaments (
                    challonge_id INTEGER PRIMARY KEY,
                    guild_id INTEGER NOT NULL,
                    name TEXT NOT NULL,
                    url TEXT, full_url TEXT, tournament_type TEXT,
                    state TEXT NOT NULL DEFAULT 'pending',
                    channel_id INTEGER, signup_message_id INTEGER,
                    archived INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL DEFAULT (datetime('now')));
                CREATE TABLE matches (
                    tournament_id INTEGER NOT NULL, match_id INTEGER NOT NULL,
                    identifier TEXT, round INTEGER NOT NULL DEFAULT 0,
                    play_order INTEGER, player1_id INTEGER, player2_id INTEGER,
                    state TEXT NOT NULL DEFAULT 'pending', winner_id INTEGER,
                    scores TEXT, thread_id INTEGER, scheduled_at TEXT,
                    PRIMARY KEY (tournament_id, match_id));
                INSERT INTO tournaments (challonge_id, guild_id, name)
                    VALUES (5, 42, 'Legacy Cup');
                INSERT INTO matches (tournament_id, match_id, state, thread_id)
                    VALUES (5, 1, 'open', 999);
                """
            )
            legacy.commit()
            legacy.close()

            upgraded = Store(old_path)
            await upgraded.connect()
            row = await upgraded.get_match(5, 1)
            check("existing rows survive", row is not None)
            check("old data intact", row is not None and row["thread_id"] == 999)
            check(
                "new columns added with defaults",
                row is not None
                and row["live"] == 0
                and row["scheduling_status"] == "pending"
                and row["event_id"] is None,
            )
            await upgraded.close()

        print("\n16. nothing a player does can spend a request")
        # The headline guarantee of this design, checked rather than promised.
        root = Path(__file__).resolve().parent.parent
        player_facing = sorted((root / "ui").glob("*.py"))
        check("there is a player-facing package to check", len(player_facing) >= 2)
        offenders = [
            path.name
            for path in player_facing
            if any(
                needle in path.read_text(encoding="utf-8")
                for needle in (".challonge.", "refresh_matches(")
            )
        ]
        check(
            "no button, modal or embed can reach Challonge",
            not offenders,
            f"offending files: {offenders}",
        )

        # Every reason the code actually uses has to be one the policy knows.
        declared = set()
        reason_literal = re.compile(r'reason(?:\s*:\s*str)?\s*=\s*"([^"]+)"')
        for path in list((root / "cogs").glob("*.py")) + [root / "services.py"]:
            declared.update(
                reason_literal.findall(path.read_text(encoding="utf-8"))
            )
        check(
            "every declared reason is in the closed set",
            declared <= REASONS,
            f"unknown: {sorted(declared - REASONS)}",
        )
        check("and the admin paths are among them", "admin:post" in declared)

        try:
            await budget.reserve("player:signup")
            check("an undeclared reason is refused", False)
        except UnknownReason:
            check("an undeclared reason is refused", True)

        ledger = await budget.status()
        check(
            "the ledger attributes today's spend",
            ledger.today.get("admin:post", 0) > 0
            and ledger.today.get("autosync", 0) > 0,
            ledger.breakdown(),
        )
        check(
            "and the day's total matches the sum of its causes",
            ledger.used_today == sum(ledger.today.values()),
        )
        print("\n17. one SQL, two engines")
        # The bot runs on SQLite here and Postgres on Render. Queries are
        # written once, in SQLite's dialect, and translated.
        check(
            "placeholders are numbered",
            to_postgres("SELECT * FROM t WHERE a = ? AND b = ?")
            == "SELECT * FROM t WHERE a = $1 AND b = $2",
        )
        check(
            "INSERT OR IGNORE becomes an upsert that does nothing",
            to_postgres("INSERT OR IGNORE INTO guilds (guild_id) VALUES (?)")
            == "INSERT INTO guilds (guild_id) VALUES ($1) ON CONFLICT DO NOTHING",
        )
        check(
            "an existing ON CONFLICT is left alone",
            to_postgres(
                "INSERT INTO api_usage (month, count) VALUES (?, ?) "
                "ON CONFLICT(month) DO UPDATE SET count = api_usage.count + excluded.count"
            ).count("ON CONFLICT")
            == 1,
        )
        check(
            "case-insensitive comparison folds both sides",
            to_postgres("SELECT * FROM p WHERE name = ? COLLATE NOCASE")
            == "SELECT * FROM p WHERE lower(name) = lower($1)",
        )
        check(
            "and does so column to column too",
            to_postgres("WHERE s.name = p.name COLLATE NOCASE")
            == "WHERE lower(s.name) = lower(p.name)",
        )
        check(
            "rowcount is read off asyncpg's status line",
            rowcount_from_status("UPDATE 3") == 3
            and rowcount_from_status("DELETE 0") == 0,
        )

        # Every real query in store.py has to survive the translation.
        store_sql = (root / "db" / "store.py").read_text(encoding="utf-8")
        translated = 0
        for chunk in store_sql.split('"""')[1::2]:
            if "SELECT" in chunk or "INSERT" in chunk or "UPDATE" in chunk:
                to_postgres(chunk)
                translated += 1
        check("every multi-line query translates without error", translated > 5)

        print("\n18. the two schemas cannot drift apart")
        # schema.sql builds SQLite; db/supabase_setup.sql is what you run in the
        # Supabase SQL Editor. Written separately because DDL is where the
        # engines genuinely differ, so drift is the risk and it is checked
        # rather than trusted.
        def tables_of(text, prefix=""):
            found = {}
            for block in text.split("CREATE TABLE IF NOT EXISTS ")[1:]:
                name = block.split("(", 1)[0].strip()
                if prefix and not name.startswith(prefix):
                    continue
                name = name[len(prefix):]
                body = block.split("(", 1)[1].split(");")[0]
                columns = set()
                for raw in body.split(","):
                    line = raw.strip().split("\n")[-1].strip()
                    if not line or line.startswith("--") or line.upper().startswith(
                        ("PRIMARY KEY", "UNIQUE", "FOREIGN KEY", "CHECK")
                    ):
                        continue
                    columns.add(line.split()[0])
                found[name] = columns
            return found

        setup_sql = (root / "db" / "supabase_setup.sql").read_text(encoding="utf-8")
        lite = tables_of((root / "db" / "schema.sql").read_text(encoding="utf-8"))
        hosted = tables_of(setup_sql, prefix="tournamentbot.")

        check("both schemas define tables", len(lite) >= 8, str(sorted(lite)))
        check(
            "the same tables exist on both engines",
            set(lite) == set(hosted),
            f"only sqlite: {sorted(set(lite) - set(hosted))}, "
            f"only supabase: {sorted(set(hosted) - set(lite))}",
        )
        mismatched = {
            name: (lite[name] ^ hosted.get(name, set()))
            for name in lite
            if lite[name] != hosted.get(name, set())
        }
        check("and every table has the same columns", not mismatched, str(mismatched))
        check(
            "Supabase widens ids to BIGINT for Discord snowflakes",
            "discord_user_id BIGINT" in " ".join(setup_sql.split()),
        )
        check(
            "the rpc is locked to service_role",
            "GRANT EXECUTE ON FUNCTION tournamentbot.bot_sql" in setup_sql
            and "REVOKE ALL ON FUNCTION" in setup_sql,
        )
        check(
            "and the bot's tables are not readable with the anon key",
            setup_sql.count("ENABLE ROW LEVEL SECURITY") >= len(lite),
        )
        print("\n19. Null Rush sets the match up and reports it back")
        # The relay is ours, so this whole section costs no Challonge quota.
        # The fake below is the relay's HTTP API, not Challonge's.
        rooms = {}

        def relay_handler(request: httpx.Request) -> httpx.Response:
            if request.headers.get("authorization") != "Bearer test-relay-token":
                return httpx.Response(401, json={"error": "bad token"})
            if request.method == "POST":
                body = json.loads(request.content)
                code = "AB23CD"
                rooms[code] = {
                    "code": code,
                    "match_id": body["matchId"],
                    "best_of": body.get("bestOf", 1),
                    "state": "open",
                }
                return httpx.Response(201, json=rooms[code])
            code = request.url.path.rsplit("/", 1)[-1]
            if code not in rooms:
                return httpx.Response(404, json={"error": "no such match"})
            return httpx.Response(200, json=rooms[code])

        relay = RelayClient(
            "http://relay.test",
            "test-relay-token",
            transport=httpx.MockTransport(relay_handler),
        )
        bot.relay = relay
        check("a configured relay reports itself as such", relay.configured)

        before_challonge = len(server.requests)
        match_one = await store.get_match(9001, 1)
        code = await open_room(bot, tournament, match_one)
        check("a room code comes back", len(code) == 6, code)
        check(
            "setting a match up costs no Challonge quota",
            len(server.requests) == before_challonge,
        )
        check(
            "and the code is remembered against the match",
            (await store.get_match(9001, 1))["room_code"] == code,
        )
        check(
            "asking twice reuses the same room",
            await open_room(bot, tournament, await store.get_match(9001, 1)) == code,
        )
        check(
            "a result can find its way home by code",
            (await store.match_for_room_code(code.lower())) is not None,
        )

        # What the webhook does when Null Rush finishes a match.
        state_before = (await store.get_match(9001, 1))["state"]
        await store.record_reported_result(9001, 1, 100, "2-1")
        reported = await store.get_match(9001, 1)
        check("the reported winner is recorded", reported["winner_id"] == 100)
        check("and the score with it", reported["scores"] == "2-1")
        check(
            "but the match state is untouched: Challonge still decides that",
            reported["state"] == state_before,
            f"{state_before} -> {reported['state']}",
        )

        unconfigured = RelayClient(None, None)
        check("an unconfigured relay is a supported state", not unconfigured.configured)
        try:
            await unconfigured.create_match(
                match_id="1", tournament="1", players=["a", "b"]
            )
            check("and it refuses clearly rather than crashing", False)
        except RelayError:
            check("and it refuses clearly rather than crashing", True)
        await unconfigured.aclose()

        print("\n20. the one write is fenced off")
        check(
            "a write cannot borrow an automatic reason",
            "autosync" not in ADMIN_REASONS and "admin:report" in ADMIN_REASONS,
        )
        try:
            await client.report_match(
                9001, 1, winner_id=100, scores_csv="2-1", reason="autosync"
            )
            check("report_match refuses a non-admin reason", False)
        except ValueError:
            check("report_match refuses a non-admin reason", True)
        check(
            "and no request was spent on the refusal",
            len(server.requests) == before_challonge,
        )
        await relay.aclose()
        print("\n21. storage over Supabase, the way the relay does it")
        # The hosted engine talks to the same project, through the same REST API
        # and the same two variables as tools/relay/scores.js. Faked here so the
        # request shape is checked without a Supabase account.
        seen = {}

        def supabase_handler(request: httpx.Request) -> httpx.Response:
            seen["path"] = request.url.path
            seen["apikey"] = request.headers.get("apikey")
            seen["auth"] = request.headers.get("authorization")
            body = json.loads(request.content)
            seen["q"] = body["q"]
            seen["args"] = body["args"]

            if "information_schema" in body["q"]:
                return httpx.Response(
                    200, json={"rows": [{"column_name": "guild_id"}], "rowcount": 1}
                )
            upper = body["q"].lstrip().upper()
            if upper.startswith("SELECT") or " RETURNING " in f" {upper} ":
                return httpx.Response(
                    200, json={"rows": [{"id": 7, "name": "Ana"}], "rowcount": 1}
                )
            return httpx.Response(200, json={"rows": [], "rowcount": 3})

        secret = SupabaseBackend(
            "https://demo.supabase.co/rest/v1",
            "sb_secret_abc",
            transport=httpx.MockTransport(supabase_handler),
        )
        check(
            "the dashboard's /rest/v1 suffix is trimmed off",
            normalise_url("https://demo.supabase.co/rest/v1") == "https://demo.supabase.co",
        )

        async with secret.execute(
            "SELECT * FROM guilds WHERE guild_id = ?", (42,)
        ) as cur:
            row = await cur.fetchone()
        check("it calls the rpc endpoint", seen["path"] == "/rest/v1/rpc/bot_sql")
        check("placeholders arrive numbered", "$1" in seen["q"], seen["q"])
        check("arguments travel as strings", seen["args"] == ["42"])
        check("rows come back as rows", row is not None and row["name"] == "Ana")

        check("apikey is always sent", seen["apikey"] == "sb_secret_abc")
        check(
            "a new secret key is NOT sent as a bearer token",
            seen["auth"] is None,
            str(seen["auth"]),
        )

        jwt_key = "eyJhbGciOiJIUzI1NiJ9.eyJyb2xlIjoic2VydmljZV9yb2xlIn0.sig"
        legacy = SupabaseBackend(
            "https://demo.supabase.co",
            jwt_key,
            transport=httpx.MockTransport(supabase_handler),
        )
        await legacy.execute("SELECT 1", ())
        check(
            "but a legacy JWT key is, so PostgREST sees its role",
            seen["auth"] == f"Bearer {jwt_key}",
        )

        cur = await secret.execute("UPDATE matches SET live = ?", (1,))
        check("a write reports how many rows it touched", cur.rowcount == 3)
        cur = await secret.execute("INSERT INTO proposals (x) VALUES (?)", (1,))
        check("an insert into a table with an id brings it back", cur.lastrowid == 7)
        check(
            "because RETURNING id was added for it",
            "RETURNING id" in seen["q"],
            seen["q"],
        )
        await secret.execute("INSERT INTO matches (x) VALUES (?)", (1,))
        check(
            "but not for a table that has no id column",
            "RETURNING" not in seen["q"],
            seen["q"],
        )

        columns = await secret.table_columns("guilds")
        check("the migration can still see the columns", columns == {"guild_id"})
        check(
            "and it asks about our schema, not public",
            seen["args"] == ["guilds", "tournamentbot"],
            str(seen["args"]),
        )

        def missing_function(request: httpx.Request) -> httpx.Response:
            return httpx.Response(404, json={"message": "function does not exist"})

        unset = SupabaseBackend(
            "https://demo.supabase.co",
            "sb_secret_abc",
            transport=httpx.MockTransport(missing_function),
        )
        try:
            await unset.connect()
            check("a missing bot_sql is explained, not a stack trace", False)
        except SupabaseError as exc:
            check(
                "a missing bot_sql is explained, not a stack trace",
                "supabase_setup.sql" in str(exc),
                str(exc)[:80],
            )
        await secret.close()
        await legacy.close()
        await unset.close()

        # The setup SQL and the tables the bot actually uses must agree.
        setup = (root / "db" / "supabase_setup.sql").read_text(encoding="utf-8")
        lite_tables = {
            block.split("(", 1)[0].strip()
            for block in (root / "db" / "schema.sql")
            .read_text(encoding="utf-8")
            .split("CREATE TABLE IF NOT EXISTS ")[1:]
        }
        missing = [t for t in lite_tables if f"tournamentbot.{t}" not in setup]
        check(
            "every table the bot uses is in the setup SQL",
            not missing,
            f"missing: {missing}",
        )
        print("\n22. the round calendar")
        check(
            "rounds are put in the order they are played",
            round_sequence([2, -1, 1, -2]) == [1, -1, 2, -2],
        )
        check("uneven match days are read", parse_round_days("0, 3,7") == [0, 3, 7])
        check("an empty setting means no custom plan", parse_round_days("") == [])
        for bad in ("7,3", "x", "-1", "0,20"):
            try:
                parse_round_days(bad)
                check(f"{bad!r} is refused", False)
            except ValueError:
                check(f"{bad!r} is refused", True)

        check(
            "a uniform gap covers the whole bracket",
            round_offsets(3, days_per_round=4) == [0, 4, 8],
        )
        check(
            "a short custom list is extended by its own last gap",
            round_offsets(5, custom=[0, 2, 5]) == [0, 2, 5, 8, 11],
        )
        check(
            "no gap at all puts every round on the first day",
            round_offsets(3) == [0, 0, 0],
        )
        check(
            f"nothing is ever placed past day {MAX_ROUND_DAYS}",
            round_offsets(4, days_per_round=7) == [0, 7, 13, 13],
        )
        check(
            "a custom day beyond the limit is pulled back too",
            round_offsets(4, custom=[0, 3, 7, 40]) == [0, 3, 7, 13],
        )

        plan = build_round_plan(
            [1, 2, 3], first_day=date(2026, 9, 12), days_per_round=4
        )
        check("every round gets a day", len(plan.days) == 3)
        check("the final is eight days out", plan.last_day == date(2026, 9, 20))
        check(
            "so the organiser knows the length up front",
            plan.total_days == 9,
            str(plan.total_days),
        )
        check("and nothing had to be pulled back", not plan.clamped)

        long_plan = build_round_plan(
            [1, 2, 3, 4], first_day=date(2026, 9, 12), days_per_round=7
        )
        check(
            "a tournament can never run past two weeks",
            long_plan.total_days == MAX_TOURNAMENT_DAYS,
            str(long_plan.total_days),
        )
        check(
            "and it says which rounds it had to pull back",
            long_plan.clamped == [3, 4],
            str(long_plan.clamped),
        )
        check(
            "even an absurd gap stays inside the fortnight",
            build_round_plan(
                [1, 2], first_day=date(2026, 9, 12), days_per_round=999
            ).total_days
            == MAX_TOURNAMENT_DAYS,
        )
        check(
            "a one-day event is one day long",
            build_round_plan([1, 2], first_day=date(2026, 9, 12)).total_days == 1,
        )
        check(
            "a round closes at the end of its day",
            end_of_day(date(2026, 9, 12), timezone.utc)
            == datetime(2026, 9, 12, 23, 59, tzinfo=timezone.utc),
        )

        print("\n23. sync entrants, then start the round")
        await round_start_flow(tmp)

        print("\n24. what the whole tournament cost")
        for method, path in server.requests:
            print(f"     {method:6} {path}")
        check(
            "a full 4-player event stayed in single digits",
            len(server.requests) < 10,
            f"{len(server.requests)} requests",
        )

        await client.aclose()
        await store.close()

    print(f"\n{'-' * 60}\n{PASSED} passed, {FAILED} failed")
    if FAILED:
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(run())
