from __future__ import annotations

import unittest
import uuid
import shutil
from pathlib import Path

from context_engine.app.services.sync_orchestrator import SyncOrchestrator as SyncOrchestratorLegacy


class _Repo:
    def __init__(self, entry):
        self.entry = entry

    def list_all(self):
        return [self.entry] if self.entry else []

    def get_by_id(self, workspace_id: str):
        if self.entry and self.entry.get("id") == workspace_id:
            return self.entry
        return None

    def save_all(self, entries):
        self.entry = entries[0] if entries else None


class _Git:
    def __init__(self):
        self.calls = []

    async def fetch_and_reset(self, *, project_url: str, clone_dir: str, branch: str, token: str | None, ssh_key_file: str | None = None) -> None:
        self.calls.append((project_url, clone_dir, branch, token))


class _Mirror:
    def __init__(self):
        self.calls = []

    async def run(self, *, workspace_entry, gitlab_config, force=False):
        self.calls.append((workspace_entry, gitlab_config, force))
        return {"written": {"python": 2}}


class _Indexer:
    def __init__(self):
        self.calls = []

    async def trigger_reindex(self, *, workspace_name: str, force: bool = False) -> None:
        self.calls.append((workspace_name, force))


class _Job:
    workspace_id = "ws-1"
    workspace_name = "alpha"


def _make_test_dir(prefix: str) -> Path:
    tmp_root = Path.cwd() / "ws-tests-sync-orchestrator"
    tmp_root.mkdir(parents=True, exist_ok=True)
    path = tmp_root / f"{prefix}{uuid.uuid4().hex[:8]}"
    path.mkdir(parents=True, exist_ok=False)
    return path


class SyncOrchestratorCharacterizationTests(unittest.IsolatedAsyncioTestCase):
    async def test_sync_flow_keeps_legacy_sequence(self) -> None:
        entry = {
            "id": "ws-1",
            "name": "alpha",
            "sourceType": "gitlab",
            "gitlabConfig": {
                "projectUrl": "https://gitlab.example.com/org/repo",
                "cloneDir": "/tmp/repo",
                "branch": "main",
                "accessToken": "abc123",
            },
        }
        repo = _Repo(entry)
        git = _Git()
        indexer = _Indexer()
        orchestrator = SyncOrchestratorLegacy(
            workspace_repository=repo,
            git_client=git,
            mirror_service=_Mirror(),
            indexer_service=indexer,
        )
        logs: list[str] = []

        await orchestrator.execute_sync(job=_Job(), log=logs.append)

        self.assertEqual(
            logs,
            [
                "Fetching https://gitlab.example.com/org/repo (main)...",
                "Fetch complete.",
                "Mirroring...",
                "Mirror complete. Notes written: 2.",
            ],
        )
        self.assertEqual(
            git.calls,
            [("https://gitlab.example.com/org/repo", "/tmp/repo", "main", "abc123")],
        )
        self.assertEqual(indexer.calls, [])

    async def test_rebuild_clears_workspace_generated_data_before_mirroring(self) -> None:
        data_dir = _make_test_dir("sync-orch-")
        try:
            entry = {
                "id": "ws-1",
                "name": "alpha",
                "sourceType": "local",
                "sourceDir": str(data_dir / "src"),
                "languages": ["py"],
            }
            mirror_note = data_dir / "md_db" / "code" / "alpha" / "old.md"
            mirror_note.parent.mkdir(parents=True, exist_ok=True)
            mirror_note.write_text("# old", "utf-8")

            cache_file = (
                data_dir
                / ".context-garden"
                / "knowledge_graph"
                / "ws-1"
                / "index"
                / "_notes_cache.json"
            )
            cache_file.parent.mkdir(parents=True, exist_ok=True)
            cache_file.write_text("{}", "utf-8")

            repo = _Repo(entry)
            mirror = _Mirror()
            indexer = _Indexer()
            orchestrator = SyncOrchestratorLegacy(
                workspace_repository=repo,
                git_client=_Git(),
                mirror_service=mirror,
                indexer_service=indexer,
                data_dir=data_dir,
            )
            logs: list[str] = []

            await orchestrator.execute_rebuild(job=_Job(), log=logs.append)

            self.assertTrue((data_dir / "md_db" / "code" / "alpha").is_dir())
            self.assertFalse(mirror_note.exists())
            self.assertFalse(cache_file.exists())
            self.assertTrue(cache_file.parent.is_dir())
            self.assertEqual(mirror.calls[0][2], False)
            self.assertEqual(indexer.calls, [("alpha", True)])
            self.assertIn("Clearing generated workspace data...", logs)
            self.assertIn("Mirroring from empty workspace data...", logs)
        finally:
            shutil.rmtree(data_dir, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
