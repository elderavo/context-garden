from __future__ import annotations

import asyncio
import inspect
from collections import defaultdict
from typing import Any, Awaitable, Callable


class InProcEventBus:
    """Serialized in-process pub/sub bus."""

    def __init__(self) -> None:
        self._handlers: dict[type[Any], list[Callable[[Any], Awaitable[None] | None]]] = defaultdict(list)
        self._publish_lock = asyncio.Lock()

    def subscribe(self, event_type: type[Any], handler: Callable[[Any], Awaitable[None] | None]) -> None:
        self._handlers[event_type].append(handler)

    async def publish(self, event: Any) -> None:
        async with self._publish_lock:
            for event_type, handlers in self._handlers.items():
                if isinstance(event, event_type):
                    for handler in handlers:
                        result = handler(event)
                        if inspect.isawaitable(result):
                            await result

