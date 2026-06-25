"""Data models used by Linkcat integration."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(slots=True)
class CheckoutItem:
    """Represents a checked out title."""

    title: str
    author: str | None = None
    image_url: str | None = None
    due_date: str | None = None


@dataclass(slots=True)
class HoldItem:
    """Represents a hold title."""

    title: str
    author: str | None = None
    image_url: str | None = None
    status: str | None = None
    ready: bool = False


@dataclass(slots=True)
class LinkcatAccountData:
    """Aggregated account data from Linkcat."""

    checkouts: list[CheckoutItem] = field(default_factory=list)
    holds: list[HoldItem] = field(default_factory=list)

    @property
    def checkout_count(self) -> int:
        return len(self.checkouts)

    @property
    def hold_count(self) -> int:
        return len(self.holds)

    @property
    def ready_hold_count(self) -> int:
        return sum(1 for item in self.holds if item.ready)
