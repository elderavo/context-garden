from __future__ import annotations

import unittest
from unittest.mock import Mock

from context_engine.app.services import webhook_service as webhook_service_v2


class _Inbox:
    def __init__(self) -> None:
        self._claimed: set[str] = set()
        self.release_calls: list[str] = []
        self.try_claim_calls: list[str] = []

    def try_claim(
        self,
        *,
        idempotency_key: str,
        workspace_id: str,
        delivery_id: str | None,
        ref: str | None,
        commit_sha: str | None,
        payload_json: str,
    ) -> bool:
        self.try_claim_calls.append(idempotency_key)
        if idempotency_key in self._claimed:
            return False
        self._claimed.add(idempotency_key)
        return True

    def release(self, *, idempotency_key: str) -> None:
        self.release_calls.append(idempotency_key)
        self._claimed.discard(idempotency_key)


class _Bus:
    def __init__(self) -> None:
        self.published = []

    async def publish(self, event) -> None:
        event.job_id = "job-123"
        self.published.append(event)


class _FailingBus:
    async def publish(self, _event) -> None:
        raise RuntimeError("publish failed")


class WebhookServiceV2Tests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.workspaces = [
            {
                "id": "ws-1",
                "name": "alpha",
                "sourceType": "gitlab",
                "gitlabConfig": {
                    "webhookSecret": "secret-token",
                    "branch": "main",
                },
            }
        ]

    async def test_duplicate_delivery_is_ignored(self) -> None:
        inbox = _Inbox()
        bus = _Bus()
        enqueue_sync = Mock()

        first = await webhook_service_v2.handle_gitlab_webhook(
            token="secret-token",
            payload={"ref": "refs/heads/main", "after": "abc123"},
            headers={"X-Gitlab-Event-UUID": "delivery-1"},
            workspaces=self.workspaces,
            enqueue_sync=enqueue_sync,
            event_bus=bus,
            inbox_repository=inbox,
        )
        second = await webhook_service_v2.handle_gitlab_webhook(
            token="secret-token",
            payload={"ref": "refs/heads/main", "after": "abc123"},
            headers={"X-Gitlab-Event-UUID": "delivery-1"},
            workspaces=self.workspaces,
            enqueue_sync=enqueue_sync,
            event_bus=bus,
            inbox_repository=inbox,
        )

        self.assertEqual(first.status, 202)
        self.assertEqual(first.body["status"], "accepted")
        self.assertEqual(first.body["jobId"], "job-123")
        self.assertEqual(second.status, 200)
        self.assertEqual(second.body["status"], "ignored")
        self.assertEqual(second.body["reason"], "duplicate delivery")
        self.assertEqual(len(bus.published), 1)
        enqueue_sync.assert_not_called()

    async def test_untracked_branch_is_ignored_before_claim(self) -> None:
        inbox = _Inbox()
        bus = _Bus()
        enqueue_sync = Mock()

        result = await webhook_service_v2.handle_gitlab_webhook(
            token="secret-token",
            payload={"ref": "refs/heads/feature/refactor"},
            headers={"X-Gitlab-Event-UUID": "delivery-2"},
            workspaces=self.workspaces,
            enqueue_sync=enqueue_sync,
            event_bus=bus,
            inbox_repository=inbox,
        )

        self.assertEqual(result.status, 200)
        self.assertEqual(result.body["status"], "ignored")
        self.assertEqual(len(inbox.try_claim_calls), 0)
        self.assertEqual(len(bus.published), 0)
        enqueue_sync.assert_not_called()

    async def test_publish_failure_releases_claim(self) -> None:
        inbox = _Inbox()
        enqueue_sync = Mock()

        with self.assertRaises(RuntimeError):
            await webhook_service_v2.handle_gitlab_webhook(
                token="secret-token",
                payload={"ref": "refs/heads/main", "after": "abc123"},
                headers={"X-Gitlab-Event-UUID": "delivery-3"},
                workspaces=self.workspaces,
                enqueue_sync=enqueue_sync,
                event_bus=_FailingBus(),
                inbox_repository=inbox,
            )

        self.assertEqual(len(inbox.release_calls), 1)
        enqueue_sync.assert_not_called()


if __name__ == "__main__":
    unittest.main()

