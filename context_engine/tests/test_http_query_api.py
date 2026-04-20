from __future__ import annotations

import json
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

from context_engine import http_server


class _FakeRequest:
    def __init__(
        self,
        *,
        json_body: dict[str, Any] | None = None,
        query: dict[str, str] | None = None,
        app_state: Any = None,
    ) -> None:
        self._json_body = json_body if json_body is not None else {}
        self.query_params = query if query is not None else {}
        self.app = type("_App", (), {"state": app_state})()

    async def json(self) -> dict[str, Any]:
        return self._json_body


class _FakeDaemon:
    def handle_query_retrieve(self, params: dict[str, Any]) -> dict[str, Any]:
        return {
            "seed_notes": [{"noteId": "n1", "score": 0.9, "workspace": params.get("workspace", "")}],
            "expanded_notes": [],
        }

    def handle_query_find_path(self, params: dict[str, Any]) -> dict[str, Any]:
        return {
            "start_id": params["start"],
            "end_id": params["end"],
            "start_resolved_by": "path",
            "end_resolved_by": "path",
            "path_length": 1,
            "path_steps": [],
            "path_notes": [],
            "no_path": False,
            "duration_ms": 1,
        }

    def handle_query_get_note_content(self, params: dict[str, Any]) -> dict[str, Any]:
        return {"body": f"note:{params['relative_path']}"}

    def handle_query_stats(self, _params: dict[str, Any]) -> dict[str, Any]:
        return {"doc_count": 3, "node_count": 3, "edge_count": 2, "index_loaded": True}


async def _direct_to_thread(func, *args, **kwargs):  # type: ignore[no-untyped-def]
    return func(*args, **kwargs)


def _json(resp: Any) -> dict[str, Any]:
    return json.loads(resp.body.decode("utf-8"))


class HttpQueryApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.app = http_server.make_http_app(_FakeDaemon(), Path.cwd())

    def _req(self, **kwargs: Any) -> _FakeRequest:
        return _FakeRequest(app_state=self.app.state, **kwargs)

    async def test_retrieve_endpoint(self) -> None:
        with patch("asyncio.to_thread", new=_direct_to_thread):
            resp = await http_server._handle_api_retrieve(
                self._req(json_body={"query": "how auth works", "top_k": 5, "workspace": "alpha"})
            )
        self.assertEqual(resp.status_code, 200)
        body = _json(resp)
        self.assertIn("seed_notes", body)
        self.assertEqual(body["seed_notes"][0]["workspace"], "alpha")

    async def test_find_path_endpoint(self) -> None:
        with patch("asyncio.to_thread", new=_direct_to_thread):
            resp = await http_server._handle_api_find_path(
                self._req(json_body={"start": "a", "end": "b", "max_depth": 3})
            )
        self.assertEqual(resp.status_code, 200)
        body = _json(resp)
        self.assertEqual(body["start_id"], "a")
        self.assertEqual(body["end_id"], "b")

    async def test_rate_endpoint_validation(self) -> None:
        bad = await http_server._handle_api_rate(
            self._req(json_body={"retrieval_id": "r1", "query": "q", "score": 9})
        )
        self.assertEqual(bad.status_code, 400)

        ok = await http_server._handle_api_rate(
            self._req(
                json_body={
                    "retrieval_id": "r1",
                    "query": "q",
                    "score": 5,
                    "helpful": "note a",
                    "missing": "",
                }
            )
        )
        self.assertEqual(ok.status_code, 200)
        self.assertEqual(_json(ok)["status"], "recorded")

    async def test_note_endpoint(self) -> None:
        with patch("asyncio.to_thread", new=_direct_to_thread):
            resp = await http_server._handle_api_note(
                self._req(query={"relative_path": "code/alpha/a.md"})
            )
        self.assertEqual(resp.status_code, 200)
        body = _json(resp)
        self.assertEqual(body["body"], "note:code/alpha/a.md")

    async def test_stats_endpoint(self) -> None:
        with patch("asyncio.to_thread", new=_direct_to_thread):
            resp = await http_server._handle_api_stats(self._req(query={}))
        self.assertEqual(resp.status_code, 200)
        body = _json(resp)
        self.assertEqual(body["doc_count"], 3)
        self.assertTrue(body["index_loaded"])


if __name__ == "__main__":
    unittest.main()
