"""Webhook service — GitLab push webhook handling with idempotency."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import logging
from dataclasses import dataclass
from typing import Any, Callable, Literal

from ..ports.event_bus import EventBus
from ..ports.inbox_repository import WebhookInboxRepository
from ...domain.webhook.normalize import normalize_gitlab_webhook
from ...domain.webhook.policies import build_idempotency_key, expected_branch_ref, should_ignore_ref
from .events import WorkspaceSyncRequested
from . import workspace_service

log = logging.getLogger(__name__)


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


def _find_workspace_by_id(
    workspace_id: str,
    workspaces: list[dict[str, Any]],
) -> dict[str, Any] | None:
    return next((w for w in workspaces if w.get("id") == workspace_id), None)


def _workspace_log_context(entry: dict[str, Any] | None) -> str:
    if not entry:
        return "workspace=<unknown>"
    return f"workspace={entry.get('name')} id={entry.get('id')}"


def _record_webhook_status(
    *,
    workspace_repository: Any | None,
    workspace_id: str | None,
    status: str,
    reason: str = "",
    payload_ref: str | None = None,
    commit_sha: str | None = None,
    delivery_id: str | None = None,
) -> None:
    if workspace_repository is None or not workspace_id:
        return
    workspace_service.update_workspace_status(
        workspace_repository=workspace_repository,
        workspace_id=workspace_id,
        patch={
            "lastWebhookReceivedAt": datetime.now(timezone.utc).isoformat(),
            "lastWebhookStatus": status,
            "lastWebhookReason": reason,
            "lastWebhookRef": payload_ref or "",
            "lastWebhookCommit": commit_sha or "",
            "lastWebhookDeliveryId": delivery_id or "",
        },
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
    workspace_id: str | None = None,
    workspace_repository: Any | None = None,
) -> ServiceResponse:
    normalized = normalize_gitlab_webhook(payload, headers or {})

    if not token:
        _record_webhook_status(
            workspace_repository=workspace_repository,
            workspace_id=workspace_id,
            status="rejected_missing_token",
            reason="Missing X-Gitlab-Token header",
            payload_ref=normalized.ref,
            commit_sha=normalized.after,
            delivery_id=normalized.delivery_id,
        )
        log.warning(
            "GitLab webhook rejected: missing token route_workspace_id=%s ref=%s commit=%s delivery=%s",
            workspace_id,
            normalized.ref,
            normalized.after,
            normalized.delivery_id,
        )
        return ServiceResponse(status=401, body={"error": "Missing X-Gitlab-Token header"})

    entry = _find_workspace_by_id(workspace_id, workspaces) if workspace_id else None
    if workspace_id and entry is None:
        log.warning(
            "GitLab webhook rejected: unknown workspace id=%s ref=%s commit=%s delivery=%s",
            workspace_id,
            normalized.ref,
            normalized.after,
            normalized.delivery_id,
        )
        return ServiceResponse(status=404, body={"error": "Workspace not found"})
    if entry and entry.get("sourceType") != "gitlab":
        log.warning("GitLab webhook rejected: non-gitlab %s", _workspace_log_context(entry))
        return ServiceResponse(status=404, body={"error": "Workspace not found"})

    if entry and entry.get("gitlabConfig", {}).get("webhookSecret") != token:
        _record_webhook_status(
            workspace_repository=workspace_repository,
            workspace_id=entry.get("id"),
            status="rejected_invalid_token",
            reason="Unknown webhook token",
            payload_ref=normalized.ref,
            commit_sha=normalized.after,
            delivery_id=normalized.delivery_id,
        )
        log.warning(
            "GitLab webhook rejected: invalid token %s ref=%s commit=%s delivery=%s",
            _workspace_log_context(entry),
            normalized.ref,
            normalized.after,
            normalized.delivery_id,
        )
        return ServiceResponse(status=401, body={"error": "Unknown webhook token"})

    if entry is None:
        entry = _find_workspace_by_token(token, workspaces)
    if not entry:
        log.warning(
            "GitLab webhook rejected: unknown token route_workspace_id=%s ref=%s commit=%s delivery=%s",
            workspace_id,
            normalized.ref,
            normalized.after,
            normalized.delivery_id,
        )
        return ServiceResponse(status=401, body={"error": "Unknown webhook token"})

    branch = entry["gitlabConfig"]["branch"]
    expected_ref = f"refs/heads/{branch}"

    # Without idempotency infrastructure, use simple branch filtering
    if event_bus is None or inbox_repository is None:
        push_ref = payload.get("ref")
        if push_ref and push_ref != expected_ref:
            _record_webhook_status(
                workspace_repository=workspace_repository,
                workspace_id=entry["id"],
                status="ignored_wrong_ref",
                reason=f"push was to {push_ref}, tracking {expected_ref}",
                payload_ref=push_ref,
                commit_sha=payload.get("after"),
                delivery_id=normalized.delivery_id,
            )
            log.info(
                "GitLab webhook ignored: wrong ref %s ref=%s tracked=%s commit=%s",
                _workspace_log_context(entry),
                push_ref,
                expected_ref,
                payload.get("after"),
            )
            return ServiceResponse(
                status=200,
                body={
                    "status": "ignored",
                    "reason": f"push was to {push_ref}, tracking {expected_ref}",
                },
            )
        job = enqueue_sync(entry["id"], entry["name"], "webhook")
        _record_webhook_status(
            workspace_repository=workspace_repository,
            workspace_id=entry["id"],
            status="accepted",
            reason="sync enqueued",
            payload_ref=push_ref,
            commit_sha=payload.get("after"),
            delivery_id=normalized.delivery_id,
        )
        log.info(
            "GitLab webhook accepted: %s ref=%s commit=%s job_id=%s",
            _workspace_log_context(entry),
            push_ref,
            payload.get("after"),
            job.id,
        )
        return ServiceResponse(
            status=202,
            body={"status": "accepted", "jobId": job.id, "workspaceName": entry["name"]},
        )

    # Full path: idempotency + event bus
    tracked_ref = expected_branch_ref(branch)
    if should_ignore_ref(payload_ref=normalized.ref, tracked_branch=branch):
        _record_webhook_status(
            workspace_repository=workspace_repository,
            workspace_id=entry["id"],
            status="ignored_wrong_ref",
            reason=f"push was to {normalized.ref}, tracking {tracked_ref}",
            payload_ref=normalized.ref,
            commit_sha=normalized.after,
            delivery_id=normalized.delivery_id,
        )
        log.info(
            "GitLab webhook ignored: wrong ref %s ref=%s tracked=%s commit=%s delivery=%s",
            _workspace_log_context(entry),
            normalized.ref,
            tracked_ref,
            normalized.after,
            normalized.delivery_id,
        )
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
        _record_webhook_status(
            workspace_repository=workspace_repository,
            workspace_id=entry["id"],
            status="ignored_duplicate",
            reason="duplicate delivery",
            payload_ref=normalized.ref,
            commit_sha=normalized.after,
            delivery_id=normalized.delivery_id,
        )
        log.info(
            "GitLab webhook ignored: duplicate %s ref=%s commit=%s delivery=%s",
            _workspace_log_context(entry),
            normalized.ref,
            normalized.after,
            normalized.delivery_id,
        )
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
        _record_webhook_status(
            workspace_repository=workspace_repository,
            workspace_id=entry["id"],
            status="enqueue_failed",
            reason="event bus publish failed",
            payload_ref=normalized.ref,
            commit_sha=normalized.after,
            delivery_id=normalized.delivery_id,
        )
        raise

    _record_webhook_status(
        workspace_repository=workspace_repository,
        workspace_id=entry["id"],
        status="accepted",
        reason="sync enqueued",
        payload_ref=normalized.ref,
        commit_sha=normalized.after,
        delivery_id=normalized.delivery_id,
    )
    log.info(
        "GitLab webhook accepted: %s ref=%s commit=%s delivery=%s job_id=%s",
        _workspace_log_context(entry),
        normalized.ref,
        normalized.after,
        normalized.delivery_id,
        event.job_id,
    )
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
