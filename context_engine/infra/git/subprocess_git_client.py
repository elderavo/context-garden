from __future__ import annotations

import asyncio
import os
import shlex
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse, urlunparse


class SubprocessGitClient:
    """Git subprocess adapter used by sync orchestration."""

    async def clone_repo(
        self,
        *,
        project_url: str,
        clone_dir: str,
        branch: str,
        token: Optional[str],
    ) -> None:
        auth_url = self._build_auth_url(project_url, token)
        env = self._build_git_env(project_url=project_url, clone_dir=clone_dir)
        await self._run_git(
            ["clone", "--depth=1", f"--branch={branch}", "--no-single-branch", auth_url, clone_dir],
            cwd=None,
            env=env,
        )

    async def fetch_and_reset(
        self,
        *,
        project_url: str,
        clone_dir: str,
        branch: str,
        token: Optional[str],
    ) -> None:
        auth_url = self._build_auth_url(project_url, token)
        env = self._build_git_env(project_url=project_url, clone_dir=clone_dir)
        await self._run_git(["remote", "set-url", "origin", auth_url], cwd=clone_dir, env=env)
        await self._run_git(["fetch", "origin", branch], cwd=clone_dir, env=env)
        await self._run_git(["reset", "--hard", f"origin/{branch}"], cwd=clone_dir, env=env)

    @staticmethod
    def _build_auth_url(project_url: str, token: Optional[str]) -> str:
        if project_url.startswith("git@") or project_url.startswith("ssh://"):
            return project_url if project_url.endswith(".git") else f"{project_url}.git"

        normalized = project_url if project_url.endswith(".git") else f"{project_url}.git"
        if not token:
            return normalized

        parsed = urlparse(normalized)
        netloc = (
            f"oauth2:{token}@{parsed.hostname}:{parsed.port}"
            if parsed.port
            else f"oauth2:{token}@{parsed.hostname}"
        )
        return urlunparse(parsed._replace(netloc=netloc))

    @staticmethod
    def _build_git_env(*, project_url: str, clone_dir: str) -> dict[str, str]:
        env = os.environ.copy()
        if not (project_url.startswith("git@") or project_url.startswith("ssh://")):
            return env

        custom_ssh_command = env.get("CG_GIT_SSH_COMMAND")
        if custom_ssh_command:
            env["GIT_SSH_COMMAND"] = custom_ssh_command
            return env

        known_hosts_file = env.get("CG_GIT_SSH_KNOWN_HOSTS_FILE")
        if known_hosts_file:
            known_hosts_path = Path(known_hosts_file).expanduser()
        else:
            # Keep known hosts in workspace-local state so SSH can run when ~/.ssh is read-only.
            known_hosts_path = Path(clone_dir).resolve().parent / "known_hosts"

        known_hosts_path.parent.mkdir(parents=True, exist_ok=True)

        ssh_args = [
            "ssh",
            "-o",
            "BatchMode=yes",
            "-o",
            "StrictHostKeyChecking=accept-new",
            "-o",
            f"UserKnownHostsFile={known_hosts_path}",
        ]

        key_file = env.get("CG_GIT_SSH_KEY_FILE")
        if key_file:
            ssh_args.extend(["-i", str(Path(key_file).expanduser())])

        env["GIT_SSH_COMMAND"] = shlex.join(ssh_args)
        return env

    @staticmethod
    async def _run_git(args: list[str], *, cwd: str | None, env: dict[str, str]) -> None:
        proc = await asyncio.create_subprocess_exec(
            "git",
            *args,
            cwd=cwd,
            env=env,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr_bytes = await proc.communicate()
        if proc.returncode != 0:
            stderr = stderr_bytes.decode("utf-8", errors="replace").strip() if stderr_bytes else ""
            raise RuntimeError(f"git {args[0]} failed (exit {proc.returncode}): {stderr}")
