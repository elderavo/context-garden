from __future__ import annotations

import unittest
from unittest.mock import Mock

from graph.app.services import webhook_service_legacy


class _Job:
    def __init__(self, job_id: str) -> None:
        self.id = job_id


class WebhookServiceCharacterizationTests(unittest.IsolatedAsyncioTestCase):
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

    async def test_missing_token_is_rejected(self) -> None:
        enqueue_sync = Mock(return_value=_Job("job-1"))
        result = await webhook_service_legacy.handle_gitlab_webhook(
            token=None,
            payload={},
            workspaces=self.workspaces,
            enqueue_sync=enqueue_sync,
        )
        self.assertEqual(result.status, 401)
        self.assertEqual(result.body, {"error": "Missing X-Gitlab-Token header"})
        enqueue_sync.assert_not_called()

    async def test_push_to_untracked_branch_is_ignored(self) -> None:
        enqueue_sync = Mock(return_value=_Job("job-1"))
        result = await webhook_service_legacy.handle_gitlab_webhook(
            token="secret-token",
            payload={"ref": "refs/heads/feature/refactor"},
            workspaces=self.workspaces,
            enqueue_sync=enqueue_sync,
        )
        self.assertEqual(result.status, 200)
        self.assertEqual(result.body["status"], "ignored")
        self.assertEqual(
            result.body["reason"],
            "push was to refs/heads/feature/refactor, tracking refs/heads/main",
        )
        enqueue_sync.assert_not_called()

    async def test_tracked_branch_push_is_accepted(self) -> None:
        enqueue_sync = Mock(return_value=_Job("job-42"))
        result = await webhook_service_legacy.handle_gitlab_webhook(
            token="secret-token",
            payload={"ref": "refs/heads/main"},
            workspaces=self.workspaces,
            enqueue_sync=enqueue_sync,
        )
        self.assertEqual(result.status, 202)
        self.assertEqual(
            result.body,
            {"status": "accepted", "jobId": "job-42", "workspaceName": "alpha"},
        )
        enqueue_sync.assert_called_once_with("ws-1", "alpha", "webhook")


if __name__ == "__main__":
    unittest.main()

