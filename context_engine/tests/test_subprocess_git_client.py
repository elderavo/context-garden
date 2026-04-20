from __future__ import annotations

import shutil
import unittest
import uuid
from pathlib import Path
from unittest.mock import AsyncMock, patch

from context_engine.infra.git.subprocess_git_client import SubprocessGitClient


class _ProcSuccess:
    returncode = 0

    async def communicate(self):
        return b"", b""


def _make_test_dir(prefix: str) -> Path:
    root = Path.cwd() / "ws-tests-http-api"
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"{prefix}{uuid.uuid4().hex[:8]}"
    path.mkdir(parents=True, exist_ok=False)
    return path


class SubprocessGitClientTests(unittest.IsolatedAsyncioTestCase):
    async def test_clone_repo_uses_workspace_known_hosts_for_ssh_urls(self) -> None:
        tmp = _make_test_dir("git-client-")
        clone_dir = tmp / "repo"
        expected_known_hosts = clone_dir.resolve().parent / "known_hosts"
        create_proc = AsyncMock(return_value=_ProcSuccess())

        try:
            with (
                patch(
                    "context_engine.infra.git.subprocess_git_client.asyncio.create_subprocess_exec",
                    new=create_proc,
                ),
                patch.dict("context_engine.infra.git.subprocess_git_client.os.environ", {}, clear=True),
            ):
                await SubprocessGitClient().clone_repo(
                    project_url="git@10.0.132.100:elderavo/context_garden",
                    clone_dir=str(clone_dir),
                    branch="master",
                    token=None,
                )

            args = create_proc.await_args.args
            env = create_proc.await_args.kwargs["env"]

            self.assertIn("git@10.0.132.100:elderavo/context_garden.git", args)
            self.assertEqual(args[1], "clone")
            self.assertEqual(args[3], "--branch=master")
            self.assertIn("GIT_SSH_COMMAND", env)
            self.assertIn("StrictHostKeyChecking=accept-new", env["GIT_SSH_COMMAND"])
            self.assertIn(f"UserKnownHostsFile={expected_known_hosts}", env["GIT_SSH_COMMAND"])
            self.assertTrue(expected_known_hosts.parent.exists())
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    async def test_fetch_and_reset_does_not_force_git_ssh_command_for_https_urls(self) -> None:
        tmp = _make_test_dir("git-client-")
        create_proc = AsyncMock(return_value=_ProcSuccess())

        try:
            with (
                patch(
                    "context_engine.infra.git.subprocess_git_client.asyncio.create_subprocess_exec",
                    new=create_proc,
                ),
                patch.dict("context_engine.infra.git.subprocess_git_client.os.environ", {}, clear=True),
            ):
                await SubprocessGitClient().fetch_and_reset(
                    project_url="https://gitlab.example.com/org/repo",
                    clone_dir=str(tmp),
                    branch="main",
                    token="abc123",
                )

            self.assertEqual(create_proc.await_count, 3)
            first_args = create_proc.await_args_list[0].args
            self.assertIn("https://oauth2:abc123@gitlab.example.com/org/repo.git", first_args)
            for call in create_proc.await_args_list:
                self.assertNotIn("GIT_SSH_COMMAND", call.kwargs["env"])
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    async def test_custom_ssh_command_override_is_used(self) -> None:
        tmp = _make_test_dir("git-client-")
        create_proc = AsyncMock(return_value=_ProcSuccess())
        custom_command = "ssh -F /tmp/custom_ssh_config"

        try:
            with (
                patch(
                    "context_engine.infra.git.subprocess_git_client.asyncio.create_subprocess_exec",
                    new=create_proc,
                ),
                patch.dict(
                    "context_engine.infra.git.subprocess_git_client.os.environ",
                    {"CG_GIT_SSH_COMMAND": custom_command},
                    clear=True,
                ),
            ):
                await SubprocessGitClient().clone_repo(
                    project_url="ssh://git@10.0.132.100:2222/elderavo/context_garden",
                    clone_dir=str(tmp / "repo"),
                    branch="main",
                    token=None,
                )

            env = create_proc.await_args.kwargs["env"]
            self.assertEqual(env["GIT_SSH_COMMAND"], custom_command)
        finally:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()
