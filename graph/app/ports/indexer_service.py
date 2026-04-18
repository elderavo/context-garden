from __future__ import annotations

from typing import Protocol


class IndexerService(Protocol):
    """Indexer enqueue/trigger operations for sync workflows."""

    async def trigger_reindex(self, *, workspace_name: str) -> None:
        ...

