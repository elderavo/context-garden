from __future__ import annotations

import unittest

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

    async def fetch_and_reset(self, *, project_url: str, clone_dir: str, branch: str, token: str | None) -> None:
        self.calls.append((project_url, clone_dir, branch, token))


class _Mirror:
    async def run(self, *, workspace_entry, gitlab_config):
        return {"written": {"python": 2}}


class _Indexer:
    def __init__(self):
        self.calls = []

    async def trigger_reindex(self, *, workspace_name: str) -> None:
        self.calls.append(workspace_name)


class _Job:
    workspace_id = "ws-1"
    workspace_name = "alpha"


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
                "Triggering daemon reindex...",
                "Reindex queued.",
            ],
        )
        self.assertEqual(
            git.calls,
            [("https://gitlab.example.com/org/repo", "/tmp/repo", "main", "abc123")],
        )
        self.assertEqual(indexer.calls, ["alpha"])


if __name__ == "__main__":
    unittest.main()

