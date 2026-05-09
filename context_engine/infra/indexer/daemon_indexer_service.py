from __future__ import annotations

import asyncio
from typing import Any, Callable


class DaemonIndexerService:
    """Adapter that triggers reindexing through the daemon handler."""

    def __init__(self, get_daemon: Callable[[], Any]) -> None:
        self._get_daemon = get_daemon

    async def trigger_reindex(self, *, workspace_name: str, force: bool = False) -> None:
        daemon = self._get_daemon()
        await asyncio.to_thread(daemon.handle_index_rebuild, {"name": workspace_name, "force": force})

    async def pause_indexing(self, *, workspace_id: str) -> None:
        daemon = self._get_daemon()
        await asyncio.to_thread(daemon.handle_index_pause, {"workspace_id": workspace_id})

    async def resume_indexing(self, *, workspace_id: str) -> None:
        daemon = self._get_daemon()
        await asyncio.to_thread(daemon.handle_index_resume, {"workspace_id": workspace_id})
