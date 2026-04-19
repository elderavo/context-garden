"""V2 sync orchestrator (Stage 1 delegates to legacy)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from .sync_orchestrator_legacy import JobLike
from .sync_orchestrator_legacy import SyncOrchestratorLegacy


@dataclass
class SyncOrchestratorV2:
    legacy: SyncOrchestratorLegacy

    async def execute_sync(self, *, job: JobLike, log: Callable[[str], None]) -> None:
        await self.legacy.execute_sync(job=job, log=log)

    async def execute_index(self, *, job: JobLike, log: Callable[[str], None]) -> None:
        await self.legacy.execute_index(job=job, log=log)

