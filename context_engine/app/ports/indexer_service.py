from __future__ import annotations

from typing import Protocol


class IndexerService(Protocol):
    """Indexer enqueue/trigger operations for sync workflows."""

    async def trigger_reindex(self, *, workspace_name: str, force: bool = False) -> None:
        ...

    async def pause_indexing(self, *, workspace_id: str) -> None:
        ...

    async def resume_indexing(self, *, workspace_id: str) -> None:
        ...
