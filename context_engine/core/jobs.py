"""Job queue for the ContextGarden control plane.

Asyncio-native queue with per-workspace workers:
- Jobs for the same workspace are processed in order.
- Different workspaces can process concurrently.
- All state is in-memory and non-persistent.

Job types:
  sync_workspace  - git fetch+reset, mirror, reindex
  index_workspace - reindex only
"""

from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Literal, Optional

from ..app.services import sync_orchestrator as sync_orchestrator_adapter

log = logging.getLogger(__name__)

JobType = Literal["sync_workspace", "index_workspace", "rebuild_workspace"]
JobStatus = Literal["pending", "running", "done", "failed"]

MAX_HISTORY = 200


@dataclass
class Job:
    id: str
    type: JobType
    workspace_id: str
    workspace_name: str
    triggered_by: Literal["webhook", "manual"]
    status: JobStatus
    created_at: str
    started_at: Optional[str] = None
    completed_at: Optional[str] = None
    log: list[str] = field(default_factory=list)
    error: Optional[str] = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "type": self.type,
            "workspaceId": self.workspace_id,
            "workspaceName": self.workspace_name,
            "triggeredBy": self.triggered_by,
            "status": self.status,
            "error": self.error,
            "createdAt": self.created_at,
            "startedAt": self.started_at,
            "completedAt": self.completed_at,
            "log": self.log,
        }


_jobs: list[Job] = []
# Backward-compat alias used by older tests; points to the most recently created worker.
_worker_task: Optional[asyncio.Task[None]] = None
_workspace_queues: dict[str, asyncio.Queue[Job]] = {}
_workspace_worker_tasks: dict[str, asyncio.Task[None]] = {}
_data_dir: Path = Path.cwd()
_node_bin: str = "node"
_mirror_cli: Optional[str] = None
_get_daemon: Optional[Callable[[], Any]] = None
_sync_orchestrator: Optional[sync_orchestrator_adapter.SyncOrchestrator] = None


def init(data_dir: str | Path, node_bin: str, mirror_cli: str, get_daemon: Callable[[], Any]) -> None:
    """Call once at daemon startup."""
    global _data_dir, _node_bin, _mirror_cli, _get_daemon, _sync_orchestrator
    _data_dir = Path(data_dir)
    _node_bin = node_bin
    _mirror_cli = mirror_cli
    _get_daemon = get_daemon
    _sync_orchestrator = sync_orchestrator_adapter.build_default(
        data_dir=_data_dir,
        node_bin=_node_bin,
        mirror_cli=_mirror_cli,
        get_daemon=_get_daemon,
    )


def enqueue_sync(
    workspace_id: str,
    workspace_name: str,
    triggered_by: Literal["webhook", "manual"] = "manual",
) -> Job:
    existing = _find_active(workspace_id, "sync_workspace")
    if existing:
        return existing
    job = _make_job("sync_workspace", workspace_id, workspace_name, triggered_by)
    _push_and_schedule(job)
    return job


def enqueue_index(
    workspace_id: str,
    workspace_name: str,
    triggered_by: Literal["webhook", "manual"] = "manual",
) -> Job:
    existing = _find_active(workspace_id, "index_workspace")
    if existing:
        return existing
    job = _make_job("index_workspace", workspace_id, workspace_name, triggered_by)
    _push_and_schedule(job)
    return job


def enqueue_rebuild(
    workspace_id: str,
    workspace_name: str,
    triggered_by: Literal["webhook", "manual"] = "manual",
) -> Job:
    existing = _find_active(workspace_id, "rebuild_workspace")
    if existing:
        return existing
    job = _make_job("rebuild_workspace", workspace_id, workspace_name, triggered_by)
    _push_and_schedule(job)
    return job


def list_jobs() -> list[Job]:
    return list(reversed(_jobs))


def get_job(job_id: str) -> Optional[Job]:
    return next((j for j in _jobs if j.id == job_id), None)


def _find_active(workspace_id: str, job_type: JobType) -> Optional[Job]:
    return next(
        (
            j
            for j in _jobs
            if j.workspace_id == workspace_id and j.type == job_type and j.status in ("pending", "running")
        ),
        None,
    )


def _make_job(
    job_type: JobType,
    workspace_id: str,
    workspace_name: str,
    triggered_by: Literal["webhook", "manual"],
) -> Job:
    from datetime import datetime, timezone

    return Job(
        id=str(uuid.uuid4()),
        type=job_type,
        workspace_id=workspace_id,
        workspace_name=workspace_name,
        triggered_by=triggered_by,
        status="pending",
        created_at=datetime.now(timezone.utc).isoformat(),
    )


def _push_and_schedule(job: Job) -> None:
    _jobs.append(job)
    if len(_jobs) > MAX_HISTORY:
        del _jobs[: len(_jobs) - MAX_HISTORY]

    queue = _workspace_queues.get(job.workspace_id)
    if queue is None:
        queue = asyncio.Queue()
        _workspace_queues[job.workspace_id] = queue
    queue.put_nowait(job)
    _schedule_workspace_worker(job.workspace_id)


def _log(job: Job, line: str) -> None:
    from datetime import datetime, timezone

    job.log.append(f"[{datetime.now(timezone.utc).isoformat()}] {line}")


def _schedule_workspace_worker(workspace_id: str) -> None:
    global _worker_task

    existing = _workspace_worker_tasks.get(workspace_id)
    if existing is not None and not existing.done():
        return

    task = asyncio.create_task(_run_workspace_worker(workspace_id))
    _workspace_worker_tasks[workspace_id] = task
    _worker_task = task

    def _cleanup(done_task: asyncio.Task[None]) -> None:
        current = _workspace_worker_tasks.get(workspace_id)
        if current is done_task:
            _workspace_worker_tasks.pop(workspace_id, None)
        queue = _workspace_queues.get(workspace_id)
        if queue is not None and queue.empty():
            _workspace_queues.pop(workspace_id, None)

    task.add_done_callback(_cleanup)


async def _run_workspace_worker(workspace_id: str) -> None:
    from datetime import datetime, timezone

    queue = _workspace_queues.get(workspace_id)
    if queue is None:
        return

    while True:
        try:
            job = queue.get_nowait()
        except asyncio.QueueEmpty:
            # Yield once before shutdown so late enqueues can schedule cleanly.
            await asyncio.sleep(0)
            try:
                job = queue.get_nowait()
            except asyncio.QueueEmpty:
                return

        try:
            if job.status != "pending":
                continue

            job.status = "running"
            job.started_at = datetime.now(timezone.utc).isoformat()

            try:
                if job.type == "sync_workspace":
                    await _execute_sync_job(job)
                elif job.type == "rebuild_workspace":
                    await _execute_rebuild_job(job)
                else:
                    await _execute_index_job(job)
                job.status = "done"
            except Exception as exc:
                job.status = "failed"
                job.error = str(exc)
                _log(job, f"Error: {exc}")
                log.error("Job %s failed: %s", job.id, exc)
            finally:
                job.completed_at = datetime.now(timezone.utc).isoformat()
        finally:
            queue.task_done()


def _require_orchestrator() -> sync_orchestrator_adapter.SyncOrchestrator:
    if _sync_orchestrator is None:
        raise RuntimeError("jobs.init() not called")
    return _sync_orchestrator


async def _execute_sync_job(job: Job) -> None:
    orchestrator = _require_orchestrator()
    await orchestrator.execute_sync(job=job, log=lambda line: _log(job, line))
    enqueue_index(job.workspace_id, job.workspace_name, job.triggered_by)


async def _execute_index_job(job: Job) -> None:
    orchestrator = _require_orchestrator()
    await orchestrator.execute_index(job=job, log=lambda line: _log(job, line))


async def _execute_rebuild_job(job: Job) -> None:
    orchestrator = _require_orchestrator()
    await orchestrator.execute_rebuild(job=job, log=lambda line: _log(job, line))
