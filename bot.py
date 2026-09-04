"""Tournament bot main entry point."""

from __future__ import annotations

import asyncio
import logging

import aiosqlite
import discord
from discord.ext import commands

from challonge.budget import Budget
from challonge.client import ChallongeClient
from config import Config, ConfigError
from db.store import Store
from nullrush import RelayClient

log = logging.getLogger("tournamentbot")

COGS = (
    "cogs.tournament",
    "cogs.threads",
    "cogs.scheduling",
    "cogs.sync",
)


class TournamentBot(commands.Bot):
    def __init__(self, config: Config) -> None:
        intents = discord.Intents.default()
        super().__init__(command_prefix=commands.when_mentioned, intents=intents)

        self.config = config
        self.store = Store(
            config.db_path,
            config.supabase_url,
            config.supabase_key,
            config.database_schema,
        )
        self.budget = Budget(self.store, limit=config.monthly_budget)
        self.challonge = ChallongeClient(config.challonge_api_key, self.budget)
        self.relay = RelayClient(config.relay_url, config.relay_token)

    async def setup_hook(self) -> None:
        await self.store.connect()
        log.info("storage: %s", self.store.dialect)

        if self.config.port:
            import web

            self._web_runner = await web.start(
                self, self.config.port, self.config.bot_host
            )

        from ui.views import (
            EscalationView,
            MatchThreadView,
            PanelView,
            ProposalView,
            ResultView,
        )

        for view in (
            PanelView(self),
            MatchThreadView(self),
            ProposalView(self),
            EscalationView(self),
            ResultView(self),
        ):
            self.add_view(view)

        for cog in COGS:
            await self.load_extension(cog)
            log.info("loaded %s", cog)

        if self.config.guild_id:
            guild = discord.Object(id=self.config.guild_id)
            self.tree.copy_global_to(guild=guild)
            await self.tree.sync(guild=guild)
            log.info("synced commands to guild %s", self.config.guild_id)
        else:
            await self.tree.sync()
            log.info("synced commands globally (may take up to an hour)")

    async def close(self) -> None:
        runner = getattr(self, "_web_runner", None)
        if runner is not None:
            await runner.cleanup()
        await self.challonge.aclose()
        await self.relay.aclose()
        await self.store.close()
        await super().close()

    # ------------------------------------------------------------- refresh

    async def refresh_matches(
        self, tournament: aiosqlite.Row, *, reason: str
    ) -> tuple[list[aiosqlite.Row], list[aiosqlite.Row]]:
        """Pull the bracket from Challonge and diff it against the cache."""
        from services import refresh_matches

        return await refresh_matches(self, tournament, reason=reason)

    # -------------------------------------------------------- error surface

    async def on_error(self, event: str, *args: object, **kwargs: object) -> None:
        log.exception("unhandled error in %s", event)


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s: %(message)s",
    )
    logging.getLogger("discord").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)

    try:
        config = Config.load()
    except ConfigError as exc:
        raise SystemExit(f"Configuration error: {exc}")

    bot = TournamentBot(config)

    @bot.tree.error
    async def on_app_command_error(
        interaction: discord.Interaction, error: Exception
    ) -> None:
        from cogs.common import report_error

        await report_error(interaction, error)

    async with bot:
        await bot.start(config.discord_token)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
