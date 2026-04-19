from __future__ import annotations

import asyncio
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
        await self._run_git(
            ["clone", "--depth=1", f"--branch={branch}", "--no-single-branch", auth_url, clone_dir],
            cwd=None,
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
        await self._run_git(["remote", "set-url", "origin", auth_url], cwd=clone_dir)
        await self._run_git(["fetch", "origin", branch], cwd=clone_dir)
        await self._run_git(["reset", "--hard", f"origin/{branch}"], cwd=clone_dir)

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
    async def _run_git(args: list[str], *, cwd: str | None) -> None:
        proc = await asyncio.create_subprocess_exec(
            "git",
            *args,
            cwd=cwd,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr_bytes = await proc.communicate()
        if proc.returncode != 0:
            stderr = stderr_bytes.decode("utf-8", errors="replace").strip() if stderr_bytes else ""
            raise RuntimeError(f"git {args[0]} failed (exit {proc.returncode}): {stderr}")
