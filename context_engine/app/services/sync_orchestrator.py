"""Active sync orchestrator adapter selecting legacy/v2 implementation."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Callable, Protocol

from ...infra.git.subprocess_git_client import SubprocessGitClient
from ...infra.indexer.daemon_indexer_service import DaemonIndexerService
from ...infra.mirror.mirror_service_legacy import MirrorServiceLegacy
from ...infra.repo.workspace_json_repo import WorkspaceJsonRepository
from .sync_orchestrator_legacy import SyncOrchestratorLegacy
from .sync_orchestrator_v2 import SyncOrchestratorV2

_IMPL_ENV = "CG_SYNC_ORCHESTRATOR_IMPL"


class SyncOrchestrator(Protocol):
    async def execute_sync(self, *, job: Any, log: Callable[[str], None]) -> None:
        ...

    async def execute_index(self, *, job: Any, log: Callable[[str], None]) -> None:
        ...


def _use_v2() -> bool:
    return os.environ.get(_IMPL_ENV, "legacy").strip().lower() == "v2"


def build_default(
    *,
    data_dir: Path,
    node_bin: str,
    mirror_cli: str,
    get_daemon: Callable[[], Any],
) -> SyncOrchestrator:
    workspace_repository = WorkspaceJsonRepository(data_dir)
    git_client = SubprocessGitClient()
    mirror_service = MirrorServiceLegacy(
        data_dir=data_dir,
        node_bin=node_bin,
        mirror_cli=mirror_cli,
    )
    indexer_service = DaemonIndexerService(get_daemon)

    legacy = SyncOrchestratorLegacy(
        workspace_repository=workspace_repository,
        git_client=git_client,
        mirror_service=mirror_service,
        indexer_service=indexer_service,
    )
    if _use_v2():
        return SyncOrchestratorV2(legacy=legacy)
    return legacy

