"""HTTP listener for health checks and relay webhook events."""

from __future__ import annotations

import logging
import math
from datetime import datetime, timezone

from aiohttp import web

log = logging.getLogger(__name__)

STARTED_AT = datetime.now(timezone.utc)


def build_app(bot) -> web.Application:
    app = web.Application()

    async def health(_request: web.Request) -> web.Response:
        """Health check endpoint."""
        uptime = (datetime.now(timezone.utc) - STARTED_AT).total_seconds()
        latency = getattr(bot, "latency", None)
        payload = {
            "ok": bot.is_ready(),
            "uptime_seconds": round(uptime),
            "latency_ms": (
                round(latency * 1000)
                if latency is not None and math.isfinite(latency)
                else None
            ),
            "guilds": len(bot.guilds),
        }

        try:
            status = await bot.budget.status()
            payload["challonge"] = {
                "used_this_month": status.used,
                "limit": status.limit,
                "today": status.today,
            }
        except Exception:  # noqa: BLE001
            payload["challonge"] = None

        return web.json_response(payload)

    async def match_result(request: web.Request) -> web.Response:
        """Relay match result webhook handler."""
        secret = bot.config.relay_webhook_secret
        if not secret or request.headers.get("x-relay-secret") != secret:
            return web.json_response({"error": "bad secret"}, status=401)

        try:
            payload = await request.json()
        except Exception:  # noqa: BLE001
            return web.json_response({"error": "body is not JSON"}, status=400)

        code = str(payload.get("code") or "").strip()
        if not code:
            return web.json_response({"error": "code is required"}, status=400)

        bot.dispatch("relay_result", code, payload)
        return web.json_response({"ok": True})

    app.router.add_get("/", health)
    app.router.add_get("/health", health)
    app.router.add_post("/relay/result", match_result)
    return app


async def start(bot, port: int, host: str = "127.0.0.1") -> web.AppRunner:
    runner = web.AppRunner(build_app(bot))
    await runner.setup()
    await web.TCPSite(runner, host, port).start()
    log.info("listening on %s:%s", host, port)
    return runner
