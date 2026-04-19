from __future__ import annotations

from typing import Optional, Protocol


class GitClient(Protocol):
    """Git operations required by workspace sync."""

    async def clone_repo(
        self,
        *,
        project_url: str,
        clone_dir: str,
        branch: str,
        token: Optional[str],
    ) -> None:
        ...

    async def fetch_and_reset(
        self,
        *,
        project_url: str,
        clone_dir: str,
        branch: str,
        token: Optional[str],
    ) -> None:
        ...
