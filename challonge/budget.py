"""Challonge API request budgeting and usage tracking."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Protocol

REASONS = frozenset(
    {"admin:post", "admin:sync", "admin:report", "autosync", "standings"}
)
ADMIN_REASONS = frozenset(r for r in REASONS if r.startswith("admin:"))


def current_month() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m")


def current_day() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


class UsageStore(Protocol):
    async def get_usage(self, month: str) -> int: ...
    async def bump_usage(self, month: str, amount: int = 1) -> int: ...
    async def bump_usage_day(self, day: str, reason: str) -> None: ...
    async def usage_by_reason(self, day: str) -> dict[str, int]: ...


class BudgetExhausted(RuntimeError):
    """Raised instead of issuing a read that would blow the monthly quota."""

    def __init__(self, used: int, limit: int) -> None:
        self.used = used
        self.limit = limit
        super().__init__(
            f"Challonge request budget nearly exhausted ({used}/{limit} this "
            "month). Reads are paused until next month, or until "
            "CHALLONGE_MONTHLY_BUDGET is raised after upgrading the plan."
        )


class UnknownReason(ValueError):
    """A call site tried to spend without declaring a recognised reason."""

    def __init__(self, reason: str) -> None:
        super().__init__(
            f"{reason!r} is not a known request reason. Add it to "
            "challonge.budget.REASONS deliberately, or route the call through "
            "an admin path that already has one."
        )


@dataclass(frozen=True)
class BudgetStatus:
    used: int
    limit: int
    today: dict[str, int] = field(default_factory=dict)

    @property
    def remaining(self) -> int:
        return max(0, self.limit - self.used)

    @property
    def used_today(self) -> int:
        return sum(self.today.values())

    @property
    def fraction(self) -> float:
        return self.used / self.limit if self.limit else 1.0

    def bar(self, width: int = 20) -> str:
        filled = min(width, int(self.fraction * width))
        return "#" * filled + "." * (width - filled)

    def breakdown(self) -> str:
        """Today's spend, by who caused it."""
        if not self.today:
            return "nothing today"
        return ", ".join(
            f"{count} {reason}"
            for reason, count in sorted(
                self.today.items(), key=lambda kv: (-kv[1], kv[0])
            )
        )


class Budget:
    """Counts API calls by reason and enforces the monthly cut-off."""

    def __init__(
        self,
        store: UsageStore,
        limit: int = 500,
        warn_fraction: float = 0.8,
        read_cutoff_fraction: float = 0.98,
    ) -> None:
        self._store = store
        self.limit = limit
        self.warn_fraction = warn_fraction
        self.read_cutoff_fraction = read_cutoff_fraction
        self._warned_month: str | None = None

    async def status(self) -> BudgetStatus:
        return BudgetStatus(
            used=await self._store.get_usage(current_month()),
            limit=self.limit,
            today=await self._store.usage_by_reason(current_day()),
        )

    async def reserve(self, reason: str) -> None:
        """Called immediately before a request. Raises if it is not affordable."""
        if reason not in REASONS:
            raise UnknownReason(reason)
        used = await self._store.get_usage(current_month())
        if used >= self.limit * self.read_cutoff_fraction:
            raise BudgetExhausted(used, self.limit)

    async def record(self, reason: str) -> int:
        """Called after a request completes, successfully or not: it counted."""
        await self._store.bump_usage_day(current_day(), reason)
        return await self._store.bump_usage(current_month(), 1)

    async def take_warning(self) -> BudgetStatus | None:
        """Return a status once per month when usage first crosses the warn line.

        Callers surface this to the admins. Returns ``None`` when there is
        nothing new to say, so it is cheap to call after every request.
        """
        status = await self.status()
        month = current_month()
        if self._warned_month == month:
            return None
        if status.fraction >= self.warn_fraction:
            self._warned_month = month
            return status
        return None
