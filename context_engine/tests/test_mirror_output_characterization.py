from __future__ import annotations

import json
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from context_engine.infra.mirror.mirror_service import NodeMirrorService as MirrorServiceLegacy


class _FakeProc:
    def __init__(self, stdout: bytes, stderr: bytes = b"", returncode: int = 0) -> None:
        self._stdout = stdout
        self._stderr = stderr
        self.returncode = returncode

    async def communicate(self) -> tuple[bytes, bytes]:
        return self._stdout, self._stderr


class MirrorOutputCharacterizationTests(unittest.IsolatedAsyncioTestCase):
    async def test_mirror_json_output_is_returned_as_dict(self) -> None:
        service = MirrorServiceLegacy(
            data_dir=Path.cwd(),
            node_bin="node",
            mirror_cli="mirror-cli.js",
        )
        mirror_result = {
            "written": {"python": 3},
            "errors": {},
            "skipped": {"python": 1},
        }
        fake_proc = _FakeProc(stdout=json.dumps(mirror_result).encode("utf-8"))
        create_proc = AsyncMock(return_value=fake_proc)

        with patch("context_engine.infra.mirror.mirror_service.asyncio.create_subprocess_exec", new=create_proc):
            result = await service.run(
                workspace_entry={"name": "alpha", "languages": ["py"]},
                gitlab_config={
                    "cloneDir": str(Path.cwd() / "fake-clone"),
                    "projectUrl": "https://gitlab.example.com/org/repo",
                    "branch": "main",
                },
            )

        self.assertEqual(result, mirror_result)
        create_proc.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
