from __future__ import annotations

from typing import Any, Awaitable, Callable, Protocol, TypeVar

TEvent = TypeVar("TEvent")


class EventBus(Protocol):
    """In-process event bus interface."""

    def subscribe(self, event_type: type[TEvent], handler: Callable[[TEvent], Awaitable[None] | None]) -> None:
        ...

    async def publish(self, event: Any) -> None:
        ...

