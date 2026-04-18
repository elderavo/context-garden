from __future__ import annotations

from typing import Any, Protocol


class MirrorService(Protocol):
    """Code-to-markdown mirroring interface."""

    async def run(
        self,
        *,
        workspace_entry: dict[str, Any],
        gitlab_config: dict[str, Any],
    ) -> dict[str, Any]:
        ...

