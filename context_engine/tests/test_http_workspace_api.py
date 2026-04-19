from __future__ import annotations

import json
import shutil
import unittest
import uuid
from pathlib import Path
from typing import Any
from unittest.mock import patch

from context_engine import http_server


class _FakeRequest:
    def __init__(
        self,
        *,
        json_body: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        match_info: dict[str, str] | None = None,
    ) -> None:
        self._json_body = json_body if json_body is not None else {}
        self.headers = headers if headers is not None else {}
        self.path_params = match_info if match_info is not None else {}

    async def json(self) -> dict[str, Any]:
        return self._json_body


class _FakeDaemon:
    def __init__(self) -> None:
        self.sync_calls = 0
        self.reindex_calls: list[str] = []

    def handle_workspaces_sync(self, _params: dict[str, Any]) -> dict[str, Any]:
        self.sync_calls += 1
        return {"status": "ok", "syncCalls": self.sync_calls}

    def handle_index_rebuild(self, params: dict[str, Any]) -> dict[str, Any]:
        workspace_name = str(params.get("name", ""))
        self.reindex_calls.append(workspace_name)
        return {"queued": True, "name": workspace_name}

    def handle_daemon_health(self, _params: dict[str, Any]) -> dict[str, Any]:
        return {"status": "ok"}

    def handle_daemon_shutdown(self, _params: dict[str, Any]) -> dict[str, Any]:
        return {"ok": True}


class _FakeMirrorService:
    def __init__(self, data_dir: Path) -> None:
        self.data_dir = data_dir

    async def run(self, *, workspace_entry: dict[str, Any], gitlab_config: dict[str, Any]) -> dict[str, Any]:
        workspace_name = workspace_entry["name"]
        note_dir = self.data_dir / "md_db" / "code" / workspace_name
        note_dir.mkdir(parents=True, exist_ok=True)
        (note_dir / "from-http-api.md").write_text("# generated", "utf-8")
        return {"written": {"python": 1}, "skipped": {}, "cleaned": 0}


def _make_test_dir(prefix: str) -> Path:
    root = Path.cwd() / "ws-tests-http-api"
    root.mkdir(parents=True, exist_ok=True)
    target = root / f"{prefix}{uuid.uuid4().hex[:8]}"
    target.mkdir(parents=True, exist_ok=False)
    return target


def _json_response_body(resp: Any) -> dict[str, Any]:
    return json.loads(resp.body.decode("utf-8"))


async def _direct_to_thread(func, *args, **kwargs):  # type: ignore[no-untyped-def]
    return func(*args, **kwargs)


def _drop_task(coro):  # type: ignore[no-untyped-def]
    coro.close()
    class _DummyTask:
        pass
    return _DummyTask()


class HttpWorkspaceApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.data_dir = _make_test_dir("api-")
        self.source_dir = self.data_dir / "source"
        self.source_dir.mkdir(parents=True, exist_ok=True)

        self.daemon = _FakeDaemon()
        http_server.make_http_app(self.daemon, self.data_dir)
        http_server._mirror_service_ref = _FakeMirrorService(self.data_dir)  # type: ignore[assignment]

    async def asyncTearDown(self) -> None:
        shutil.rmtree(self.data_dir, ignore_errors=True)

    async def test_workspace_register_list_unregister_roundtrip(self) -> None:
        with patch("asyncio.create_task", new=_drop_task):
            register_resp = await http_server._handle_register_workspace(
                _FakeRequest(
                    json_body={
                        "name": "alpha-workspace",
                        "source_dir": str(self.source_dir),
                        "languages": ["py"],
                    }
                )
            )
        self.assertEqual(register_resp.status_code, 201)
        register_body = _json_response_body(register_resp)
        self.assertEqual(register_body["status"], "registered")
        self.assertEqual(register_body["entry"]["name"], "alpha-workspace")
        self.assertEqual(register_body["entry"]["sourceType"], "local")
        self.assertEqual(register_body["notesGenerated"], 1)
        self.assertIn("code/alpha-workspace/from-http-api.md", register_body["notePaths"])

        list_resp = await http_server._handle_list_workspaces(_FakeRequest())
        self.assertEqual(list_resp.status_code, 200)
        list_body = _json_response_body(list_resp)
        self.assertEqual(len(list_body), 1)
        self.assertEqual(list_body[0]["name"], "alpha-workspace")

        workspace_id = register_body["entry"]["id"]
        with patch("asyncio.to_thread", new=_direct_to_thread):
            unregister_resp = await http_server._handle_workspace_unregister(
                _FakeRequest(match_info={"id": workspace_id})
            )
        self.assertEqual(unregister_resp.status_code, 200)
        unregister_body = _json_response_body(unregister_resp)
        self.assertEqual(unregister_body["status"], "unregistered")
        self.assertGreaterEqual(len(unregister_body["deletedPaths"]), 1)

        post_list_resp = await http_server._handle_list_workspaces(_FakeRequest())
        self.assertEqual(post_list_resp.status_code, 200)
        post_list_body = _json_response_body(post_list_resp)
        self.assertEqual(post_list_body, [])

    async def test_register_requires_source_or_gitlab(self) -> None:
        response = await http_server._handle_register_workspace(
            _FakeRequest(json_body={"name": "missing-source", "languages": ["py"]})
        )
        self.assertEqual(response.status_code, 400)
        body = _json_response_body(response)
        self.assertIn("Provide either gitlab_url or source_dir", body["error"])

    async def test_register_rejects_duplicate_name(self) -> None:
        with patch("asyncio.create_task", new=_drop_task):
            first = await http_server._handle_register_workspace(
                _FakeRequest(
                    json_body={
                        "name": "dupe-workspace",
                        "source_dir": str(self.source_dir),
                        "languages": ["py"],
                    }
                )
            )
        self.assertEqual(first.status_code, 201)

        with patch("asyncio.create_task", new=_drop_task):
            second = await http_server._handle_register_workspace(
                _FakeRequest(
                    json_body={
                        "name": "dupe-workspace",
                        "source_dir": str(self.source_dir),
                        "languages": ["py"],
                    }
                )
            )
        self.assertEqual(second.status_code, 400)
        body = _json_response_body(second)
        self.assertIn("already exists", body["error"])


if __name__ == "__main__":
    unittest.main()
