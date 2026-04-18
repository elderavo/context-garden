from __future__ import annotations

from typing import Any

from .mirror_service_legacy import MirrorServiceLegacy


class MirrorServiceV2:
    """Stage-1 v2 mirror adapter delegating to legacy implementation."""

    def __init__(self, legacy: MirrorServiceLegacy) -> None:
        self._legacy = legacy

    async def run(
        self,
        *,
        workspace_entry: dict[str, Any],
        gitlab_config: dict[str, Any],
    ) -> dict[str, Any]:
        return await self._legacy.run(workspace_entry=workspace_entry, gitlab_config=gitlab_config)

