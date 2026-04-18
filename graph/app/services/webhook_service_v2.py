"""V2 webhook implementation (Stage 1 delegates to legacy)."""

from __future__ import annotations

import json
from typing import Any, Callable, Literal

from ..ports.event_bus import EventBus
from ..ports.inbox_repository import WebhookInboxRepository
from ...domain.webhook.normalize import normalize_gitlab_webhook
from ...domain.webhook.policies import build_idempotency_key
from ...domain.webhook.policies import expected_branch_ref
from ...domain.webhook.policies import should_ignore_ref
from .events import WorkspaceSyncRequested
from .webhook_service_legacy import ServiceResponse
from .webhook_service_legacy import handle_gitlab_webhook as handle_legacy_gitlab_webhook


async def handle_gitlab_webhook(
    *,
    token: str | None,
    payload: dict[str, Any],
    workspaces: list[dict[str, Any]],
    enqueue_sync: Callable[[str, str, Literal["webhook", "manual"]], Any],
    headers: dict[str, str] | None = None,
    event_bus: EventBus | None = None,
    inbox_repository: WebhookInboxRepository | None = None,
) -> ServiceResponse:
    if event_bus is None or inbox_repository is None:
        return await handle_legacy_gitlab_webhook(
            token=token,
            payload=payload,
            workspaces=workspaces,
            enqueue_sync=enqueue_sync,
            headers=headers,
            event_bus=event_bus,
            inbox_repository=inbox_repository,
        )

    if not token:
        return ServiceResponse(status=401, body={"error": "Missing X-Gitlab-Token header"})

    entry = next(
        (
            w
            for w in workspaces
            if w.get("sourceType") == "gitlab"
            and w.get("gitlabConfig", {}).get("webhookSecret") == token
        ),
        None,
    )
    if not entry:
        return ServiceResponse(status=401, body={"error": "Unknown webhook token"})

    normalized = normalize_gitlab_webhook(payload, headers or {})
    branch = entry["gitlabConfig"]["branch"]
    tracked_ref = expected_branch_ref(branch)
    if should_ignore_ref(payload_ref=normalized.ref, tracked_branch=branch):
        return ServiceResponse(
            status=200,
            body={
                "status": "ignored",
                "reason": f"push was to {normalized.ref}, tracking {tracked_ref}",
            },
        )

    idempotency_key = build_idempotency_key(
        workspace_id=entry["id"],
        delivery_id=normalized.delivery_id,
        payload_ref=normalized.ref,
        commit_sha=normalized.after,
    )
    claimed = inbox_repository.try_claim(
        idempotency_key=idempotency_key,
        workspace_id=entry["id"],
        delivery_id=normalized.delivery_id,
        ref=normalized.ref,
        commit_sha=normalized.after,
        payload_json=json.dumps(payload, sort_keys=True),
    )
    if not claimed:
        return ServiceResponse(
            status=200,
            body={
                "status": "ignored",
                "reason": "duplicate delivery",
                "idempotencyKey": idempotency_key,
            },
        )

    event = WorkspaceSyncRequested(
        workspace_id=entry["id"],
        workspace_name=entry["name"],
        idempotency_key=idempotency_key,
        delivery_id=normalized.delivery_id,
        ref=normalized.ref,
        commit_sha=normalized.after,
        correlation_id=idempotency_key,
    )
    try:
        await event_bus.publish(event)
    except Exception:
        inbox_repository.release(idempotency_key=idempotency_key)
        raise

    return ServiceResponse(
        status=202,
        body={
            "status": "accepted",
            "jobId": event.job_id,
            "workspaceName": entry["name"],
            "idempotencyKey": idempotency_key,
            "correlationId": event.correlation_id,
        },
    )


async def handle_gitlab_webhook_stage1_delegate(
    *,
    token: str | None,
    payload: dict[str, Any],
    workspaces: list[dict[str, Any]],
    enqueue_sync: Callable[[str, str, Literal["webhook", "manual"]], Any],
    headers: dict[str, str] | None = None,
    event_bus: EventBus | None = None,
    inbox_repository: WebhookInboxRepository | None = None,
) -> ServiceResponse:
    return await handle_legacy_gitlab_webhook(
        token=token,
        payload=payload,
        workspaces=workspaces,
        enqueue_sync=enqueue_sync,
        headers=headers,
        event_bus=event_bus,
        inbox_repository=inbox_repository,
    )
