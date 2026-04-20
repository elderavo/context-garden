"""Webhook service — GitLab push webhook handling with idempotency."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any, Callable, Literal

from ..ports.event_bus import EventBus
from ..ports.inbox_repository import WebhookInboxRepository
from ...domain.webhook.normalize import normalize_gitlab_webhook
from ...domain.webhook.policies import build_idempotency_key, expected_branch_ref, should_ignore_ref
from .events import WorkspaceSyncRequested


@dataclass(frozen=True)
class ServiceResponse:
    status: int
    body: dict[str, Any]


def _find_workspace_by_token(
    token: str,
    workspaces: list[dict[str, Any]],
) -> dict[str, Any] | None:
    return next(
        (
            w for w in workspaces
            if w.get("sourceType") == "gitlab"
            and w.get("gitlabConfig", {}).get("webhookSecret") == token
        ),
        None,
    )


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
    if not token:
        return ServiceResponse(status=401, body={"error": "Missing X-Gitlab-Token header"})

    entry = _find_workspace_by_token(token, workspaces)
    if not entry:
        return ServiceResponse(status=401, body={"error": "Unknown webhook token"})

    branch = entry["gitlabConfig"]["branch"]
    expected_ref = f"refs/heads/{branch}"

    # Without idempotency infrastructure, use simple branch filtering
    if event_bus is None or inbox_repository is None:
        push_ref = payload.get("ref")
        if push_ref and push_ref != expected_ref:
            return ServiceResponse(
                status=200,
                body={
                    "status": "ignored",
                    "reason": f"push was to {push_ref}, tracking {expected_ref}",
                },
            )
        job = enqueue_sync(entry["id"], entry["name"], "webhook")
        return ServiceResponse(
            status=202,
            body={"status": "accepted", "jobId": job.id, "workspaceName": entry["name"]},
        )

    # Full path: idempotency + event bus
    normalized = normalize_gitlab_webhook(payload, headers or {})
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
