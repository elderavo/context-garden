"""Job queue for the ContextGarden control plane.

Asyncio-native port of webapp/jobs.ts. Processes jobs serially
(one worker task per runtime). All state is in-memory; jobs are
not persisted across daemon restarts.

Job types:
  sync_workspace  — git fetch+reset, mirror, reindex
  index_workspace — reindex only
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Literal, Optional

log = logging.getLogger(__name__)

# ── Types ────────────────────────────────────────────────────────────────────

JobType = Literal["sync_workspace", "index_workspace"]
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

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "type": self.type,
            "workspaceId": self.workspace_id,
            "workspaceName": self.workspace_name,
            "triggeredBy": self.triggered_by,
            "status": self.status,
            "createdAt": self.created_at,
            "startedAt": self.started_at,
            "completedAt": self.completed_at,
            "log": self.log,
        }


# ── Module state ─────────────────────────────────────────────────────────────

_jobs: list[Job] = []
_worker_task: Optional[asyncio.Task[None]] = None
_data_dir: Path = Path.cwd()
_node_bin: str = "node"
_mirror_cli: Optional[str] = None

# Injected by init() — avoids circular import from daemon.py
_get_daemon: Optional[Callable[[], Any]] = None


def init(data_dir: str | Path, node_bin: str, mirror_cli: str, get_daemon: Callable[[], Any]) -> None:
    """Call once at daemon startup."""
    global _data_dir, _node_bin, _mirror_cli, _get_daemon
    _data_dir = Path(data_dir)
    _node_bin = node_bin
    _mirror_cli = mirror_cli
    _get_daemon = get_daemon


# ── Public API ────────────────────────────────────────────────────────────────

def enqueue_sync(
    workspace_id: str,
    workspace_name: str,
    triggered_by: Literal["webhook", "manual"] = "manual",
) -> Job:
    existing = _find_active(workspace_id, "sync_workspace")
    if existing:
        return existing
    job = _make_job("sync_workspace", workspace_id, workspace_name, triggered_by)
    _push(job)
    _schedule_worker()
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
    _push(job)
    _schedule_worker()
    return job


def list_jobs() -> list[Job]:
    return list(reversed(_jobs))


def get_job(job_id: str) -> Optional[Job]:
    return next((j for j in _jobs if j.id == job_id), None)


# ── Internal helpers ──────────────────────────────────────────────────────────

def _find_active(workspace_id: str, job_type: JobType) -> Optional[Job]:
    return next(
        (j for j in _jobs
         if j.workspace_id == workspace_id
         and j.type == job_type
         and j.status in ("pending", "running")),
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


def _push(job: Job) -> None:
    _jobs.append(job)
    if len(_jobs) > MAX_HISTORY:
        del _jobs[: len(_jobs) - MAX_HISTORY]


def _log(job: Job, line: str) -> None:
    from datetime import datetime, timezone
    job.log.append(f"[{datetime.now(timezone.utc).isoformat()}] {line}")


def _schedule_worker() -> None:
    global _worker_task
    if _worker_task is None or _worker_task.done():
        _worker_task = asyncio.ensure_future(_run_worker())


# ── Worker ────────────────────────────────────────────────────────────────────

async def _run_worker() -> None:
    from datetime import datetime, timezone

    while True:
        job = next((j for j in _jobs if j.status == "pending"), None)
        if not job:
            return

        job.status = "running"
        job.started_at = datetime.now(timezone.utc).isoformat()

        try:
            if job.type == "sync_workspace":
                await _execute_sync_job(job)
            else:
                await _execute_index_job(job)
            job.status = "done"
        except Exception as exc:
            job.status = "failed"
            _log(job, f"Error: {exc}")
            log.error("Job %s failed: %s", job.id, exc)

        job.completed_at = datetime.now(timezone.utc).isoformat()


# ── Job execution ─────────────────────────────────────────────────────────────

def _load_workspace(workspace_id: str) -> dict[str, Any]:
    ws_path = _data_dir / ".context-garden" / "workspaces.json"
    entries = json.loads(ws_path.read_text("utf-8"))
    entry = next((e for e in entries if e["id"] == workspace_id), None)
    if not entry:
        raise KeyError(f"Workspace {workspace_id} not found in registry")
    return entry


def _resolve_token(gitlab_config: dict[str, Any]) -> Optional[str]:
    if gitlab_config.get("accessToken"):
        return gitlab_config["accessToken"]
    if os.environ.get("CG_GITLAB_TOKEN"):
        return os.environ["CG_GITLAB_TOKEN"]
    env_path = Path.home() / ".context-garden" / ".env"
    if env_path.exists():
        for line in env_path.read_text("utf-8").splitlines():
            stripped = line.strip()
            if stripped.startswith("CG_GITLAB_TOKEN="):
                val = stripped[len("CG_GITLAB_TOKEN="):].strip()
                return val or None
    return None


def _build_auth_url(project_url: str, token: Optional[str]) -> str:
    if project_url.startswith("git@") or project_url.startswith("ssh://"):
        return project_url if project_url.endswith(".git") else f"{project_url}.git"
    normalized = project_url if project_url.endswith(".git") else f"{project_url}.git"
    if not token:
        return normalized
    from urllib.parse import urlparse, urlunparse
    parsed = urlparse(normalized)
    authed = parsed._replace(netloc=f"oauth2:{token}@{parsed.hostname}:{parsed.port}" if parsed.port else f"oauth2:{token}@{parsed.hostname}")
    return urlunparse(authed)


async def _run_git(args: list[str], cwd: Optional[str] = None) -> None:
    proc = await asyncio.create_subprocess_exec(
        "git", *args,
        cwd=cwd,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr_bytes = await proc.communicate()
    if proc.returncode != 0:
        stderr = stderr_bytes.decode("utf-8", errors="replace").strip() if stderr_bytes else ""
        raise RuntimeError(f"git {args[0]} failed (exit {proc.returncode}): {stderr}")


async def _fetch_and_reset(gitlab_config: dict[str, Any], token: Optional[str]) -> None:
    clone_dir = gitlab_config["cloneDir"]
    branch = gitlab_config["branch"]
    auth_url = _build_auth_url(gitlab_config["projectUrl"], token)
    await _run_git(["remote", "set-url", "origin", auth_url], cwd=clone_dir)
    await _run_git(["fetch", "origin", branch], cwd=clone_dir)
    await _run_git(["reset", "--hard", f"origin/{branch}"], cwd=clone_dir)


async def _run_mirror(entry: dict[str, Any], gitlab_config: dict[str, Any]) -> dict[str, Any]:
    assert _mirror_cli is not None, "jobs.init() not called"
    assert _node_bin, "jobs.init() not called"

    mirror_dir = str(_data_dir / "md_db" / "code" / entry["name"])
    opts = {
        "scanDir": gitlab_config["cloneDir"],
        "mirrorDir": mirror_dir,
        "languages": entry.get("languages", []),
        "force": False,
        "workspace": entry["name"],
        "wikilinkPrefix": f"code/{entry['name']}",
        "omitPatterns": entry.get("omitPatterns"),
    }

    proc = await asyncio.create_subprocess_exec(
        _node_bin, _mirror_cli, json.dumps(opts),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout_bytes, stderr_bytes = await proc.communicate()
    stdout = stdout_bytes.decode("utf-8", errors="replace").strip() if stdout_bytes else ""

    try:
        result = json.loads(stdout)
    except json.JSONDecodeError:
        stderr = stderr_bytes.decode("utf-8", errors="replace").strip() if stderr_bytes else ""
        raise RuntimeError(f"Mirror CLI produced non-JSON output: {stdout!r}\nstderr: {stderr}")

    if "error" in result:
        raise RuntimeError(f"Mirror CLI error: {result['error']}")

    return result


async def _trigger_reindex(job: Job, workspace_name: str) -> None:
    assert _get_daemon is not None, "jobs.init() not called"
    _log(job, "Triggering daemon reindex...")
    try:
        daemon = _get_daemon()
        await asyncio.to_thread(daemon.handle_index_rebuild, {"name": workspace_name})
        _log(job, "Reindex queued.")
    except Exception as exc:
        _log(job, f"Warning: reindex failed (daemon may be busy): {exc}")


async def _execute_sync_job(job: Job) -> None:
    entry = _load_workspace(job.workspace_id)

    if entry.get("sourceType") != "gitlab" or not entry.get("gitlabConfig"):
        raise ValueError(f"Workspace \"{entry['name']}\" is not a GitLab workspace")

    gitlab_config = entry["gitlabConfig"]
    token = _resolve_token(gitlab_config)

    _log(job, f"Fetching {gitlab_config['projectUrl']} ({gitlab_config['branch']})...")
    await _fetch_and_reset(gitlab_config, token)
    _log(job, "Fetch complete.")

    _log(job, "Mirroring...")
    result = await _run_mirror(entry, gitlab_config)
    written = sum(result.get("written", {}).values()) if isinstance(result.get("written"), dict) else 0
    _log(job, f"Mirror complete. Notes written: {written}.")

    await _trigger_reindex(job, entry["name"])


async def _execute_index_job(job: Job) -> None:
    await _trigger_reindex(job, job.workspace_name)
