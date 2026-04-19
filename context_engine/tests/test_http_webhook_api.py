from __future__ import annotations

import asyncio
import json
import shutil
import unittest
import uuid
from pathlib import Path
from typing import Any

from context_engine import http_server
from context_engine.core import jobs


class _FakeRequest:
    def __init__(self, *, json_body: dict[str, Any] | None = None, headers: dict[str, str] | None = None) -> None:
        self._json_body = json_body if json_body is not None else {}
        self.headers = headers if headers is not None else {}

    async def json(self) -> dict[str, Any]:
        return self._json_body


class _FakeDaemon:
    def handle_daemon_health(self, _params: dict[str, Any]) -> dict[str, Any]:
        return {"status": "ok"}

    def handle_daemon_shutdown(self, _params: dict[str, Any]) -> dict[str, Any]:
        return {"ok": True}


class _NoopOrchestrator:
    async def execute_sync(self, *, job: jobs.Job, log) -> None:
        log(f"sync {job.workspace_id}")

    async def execute_index(self, *, job: jobs.Job, log) -> None:
        log(f"index {job.workspace_id}")


def _make_test_dir(prefix: str) -> Path:
    root = Path.cwd() / "ws-tests-http-webhook"
    root.mkdir(parents=True, exist_ok=True)
    target = root / f"{prefix}{uuid.uuid4().hex[:8]}"
    target.mkdir(parents=True, exist_ok=False)
    return target


def _json_response_body(resp: Any) -> dict[str, Any]:
    return json.loads(resp.body.decode("utf-8"))


class HttpWebhookApiTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.data_dir = _make_test_dir("api-")

        jobs._jobs.clear()
        jobs._worker_task = None
        jobs._workspace_queues.clear()
        for task in jobs._workspace_worker_tasks.values():
            task.cancel()
        jobs._workspace_worker_tasks.clear()
        jobs._sync_orchestrator = _NoopOrchestrator()

        http_server.make_http_app(_FakeDaemon(), self.data_dir)
        assert http_server._workspace_repo_ref is not None
        http_server._workspace_repo_ref.save_all(
            [
                {
                    "id": "ws-1",
                    "name": "alpha-workspace",
                    "sourceType": "gitlab",
                    "active": True,
                    "registeredAt": "2026-04-19T00:00:00Z",
                    "sourceDir": str(self.data_dir / "repo"),
                    "languages": ["py"],
                    "gitlabConfig": {
                        "projectUrl": "ssh://git@gitlab.home.lab:2222/elderavo/context_garden.git",
                        "branch": "main",
                        "webhookSecret": "secret-token",
                        "cloneDir": str(self.data_dir / "repo"),
                    },
                }
            ]
        )

    async def asyncTearDown(self) -> None:
        if jobs._workspace_worker_tasks:
            await asyncio.gather(*list(jobs._workspace_worker_tasks.values()))
        shutil.rmtree(self.data_dir, ignore_errors=True)

    async def test_webhook_event_enqueues_job_and_returns_job_id(self) -> None:
        response = await http_server._handle_gitlab_webhook(
            _FakeRequest(
                headers={
                    "X-Gitlab-Token": "secret-token",
                    "X-Gitlab-Event-UUID": "delivery-1",
                    "X-Gitlab-Event": "Push Hook",
                },
                json_body={"ref": "refs/heads/main", "after": "abc123"},
            )
        )

        self.assertEqual(response.status_code, 202)
        body = _json_response_body(response)
        self.assertEqual(body["status"], "accepted")
        self.assertTrue(body["jobId"])
        self.assertIn("idempotencyKey", body)
        job = jobs.get_job(body["jobId"])
        self.assertIsNotNone(job)
        assert job is not None
        self.assertEqual(job.workspace_id, "ws-1")
        self.assertEqual(job.type, "sync_workspace")

    async def test_duplicate_delivery_is_ignored_without_second_job(self) -> None:
        first = await http_server._handle_gitlab_webhook(
            _FakeRequest(
                headers={
                    "X-Gitlab-Token": "secret-token",
                    "X-Gitlab-Event-UUID": "delivery-2",
                    "X-Gitlab-Event": "Push Hook",
                },
                json_body={"ref": "refs/heads/main", "after": "def456"},
            )
        )
        second = await http_server._handle_gitlab_webhook(
            _FakeRequest(
                headers={
                    "X-Gitlab-Token": "secret-token",
                    "X-Gitlab-Event-UUID": "delivery-2",
                    "X-Gitlab-Event": "Push Hook",
                },
                json_body={"ref": "refs/heads/main", "after": "def456"},
            )
        )

        self.assertEqual(first.status_code, 202)
        self.assertEqual(second.status_code, 200)
        second_body = _json_response_body(second)
        self.assertEqual(second_body["status"], "ignored")
        self.assertEqual(second_body["reason"], "duplicate delivery")

        workspace_jobs = [
            j for j in jobs.list_jobs() if j.workspace_id == "ws-1" and j.type == "sync_workspace"
        ]
        self.assertEqual(len(workspace_jobs), 1)


if __name__ == "__main__":
    unittest.main()
