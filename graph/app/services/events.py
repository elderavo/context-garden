from __future__ import annotations

from dataclasses import dataclass


@dataclass
class WorkspaceSyncRequested:
    workspace_id: str
    workspace_name: str
    idempotency_key: str
    delivery_id: str | None
    ref: str | None
    commit_sha: str | None
    correlation_id: str
    job_id: str | None = None

