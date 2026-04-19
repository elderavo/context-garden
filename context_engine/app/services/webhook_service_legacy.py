"""Legacy webhook behavior extracted behind a service seam."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Literal


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
    event_bus: Any | None = None,
    inbox_repository: Any | None = None,
) -> ServiceResponse:
    """Mirror current webhook semantics with no behavior changes."""
    if not token:
        return ServiceResponse(status=401, body={"error": "Missing X-Gitlab-Token header"})

    entry = _find_workspace_by_token(token, workspaces)
    if not entry:
        return ServiceResponse(status=401, body={"error": "Unknown webhook token"})

    expected_ref = f"refs/heads/{entry['gitlabConfig']['branch']}"
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
        body={
            "status": "accepted",
            "jobId": job.id,
            "workspaceName": entry["name"],
        },
    )
