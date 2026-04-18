"""Legacy sync orchestration extracted behind service seam."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Protocol

from ..ports.git_client import GitClient
from ..ports.indexer_service import IndexerService
from ..ports.mirror_service import MirrorService
from ..ports.workspace_repository import WorkspaceRepository


class JobLike(Protocol):
    workspace_id: str
    workspace_name: str


@dataclass
class SyncOrchestratorLegacy:
    workspace_repository: WorkspaceRepository
    git_client: GitClient
    mirror_service: MirrorService
    indexer_service: IndexerService

    async def execute_sync(self, *, job: JobLike, log: Callable[[str], None]) -> None:
        entry = self.workspace_repository.get_by_id(job.workspace_id)
        if not entry:
            raise KeyError(f"Workspace {job.workspace_id} not found in registry")

        if entry.get("sourceType") != "gitlab" or not entry.get("gitlabConfig"):
            raise ValueError(f"Workspace \"{entry['name']}\" is not a GitLab workspace")

        gitlab_config = entry["gitlabConfig"]
        token = self._resolve_token(gitlab_config)

        log(f"Fetching {gitlab_config['projectUrl']} ({gitlab_config['branch']})...")
        await self.git_client.fetch_and_reset(
            project_url=gitlab_config["projectUrl"],
            clone_dir=gitlab_config["cloneDir"],
            branch=gitlab_config["branch"],
            token=token,
        )
        log("Fetch complete.")

        log("Mirroring...")
        result = await self.mirror_service.run(workspace_entry=entry, gitlab_config=gitlab_config)
        written = sum(result.get("written", {}).values()) if isinstance(result.get("written"), dict) else 0
        log(f"Mirror complete. Notes written: {written}.")

        await self._trigger_reindex(workspace_name=entry["name"], log=log)

    async def execute_index(self, *, job: JobLike, log: Callable[[str], None]) -> None:
        await self._trigger_reindex(workspace_name=job.workspace_name, log=log)

    async def _trigger_reindex(self, *, workspace_name: str, log: Callable[[str], None]) -> None:
        log("Triggering daemon reindex...")
        try:
            await self.indexer_service.trigger_reindex(workspace_name=workspace_name)
            log("Reindex queued.")
        except Exception as exc:
            log(f"Warning: reindex failed (daemon may be busy): {exc}")

    @staticmethod
    def _resolve_token(gitlab_config: dict[str, str]) -> str | None:
        if gitlab_config.get("accessToken"):
            return gitlab_config["accessToken"]
        if os.environ.get("CG_GITLAB_TOKEN"):
            return os.environ["CG_GITLAB_TOKEN"]

        env_path = Path.home() / ".context-garden" / ".env"
        if env_path.exists():
            for line in env_path.read_text("utf-8").splitlines():
                stripped = line.strip()
                if stripped.startswith("CG_GITLAB_TOKEN="):
                    value = stripped[len("CG_GITLAB_TOKEN="):].strip()
                    return value or None
        return None

