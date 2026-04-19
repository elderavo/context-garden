from __future__ import annotations

from typing import Any

from .models import NormalizedGitlabWebhook


def normalize_gitlab_webhook(payload: dict[str, Any], headers: dict[str, str]) -> NormalizedGitlabWebhook:
    return NormalizedGitlabWebhook(
        ref=payload.get("ref"),
        after=payload.get("after"),
        before=payload.get("before"),
        delivery_id=headers.get("X-Gitlab-Event-UUID"),
        event_name=headers.get("X-Gitlab-Event"),
        payload=payload,
    )

