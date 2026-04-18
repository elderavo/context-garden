from __future__ import annotations

from typing import Protocol


class WebhookInboxRepository(Protocol):
    """Idempotency store for webhook deliveries."""

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
        ...

    def release(self, *, idempotency_key: str) -> None:
        ...

