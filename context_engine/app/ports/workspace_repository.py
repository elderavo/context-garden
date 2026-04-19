from __future__ import annotations

from typing import Any, Protocol


class WorkspaceRepository(Protocol):
    """Read/write access to workspace registry state."""

    def list_all(self) -> list[dict[str, Any]]:
        ...

    def get_by_id(self, workspace_id: str) -> dict[str, Any] | None:
        ...

    def save_all(self, entries: list[dict[str, Any]]) -> None:
        ...

