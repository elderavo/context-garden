from __future__ import annotations

from typing import Any, Literal, Protocol


class JobQueue(Protocol):
    """Queue operations used by HTTP/adapter layers."""

    def enqueue_sync(
        self,
        workspace_id: str,
        workspace_name: str,
        triggered_by: Literal["webhook", "manual"],
    ) -> Any:
        ...

    def enqueue_index(
        self,
        workspace_id: str,
        workspace_name: str,
        triggered_by: Literal["webhook", "manual"],
    ) -> Any:
        ...

    def enqueue_rebuild(
        self,
        workspace_id: str,
        workspace_name: str,
        triggered_by: Literal["webhook", "manual"],
    ) -> Any:
        ...

    def list_jobs(self) -> list[Any]:
        ...

    def get_job(self, job_id: str) -> Any | None:
        ...

