"""Sync orchestrator — git fetch → mirror → reindex flow."""

from __future__ import annotations

import os
import shutil
from datetime import datetime, timezone
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from ...infra.git.subprocess_git_client import SubprocessGitClient
from ...infra.indexer.daemon_indexer_service import DaemonIndexerService
from ...infra.mirror.code_note_summarizer import CodeNoteSummarizer
from ...infra.mirror.mirror_service import NodeMirrorService
from ...infra.repo.workspace_json_repo import WorkspaceJsonRepository
from ..ports.git_client import GitClient
from ..ports.indexer_service import IndexerService
from ..ports.mirror_service import MirrorService
from ..ports.workspace_repository import WorkspaceRepository
from . import workspace_service


@dataclass
class SyncOrchestrator:
    workspace_repository: WorkspaceRepository
    git_client: GitClient
    mirror_service: MirrorService
    indexer_service: IndexerService
    note_summarizer: CodeNoteSummarizer | None = None
    data_dir: Path | None = None

    async def execute_sync(self, *, job: Any, log: Callable[[str], None]) -> None:
        entry = self.workspace_repository.get_by_id(job.workspace_id)
        if not entry:
            raise KeyError(f"Workspace {job.workspace_id} not found in registry")

        is_gitlab = entry.get("sourceType") == "gitlab" and bool(entry.get("gitlabConfig"))
        gitlab_config = entry.get("gitlabConfig") if is_gitlab else None
        mirror_config = gitlab_config or {"cloneDir": entry["sourceDir"]}
        indexing_paused = False
        synced_commit = ""
        workspace_service.update_workspace_status(
            workspace_repository=self.workspace_repository,
            workspace_id=entry["id"],
            patch={
                "lastSyncStartedAt": datetime.now(timezone.utc).isoformat(),
                "lastSyncCompletedAt": None,
                "lastSyncStatus": "running",
                "lastSyncError": "",
            },
        )

        try:
            log("Pausing watcher-driven indexing...")
            await self.indexer_service.pause_indexing(workspace_id=entry["id"])
            indexing_paused = True

            if is_gitlab:
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
                synced_commit = workspace_service.resolve_workspace_clone_commit(entry)
                workspace_service.update_workspace_status(
                    workspace_repository=self.workspace_repository,
                    workspace_id=entry["id"],
                    patch={"currentCloneCommit": synced_commit},
                )
            else:
                log(f"Local workspace — scanning {entry['sourceDir']} for changes...")

            log("Mirroring...")
            result = await self.mirror_service.run(workspace_entry=entry, gitlab_config=mirror_config)
            written = sum(result.get("written", {}).values()) if isinstance(result.get("written"), dict) else 0
            log(f"Mirror complete. Notes written: {written}.")

            if self.note_summarizer is not None:
                log("Summarizing mirrored notes...")
                summary_result = await self.note_summarizer.summarize_workspace(
                    workspace_name=entry["name"],
                    log_fn=log,
                )
                summarized = sum(summary_result.get("summarized", {}).values())
                skipped = sum(summary_result.get("skipped", {}).values())
                log(f"Summary stage complete. Summaries written: {summarized}. Skipped: {skipped}.")
        except Exception as exc:
            workspace_service.update_workspace_status(
                workspace_repository=self.workspace_repository,
                workspace_id=entry["id"],
                patch={
                    "lastSyncCompletedAt": datetime.now(timezone.utc).isoformat(),
                    "lastSyncStatus": "failed",
                    "lastSyncError": str(exc),
                    "currentCloneCommit": synced_commit or workspace_service.resolve_workspace_clone_commit(entry),
                },
            )
            raise
        finally:
            if indexing_paused:
                log("Resuming watcher-driven indexing...")
                await self.indexer_service.resume_indexing(workspace_id=entry["id"])

        log("Triggering post-sync reindex...")
        await self.indexer_service.trigger_reindex(workspace_name=entry["name"])
        log("Post-sync reindex complete.")
        status_patch: dict[str, Any] = {
            "lastSyncCompletedAt": datetime.now(timezone.utc).isoformat(),
            "lastSyncStatus": "done",
            "lastSyncError": "",
        }
        if synced_commit:
            status_patch["lastSyncedCommit"] = synced_commit
            status_patch["currentCloneCommit"] = synced_commit
        workspace_service.update_workspace_status(
            workspace_repository=self.workspace_repository,
            workspace_id=entry["id"],
            patch=status_patch,
        )

    async def execute_rebuild(self, *, job: Any, log: Callable[[str], None]) -> None:
        """Delete generated workspace data, then regenerate notes and index."""
        entry = self.workspace_repository.get_by_id(job.workspace_id)
        if not entry:
            raise KeyError(f"Workspace {job.workspace_id} not found in registry")

        mirror_config = entry.get("gitlabConfig") or {"cloneDir": entry["sourceDir"]}
        indexing_paused = False

        try:
            log("Pausing watcher-driven indexing...")
            await self.indexer_service.pause_indexing(workspace_id=entry["id"])
            indexing_paused = True

            if self.data_dir is not None:
                self._reset_generated_workspace_data(entry=entry, log=log)

            log("Mirroring from empty workspace data...")
            result = await self.mirror_service.run(workspace_entry=entry, gitlab_config=mirror_config)
            written = sum(result.get("written", {}).values()) if isinstance(result.get("written"), dict) else 0
            log(f"Mirror complete. Notes written: {written}.")

            if self.note_summarizer is not None:
                log("Summarizing mirrored notes...")
                summary_result = await self.note_summarizer.summarize_workspace(
                    workspace_name=entry["name"],
                    log_fn=log,
                )
                summarized = sum(summary_result.get("summarized", {}).values())
                skipped = sum(summary_result.get("skipped", {}).values())
                log(f"Summary stage complete. Summaries written: {summarized}. Skipped: {skipped}.")
        finally:
            if indexing_paused:
                log("Resuming watcher-driven indexing...")
                await self.indexer_service.resume_indexing(workspace_id=entry["id"])

        log("Triggering force reindex...")
        await self.indexer_service.trigger_reindex(workspace_name=job.workspace_name, force=True)
        log("Rebuild complete.")
        current_commit = workspace_service.resolve_workspace_clone_commit(entry)
        if current_commit:
            workspace_service.update_workspace_status(
                workspace_repository=self.workspace_repository,
                workspace_id=entry["id"],
                patch={"currentCloneCommit": current_commit},
            )

    def _reset_generated_workspace_data(self, *, entry: dict[str, Any], log: Callable[[str], None]) -> None:
        """Clear generated markdown and persisted index/cache for one workspace."""
        assert self.data_dir is not None
        data_dir = self.data_dir.resolve()
        workspace_name = entry["name"]
        workspace_id = entry["id"]

        mirror_dir = data_dir / "md_db" / "code" / workspace_name
        index_root = data_dir / ".context-garden" / "knowledge_graph" / workspace_id

        log("Clearing generated workspace data...")
        self._clear_directory_contents(mirror_dir, data_dir)
        if index_root.exists():
            self._remove_path_under_data_dir(index_root, data_dir)
        (index_root / "index").mkdir(parents=True, exist_ok=True)

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

    @staticmethod
    def _clear_directory_contents(path: Path, data_dir: Path) -> None:
        resolved = path.resolve()
        SyncOrchestrator._assert_under_data_dir(resolved, data_dir)
        resolved.mkdir(parents=True, exist_ok=True)
        for child in resolved.iterdir():
            SyncOrchestrator._remove_path_under_data_dir(child, data_dir)

    @staticmethod
    def _remove_path_under_data_dir(path: Path, data_dir: Path) -> None:
        resolved = path.resolve()
        SyncOrchestrator._assert_under_data_dir(resolved, data_dir)
        if resolved.is_dir() and not resolved.is_symlink():
            shutil.rmtree(resolved)
        else:
            resolved.unlink(missing_ok=True)

    @staticmethod
    def _assert_under_data_dir(path: Path, data_dir: Path) -> None:
        try:
            path.relative_to(data_dir)
        except ValueError as exc:
            raise RuntimeError(f"Refusing to delete path outside data dir: {path}") from exc


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
        note_summarizer=CodeNoteSummarizer(data_dir=data_dir),
        data_dir=data_dir,
    )
