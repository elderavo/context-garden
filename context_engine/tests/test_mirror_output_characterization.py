from __future__ import annotations

import asyncio
import json
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from context_engine.infra.mirror.mirror_service import NodeMirrorService as MirrorServiceLegacy


class _FakeProc:
    def __init__(self, stdout: bytes, stderr: bytes = b"", returncode: int = 0) -> None:
        self._stdout = stdout
        self._stderr = stderr
        self.returncode = returncode

    async def communicate(self) -> tuple[bytes, bytes]:
        return self._stdout, self._stderr

    async def wait(self) -> int:
        return self.returncode


class _CancelledProc:
    def __init__(self) -> None:
        self.returncode = None
        self.terminate = Mock(side_effect=self._mark_exited)
        self.kill = Mock(side_effect=self._mark_exited)
        self.wait = AsyncMock(return_value=0)

    async def communicate(self) -> tuple[bytes, bytes]:
        raise asyncio.CancelledError

    def _mark_exited(self) -> None:
        self.returncode = -15


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

    async def test_mirror_process_is_reaped_when_cancelled(self) -> None:
        service = MirrorServiceLegacy(
            data_dir=Path.cwd(),
            node_bin="node",
            mirror_cli="mirror-cli.js",
        )
        fake_proc = _CancelledProc()
        create_proc = AsyncMock(return_value=fake_proc)

        with patch("context_engine.infra.mirror.mirror_service.asyncio.create_subprocess_exec", new=create_proc):
            with self.assertRaises(asyncio.CancelledError):
                await service.run(
                    workspace_entry={"name": "alpha", "languages": ["py"]},
                    gitlab_config={
                        "cloneDir": str(Path.cwd() / "fake-clone"),
                        "projectUrl": "https://gitlab.example.com/org/repo",
                        "branch": "main",
                    },
                )

        fake_proc.terminate.assert_called_once()
        fake_proc.wait.assert_awaited_once()
        fake_proc.kill.assert_not_called()


if __name__ == "__main__":
    unittest.main()
