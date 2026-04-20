from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class NormalizedGitlabWebhook:
    ref: str | None
    after: str | None
    before: str | None
    delivery_id: str | None
    event_name: str | None
    payload: dict[str, Any]

