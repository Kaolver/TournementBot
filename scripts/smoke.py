"""Live contract check against the Challonge API."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402
from dotenv import load_dotenv  # noqa: E402

load_dotenv()

BASE_URL = "https://api.challonge.com/v2.1"
LOG_PATH = Path(__file__).resolve().parent.parent / "smoke_log.txt"

request_count = 0


def log(text: str) -> None:
    print(text)
    with LOG_PATH.open("a", encoding="utf-8") as handle:
        handle.write(text + "\n")


async def call(
    client: httpx.AsyncClient,
    method: str,
    path: str,
    *,
    body: dict | None = None,
    params: dict | None = None,
) -> object:
    global request_count
    request_count += 1

    log(f"\n{'=' * 70}\n[{request_count}] {method} {path}")
    if body:
        log("--- request body ---\n" + json.dumps(body, indent=2))

    response = await client.request(method, path, json=body, params=params)
    log(f"--- status: {response.status_code} ---")

    try:
        payload = response.json()
        log("--- response ---\n" + json.dumps(payload, indent=2)[:6000])
    except ValueError:
        payload = None
        log("--- response (non-JSON) ---\n" + response.text[:2000])

    if response.status_code >= 400:
        raise SystemExit(f"Request failed with {response.status_code}; see above.")
    return payload


def resource_id(payload: object) -> int:
    data = payload.get("data") if isinstance(payload, dict) else None
    if isinstance(data, list):
        data = data[0]
    if not isinstance(data, dict):
        raise SystemExit(f"Could not find an id in: {payload!r}")
    return int(data["id"])


async def main() -> None:
    api_key = os.getenv("CHALLONGE_API_KEY", "").strip()
    if not api_key:
        raise SystemExit("CHALLONGE_API_KEY is not set. Copy .env.example to .env.")

    cleanup = "--cleanup" in sys.argv
    slug = f"smoketest_{int(datetime.now(timezone.utc).timestamp())}"

    LOG_PATH.write_text(
        f"Challonge smoke test {datetime.now(timezone.utc).isoformat()}\n",
        encoding="utf-8",
    )

    headers = {
        "Content-Type": "application/vnd.api+json",
        "Accept": "application/json",
        "Authorization-Type": "v1",
        "Authorization": api_key,
        "User-Agent": "TournamentBot smoke test",
    }

    async with httpx.AsyncClient(
        base_url=BASE_URL, headers=headers, timeout=30.0
    ) as client:
        created = await call(
            client,
            "POST",
            "/tournaments.json",
            body={
                "data": {
                    "type": "tournament",
                    "attributes": {
                        "name": "Bot smoke test (safe to delete)",
                        "url": slug,
                        "tournament_type": "single elimination",
                        "private": True,
                        "description": "Automated contract check.",
                    },
                }
            },
        )
        tournament_id = resource_id(created)
        log(f"\n>>> tournament id: {tournament_id}")

        # Bulk add is what the bot uses at start: confirm the envelope shape.
        await call(
            client,
            "POST",
            f"/tournaments/{tournament_id}/participants/bulk_add.json",
            body={
                "data": {
                    "type": "Participants",
                    "attributes": {
                        "participants": [
                            {"name": "Smoke Alice", "misc": "111111111111111111"},
                            {"name": "Smoke Bob", "misc": "222222222222222222"},
                        ]
                    },
                }
            },
        )

        participants = await call(
            client, "GET", f"/tournaments/{tournament_id}/participants.json"
        )

        await call(
            client,
            "PUT",
            f"/tournaments/{tournament_id}/change_state.json",
            body={
                "data": {
                    "type": "TournamentState",
                    "attributes": {"state": "start"},
                }
            },
        )

        matches = await call(
            client, "GET", f"/tournaments/{tournament_id}/matches.json"
        )
        match_id = resource_id(matches)
        winner_id = resource_id(participants)
        log(f"\n>>> reporting match {match_id}, winner {winner_id}")

        await call(
            client,
            "PUT",
            f"/tournaments/{tournament_id}/matches/{match_id}.json",
            body={
                "data": {
                    "type": "match",
                    "attributes": {"scores_csv": "2-1", "winner_id": winner_id},
                }
            },
        )

        # The read the bot performs after every write, to learn what opened.
        await call(client, "GET", f"/tournaments/{tournament_id}/matches.json")

        await call(
            client,
            "PUT",
            f"/tournaments/{tournament_id}/change_state.json",
            body={
                "data": {
                    "type": "TournamentState",
                    "attributes": {"state": "finalize"},
                }
            },
        )

        await call(client, "GET", f"/tournaments/{tournament_id}/participants.json")

        if cleanup:
            await call(client, "DELETE", f"/tournaments/{tournament_id}.json")
            log("\n>>> tournament deleted")
        else:
            log(
                f"\n>>> kept: https://challonge.com/{slug}"
                "\n>>> re-run with --cleanup to delete it"
            )

    log(f"\n{'=' * 70}\nDone. {request_count} requests used. Full log: {LOG_PATH}")


if __name__ == "__main__":
    asyncio.run(main())
