from __future__ import annotations

from typing import Literal

from ... import jobs


class InProcJobQueue:
    """Adapter over the module-level in-process job queue."""

    def enqueue_sync(
        self,
        workspace_id: str,
        workspace_name: str,
        triggered_by: Literal["webhook", "manual"] = "manual",
    ):
        return jobs.enqueue_sync(workspace_id, workspace_name, triggered_by)

    def enqueue_index(
        self,
        workspace_id: str,
        workspace_name: str,
        triggered_by: Literal["webhook", "manual"] = "manual",
    ):
        return jobs.enqueue_index(workspace_id, workspace_name, triggered_by)

    def list_jobs(self):
        return jobs.list_jobs()

    def get_job(self, job_id: str):
        return jobs.get_job(job_id)

