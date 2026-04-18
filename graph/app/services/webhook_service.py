"""Active webhook adapter selecting legacy/v2 implementation."""

from __future__ import annotations

import os
from typing import Any, Callable, Literal

from ..ports.event_bus import EventBus
from ..ports.inbox_repository import WebhookInboxRepository
from .webhook_service_legacy import ServiceResponse
from . import webhook_service_legacy
from . import webhook_service_v2

_IMPL_ENV = "CG_WEBHOOK_SERVICE_IMPL"


def _use_v2() -> bool:
    return os.environ.get(_IMPL_ENV, "v2").strip().lower() == "v2"


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
    impl = webhook_service_v2 if _use_v2() else webhook_service_legacy
    return await impl.handle_gitlab_webhook(
        token=token,
        payload=payload,
        workspaces=workspaces,
        enqueue_sync=enqueue_sync,
        headers=headers,
        event_bus=event_bus,
        inbox_repository=inbox_repository,
    )
