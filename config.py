"""Environment-backed configuration."""

from __future__ import annotations

import os
from dataclasses import dataclass

from dotenv import load_dotenv

load_dotenv()


class ConfigError(RuntimeError):
    pass


def _require(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise ConfigError(
            f"{name} is not set. Copy .env.example to .env and fill it in."
        )
    return value


@dataclass(frozen=True)
class Config:
    discord_token: str
    challonge_api_key: str
    guild_id: int | None
    db_path: str
    supabase_url: str | None
    supabase_key: str | None
    database_schema: str
    port: int | None
    bot_host: str
    monthly_budget: int
    relay_url: str | None
    relay_token: str | None
    relay_webhook_secret: str | None

    @classmethod
    def load(cls) -> "Config":
        raw_guild = os.getenv("GUILD_ID", "").strip()
        return cls(
            discord_token=_require("DISCORD_TOKEN"),
            challonge_api_key=_require("CHALLONGE_API_KEY"),
            guild_id=int(raw_guild) if raw_guild else None,
            db_path=os.getenv("DB_PATH", "tournament.db").strip() or "tournament.db",
            # The same two variables the relay's leaderboard uses, so the whole
            # project has one credential pair. Unset means SQLite, which is what
            # you want locally.
            supabase_url=os.getenv("SUPABASE_URL", "").strip() or None,
            supabase_key=(
                os.getenv("SUPABASE_SECRET_KEY")
                or os.getenv("SUPABASE_SERVICE_ROLE_KEY")
                or os.getenv("SUPABASE_KEY")
                or ""
            ).strip()
            or None,
            # Our own corner of the Supabase project, so the bot's tables and
            # the game's leaderboard cannot collide.
            database_schema=os.getenv("DATABASE_SCHEMA", "tournamentbot").strip()
            or "tournamentbot",
            # BOT_PORT when the bot shares a container with the relay, which
            # owns PORT because the platform routes to exactly one. PORT alone
            # when the bot is its own service. Neither set means no listener at
            # all, which is what you want on a home machine.
            port=(
                int(os.getenv("BOT_PORT") or os.getenv("PORT"))
                if (os.getenv("BOT_PORT") or os.getenv("PORT", "")).strip()
                else None
            ),
            # Loopback by default: co-hosted with the relay the webhook never
            # leaves the machine, and nothing else needs to reach the bot. Set
            # 0.0.0.0 only if the bot is its own public service.
            bot_host=os.getenv("BOT_HOST", "127.0.0.1").strip() or "127.0.0.1",
            monthly_budget=int(os.getenv("CHALLONGE_MONTHLY_BUDGET", "500")),
            # The Null Rush relay. Without these the bot simply never offers to
            # set up an in-game match; everything else works unchanged.
            relay_url=os.getenv("RELAY_URL", "").strip().rstrip("/") or None,
            relay_token=os.getenv("RELAY_TOKEN", "").strip() or None,
            relay_webhook_secret=os.getenv("RELAY_WEBHOOK_SECRET", "").strip() or None,
        )
