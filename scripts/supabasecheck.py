"""Integration check for Supabase backend connection and schema."""

from __future__ import annotations

import asyncio
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv  # noqa: E402

from db.store import Store  # noqa: E402

load_dotenv()

TOURNAMENT = 999999999
GUILD = 999999998

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


async def cleanup(store: Store) -> None:
    for table in (
        "reminders",
        "proposals",
        "matches",
        "participants",
        "signups",
        "tournaments",
    ):
        await store.db.execute(
            f"DELETE FROM {table} WHERE tournament_id = ?", (TOURNAMENT,)
        )
    await store.db.execute("DELETE FROM tournaments WHERE challonge_id = ?", (TOURNAMENT,))
    await store.db.execute("DELETE FROM guilds WHERE guild_id = ?", (GUILD,))
    await store.db.commit()


async def main() -> None:
    url = os.getenv("SUPABASE_URL", "").strip()
    key = (
        os.getenv("SUPABASE_SECRET_KEY")
        or os.getenv("SUPABASE_SERVICE_ROLE_KEY")
        or os.getenv("SUPABASE_KEY")
        or ""
    ).strip()
    if not (url and key):
        raise SystemExit(
            "SUPABASE_URL and SUPABASE_SECRET_KEY are both needed. This script "
            "exists to test the hosted path; without them there is nothing to "
            "test."
        )

    store = Store("unused.db", url, key)
    await store.connect()
    check("connected, and bot_sql answered", store.dialect == "postgres")

    await cleanup(store)

    print("\nguild settings")
    await store.set_guild_config(GUILD, tz="Europe/Berlin", syncs_per_day=7)
    config = await store.get_guild_config(GUILD)
    check("insert-or-ignore then update", config is not None)
    check("values round-trip", config["timezone"] == "Europe/Berlin")
    check("and the spend dial persists", int(config["syncs_per_day"]) == 7)

    print("\nthe request ledger")
    await store.bump_usage("2999-01", 1)
    await store.bump_usage("2999-01", 1)
    check("the monthly upsert accumulates", await store.get_usage("2999-01") == 2)
    await store.bump_usage_day("2999-01-01", "autosync")
    await store.bump_usage_day("2999-01-01", "autosync")
    by_reason = await store.usage_by_reason("2999-01-01")
    check("and the daily one counts by reason", by_reason.get("autosync") == 2)
    await store.db.execute("DELETE FROM api_usage WHERE month = ?", ("2999-01",))
    await store.db.execute("DELETE FROM api_usage_day WHERE day = ?", ("2999-01-01",))
    await store.db.commit()

    print("\nsnowflakes")
    # The reason the setup SQL says BIGINT: a Discord id overflows int4.
    big = 1234567890123456789
    await store.db.execute(
        "INSERT INTO tournaments (challonge_id, guild_id, name) VALUES (?, ?, ?)",
        (TOURNAMENT, GUILD, "pgcheck"),
    )
    await store.db.execute(
        "INSERT INTO signups (tournament_id, discord_user_id, name) VALUES (?, ?, ?)",
        (TOURNAMENT, big, "Ana"),
    )
    await store.db.commit()
    signup = await store.signup_for_discord(TOURNAMENT, big)
    check("a 64-bit Discord id survives", signup is not None and int(signup["discord_user_id"]) == big)

    print("\nname matching")
    await store.db.execute(
        "INSERT INTO participants (tournament_id, participant_id, name) "
        "VALUES (?, ?, ?)",
        (TOURNAMENT, 1, "ANA"),
    )
    await store.db.commit()
    linked = await store.link_signups_to_bracket(TOURNAMENT)
    check("case-insensitive join links the sign-up", linked == 1, f"{linked} linked")
    found = await store.participant_by_name(TOURNAMENT, "ana")
    check("and lookup by name ignores case", found is not None)

    print("\ngenerated ids")
    proposal_id = await store.create_proposal(
        TOURNAMENT,
        1,
        proposer_id=big,
        responder_id=None,
        proposed_at=datetime.now(timezone.utc) + timedelta(hours=1),
    )
    check("an insert returns its new id", isinstance(proposal_id, int) and proposal_id > 0)
    check(
        "and the row is readable back",
        (await store.get_proposal(proposal_id)) is not None,
    )

    print("\nreminders and partial indexes")
    await store.schedule_reminder(
        TOURNAMENT, 1, "1h", datetime.now(timezone.utc) + timedelta(minutes=1)
    )
    await store.schedule_reminder(
        TOURNAMENT, 1, "1h", datetime.now(timezone.utc) + timedelta(minutes=2)
    )
    due = await store.due_reminders(datetime.now(timezone.utc) + timedelta(hours=1))
    mine = [r for r in due if int(r["tournament_id"]) == TOURNAMENT]
    check("the unique reminder index upserts rather than duplicating", len(mine) == 1)

    await cleanup(store)
    await store.close()

    print(f"\n{'-' * 60}\n{PASSED} passed, {FAILED} failed")
    if FAILED:
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
