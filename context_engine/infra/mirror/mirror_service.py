from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any


class NodeMirrorService:
    """Mirror adapter that shells out to the TypeScript mirror CLI."""

    def __init__(self, *, data_dir: Path, node_bin: str, mirror_cli: str) -> None:
        self._data_dir = data_dir
        self._node_bin = node_bin
        self._mirror_cli = mirror_cli

    async def run(
        self,
        *,
        workspace_entry: dict[str, Any],
        gitlab_config: dict[str, Any],
        force: bool = False,
    ) -> dict[str, Any]:
        mirror_dir_path = self._data_dir / "md_db" / "code" / workspace_entry["name"]
        mirror_dir_path.mkdir(parents=True, exist_ok=True)
        mirror_dir = str(mirror_dir_path)
        opts = {
            "scanDir": gitlab_config["cloneDir"],
            "mirrorDir": mirror_dir,
            "languages": workspace_entry.get("languages", []),
            "force": force,
            "workspace": workspace_entry["name"],
            "wikilinkPrefix": f"code/{workspace_entry['name']}",
            "omitPatterns": workspace_entry.get("omitPatterns"),
        }

        proc = await asyncio.create_subprocess_exec(
            self._node_bin,
            self._mirror_cli,
            json.dumps(opts),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout_bytes, stderr_bytes = await proc.communicate()
        stdout = stdout_bytes.decode("utf-8", errors="replace").strip() if stdout_bytes else ""

        try:
            result = json.loads(stdout)
        except json.JSONDecodeError:
            stderr = stderr_bytes.decode("utf-8", errors="replace").strip() if stderr_bytes else ""
            raise RuntimeError(f"Mirror CLI produced non-JSON output: {stdout!r}\nstderr: {stderr}")

        if "error" in result:
            raise RuntimeError(f"Mirror CLI error: {result['error']}")

        return result
