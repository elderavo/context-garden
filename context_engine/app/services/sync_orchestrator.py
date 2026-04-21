"""Sync orchestrator — git fetch → mirror → reindex flow."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from ...infra.git.subprocess_git_client import SubprocessGitClient
from ...infra.indexer.daemon_indexer_service import DaemonIndexerService
from ...infra.mirror.mirror_service import NodeMirrorService
from ...infra.repo.workspace_json_repo import WorkspaceJsonRepository
from ..ports.git_client import GitClient
from ..ports.indexer_service import IndexerService
from ..ports.mirror_service import MirrorService
from ..ports.workspace_repository import WorkspaceRepository


@dataclass
class SyncOrchestrator:
    workspace_repository: WorkspaceRepository
    git_client: GitClient
    mirror_service: MirrorService
    indexer_service: IndexerService

    async def execute_sync(self, *, job: Any, log: Callable[[str], None]) -> None:
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
            ssh_key_file=entry.get("sshKeyFile"),
        )
        log("Fetch complete.")

        log("Mirroring...")
        mirror_error: str | None = None
        try:
            result = await self.mirror_service.run(workspace_entry=entry, gitlab_config=gitlab_config)
            written = sum(result.get("written", {}).values()) if isinstance(result.get("written"), dict) else 0
            log(f"Mirror complete. Notes written: {written}.")
        except Exception as exc:
            mirror_error = str(exc)
            log(f"Mirror failed (will still reindex existing notes): {exc}")

        if mirror_error:
            raise RuntimeError(f"Mirror failed: {mirror_error}")

    async def execute_rebuild(self, *, job: Any, log: Callable[[str], None]) -> None:
        """Force-regenerate all notes (bypasses mtime guard) then reindex."""
        entry = self.workspace_repository.get_by_id(job.workspace_id)
        if not entry:
            raise KeyError(f"Workspace {job.workspace_id} not found in registry")

        mirror_config = entry.get("gitlabConfig") or {"cloneDir": entry["sourceDir"]}

        log("Force-mirroring (bypassing mtime guard)...")
        try:
            result = await self.mirror_service.run(workspace_entry=entry, gitlab_config=mirror_config, force=True)
            written = sum(result.get("written", {}).values()) if isinstance(result.get("written"), dict) else 0
            log(f"Mirror complete. Notes written: {written}.")
        except Exception as exc:
            raise RuntimeError(f"Mirror failed: {exc}") from exc

        log("Triggering force reindex...")
        await self.indexer_service.trigger_reindex(workspace_name=job.workspace_name, force=True)
        log("Rebuild complete.")

    async def execute_index(self, *, job: Any, log: Callable[[str], None]) -> None:
        log("Triggering daemon reindex...")
        try:
            await self.indexer_service.trigger_reindex(workspace_name=job.workspace_name)
            log("Reindex complete.")
        except Exception as exc:
            log(f"Warning: reindex failed: {exc}")

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


def build_default(
    *,
    data_dir: Path,
    node_bin: str,
    mirror_cli: str,
    get_daemon: Callable[[], Any],
) -> SyncOrchestrator:
    return SyncOrchestrator(
        workspace_repository=WorkspaceJsonRepository(data_dir),
        git_client=SubprocessGitClient(),
        mirror_service=NodeMirrorService(
            data_dir=data_dir,
            node_bin=node_bin,
            mirror_cli=mirror_cli,
        ),
        indexer_service=DaemonIndexerService(get_daemon),
    )
