from __future__ import annotations

import asyncio
import unittest

from graph.infra.queue.inproc_event_bus import InProcEventBus


class _Evt:
    def __init__(self, value: str) -> None:
        self.value = value


class InProcEventBusTests(unittest.IsolatedAsyncioTestCase):
    async def test_publish_is_serialized(self) -> None:
        bus = InProcEventBus()
        seen: list[str] = []

        async def handler(event: _Evt) -> None:
            await asyncio.sleep(0.01)
            seen.append(event.value)

        bus.subscribe(_Evt, handler)

        await asyncio.gather(
            bus.publish(_Evt("a")),
            bus.publish(_Evt("b")),
            bus.publish(_Evt("c")),
        )

        self.assertEqual(seen, ["a", "b", "c"])


if __name__ == "__main__":
    unittest.main()

