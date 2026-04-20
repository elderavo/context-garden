from __future__ import annotations

import shutil
import unittest
import uuid
from pathlib import Path

from context_engine.app.services import workspace_service


class _Repo:
    def __init__(self) -> None:
        self.entries: list[dict] = []

    def list_all(self):
        return list(self.entries)

    def get_by_id(self, workspace_id: str):
        return next((entry for entry in self.entries if entry["id"] == workspace_id), None)

    def save_all(self, entries):
        self.entries = list(entries)


class _Git:
    def __init__(self) -> None:
        self.clone_calls = []

    async def clone_repo(self, *, project_url: str, clone_dir: str, branch: str, token: str | None):
        self.clone_calls.append((project_url, clone_dir, branch, token))
        Path(clone_dir).mkdir(parents=True, exist_ok=True)

    async def fetch_and_reset(self, *, project_url: str, clone_dir: str, branch: str, token: str | None):
        return None


class _Mirror:
    def __init__(self) -> None:
        self.calls = []

    async def run(self, *, workspace_entry, gitlab_config):
        self.calls.append((workspace_entry, gitlab_config))
        md_root = Path(workspace_entry["sourceDir"])
        md_root.mkdir(parents=True, exist_ok=True)
        return {"written": {"python": 2}}


def _make_test_dir(prefix: str) -> Path:
    tmp_root = Path.cwd() / "ws-tests-workspace-service"
    tmp_root.mkdir(parents=True, exist_ok=True)
    path = tmp_root / f"{prefix}{uuid.uuid4().hex[:8]}"
    path.mkdir(parents=True, exist_ok=False)
    return path


class WorkspaceServiceTests(unittest.IsolatedAsyncioTestCase):
    async def test_register_gitlab_workspace_creates_entry_and_clones(self) -> None:
        data_dir = _make_test_dir("ws-service-")
        try:
            repo = _Repo()
            git = _Git()
            mirror = _Mirror()

            result = await workspace_service.register_workspace(
                payload={
                    "name": "alpha-workspace",
                    "gitlab_url": "https://gitlab.example.com/org/repo",
                    "gitlab_branch": "main",
                    "languages": ["py"],
                },
                data_dir=data_dir,
                workspace_repository=repo,
                git_client=git,
                mirror_service=mirror,
            )

            self.assertEqual(result.entry["name"], "alpha-workspace")
            self.assertEqual(result.entry["sourceType"], "gitlab")
            self.assertIn("gitlabConfig", result.entry)
            self.assertEqual(result.notes_generated, 2)
            self.assertEqual(len(repo.entries), 1)
            self.assertEqual(len(git.clone_calls), 1)
            self.assertTrue(result.entry["gitlabConfig"]["cloneDir"].endswith("alpha-workspace"))
            self.assertEqual(len(result.entry["gitlabConfig"]["webhookSecret"]), 64)
        finally:
            shutil.rmtree(data_dir, ignore_errors=True)

    async def test_register_local_workspace_requires_existing_source_dir(self) -> None:
        data_dir = _make_test_dir("ws-service-")
        try:
            repo = _Repo()
            git = _Git()
            mirror = _Mirror()

            source_dir = data_dir / "src"
            source_dir.mkdir(parents=True, exist_ok=True)

            result = await workspace_service.register_workspace(
                payload={
                    "name": "local-workspace",
                    "source_dir": str(source_dir),
                    "languages": ["ts"],
                },
                data_dir=data_dir,
                workspace_repository=repo,
                git_client=git,
                mirror_service=mirror,
            )

            self.assertEqual(result.entry["sourceType"], "local")
            self.assertEqual(result.entry["sourceDir"], str(source_dir.resolve()))
            self.assertEqual(len(git.clone_calls), 0)
        finally:
            shutil.rmtree(data_dir, ignore_errors=True)

    async def test_unregister_workspace_removes_registry_and_paths(self) -> None:
        data_dir = _make_test_dir("ws-service-")
        try:
            repo = _Repo()
            entry = {
                "id": "ws-1",
                "name": "alpha",
                "sourceType": "gitlab",
                "sourceDir": str(data_dir / ".context-garden" / "clones" / "alpha"),
                "languages": ["py"],
                "active": True,
                "registeredAt": "2026-01-01T00:00:00+00:00",
                "gitlabConfig": {
                    "cloneDir": str(data_dir / ".context-garden" / "clones" / "alpha"),
                },
            }
            repo.entries = [entry]

            mirror_note = data_dir / "md_db" / "code" / "alpha" / "a.md"
            mirror_note.parent.mkdir(parents=True, exist_ok=True)
            mirror_note.write_text("# note", "utf-8")
            clone_dir = Path(entry["gitlabConfig"]["cloneDir"])
            clone_dir.mkdir(parents=True, exist_ok=True)

            result = workspace_service.unregister_workspace(
                workspace_id="ws-1",
                data_dir=data_dir,
                workspace_repository=repo,
            )

            self.assertIsNotNone(result.removed)
            self.assertEqual(len(repo.entries), 0)
            self.assertFalse((data_dir / "md_db" / "code" / "alpha").exists())
            self.assertFalse(clone_dir.exists())
            self.assertIn("code/alpha/a.md", result.deleted_paths)
        finally:
            shutil.rmtree(data_dir, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
