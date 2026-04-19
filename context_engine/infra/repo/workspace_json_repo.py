from __future__ import annotations

import json
from pathlib import Path
from typing import Any


class WorkspaceJsonRepository:
    """Filesystem-backed workspace registry adapter."""

    def __init__(self, data_dir: Path) -> None:
        self._path = data_dir / ".context-garden" / "workspaces.json"

    def list_all(self) -> list[dict[str, Any]]:
        if not self._path.exists():
            return []
        try:
            return json.loads(self._path.read_text("utf-8"))
        except Exception:
            return []

    def get_by_id(self, workspace_id: str) -> dict[str, Any] | None:
        return next((entry for entry in self.list_all() if entry.get("id") == workspace_id), None)

    def save_all(self, entries: list[dict[str, Any]]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._path.write_text(json.dumps(entries, indent=2), "utf-8")

