"""HTTP server for ContextGarden — Starlette + uvicorn.

Serves on port 7433 (CG_WEBAPP_PORT):
  /          — webapp SPA
  /api/*     — REST control plane
  /webhooks/ — GitLab push webhooks
  /mcp       — MCP StreamableHTTP (Claude Code / agents)
"""

from __future__ import annotations

import asyncio
import collections
import datetime
import logging
import os
import shutil
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Mount, Route

from .app.services import workspace_service
from .app.services.events import WorkspaceSyncRequested
from .infra.git.subprocess_git_client import SubprocessGitClient
from .infra.mirror.mirror_service import NodeMirrorService
from .infra.repo.workspace_json_repo import WorkspaceJsonRepository
from .infra.repo.webhook_inbox_sqlite import WebhookInboxSqliteRepository
from .infra.queue.inproc_event_bus import InProcEventBus
from .infra.queue.inproc_job_queue import InProcJobQueue
from .runtime_paths import resolve_mirror_cli

if TYPE_CHECKING:
    from .server import _DaemonServer

log = logging.getLogger(__name__)

HTTP_PORT = int(os.environ.get("CG_WEBAPP_PORT", "7433"))

_static_dir: Path = Path(__file__).parent / "static"

# Process-level singletons: ring buffers are intentionally module-level so
# server.py can call record_retrieval() without a request context.
_ACTIVITY_MAX = 50
_activity_ring: collections.deque[dict[str, Any]] = collections.deque(maxlen=_ACTIVITY_MAX)
_RATE_MAX = 200
_rate_ring: collections.deque[dict[str, Any]] = collections.deque(maxlen=_RATE_MAX)


def record_retrieval(query: str, result: dict[str, Any]) -> None:
    """Called by the daemon after each retrieve to record which notes were hit."""
    seed_notes = result.get("seed_notes", [])
    expanded_notes = result.get("expanded_notes", [])
    notes = [
        {
            "title": n.get("title") or Path(n.get("noteId", "")).stem,
            "workspace": n.get("workspace", ""),
            "score": round(n.get("score", 0), 3),
            "via": None,
        }
        for n in seed_notes[:8]
    ] + [
        {
            "title": n.get("title") or Path(n.get("noteId", "")).stem,
            "workspace": n.get("workspace", ""),
            "score": round(n.get("score", 0), 3),
            "via": n.get("viaEdge"),
        }
        for n in expanded_notes[:4]
    ]
    _activity_ring.appendleft({
        "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "query": query,
        "noteCount": len(seed_notes) + len(expanded_notes),
        "notes": notes,
    })


# ── Route handlers ────────────────────────────────────────────────────────────


async def _handle_index(_req: Request) -> Response:
    index_path = _static_dir / "index.html"
    if not index_path.exists():
        return Response("Dashboard not found", status_code=404, media_type="text/plain")
    return Response(content=index_path.read_bytes(), media_type="text/html")


async def _handle_gitlab_webhook(req: Request) -> JSONResponse:
    from .app.services import webhook_service

    token = req.headers.get("X-Gitlab-Token")
    workspaces = req.app.state.workspace_repo.list_all()
    headers = dict(req.headers)

    try:
        payload = await req.json()
    except Exception:
        payload = {}

    result = await webhook_service.handle_gitlab_webhook(
        token=token,
        payload=payload,
        workspaces=workspaces,
        enqueue_sync=req.app.state.job_queue.enqueue_sync,
        headers=headers,
        event_bus=req.app.state.event_bus,
        inbox_repository=req.app.state.webhook_inbox_repo,
    )
    return JSONResponse(result.body, status_code=result.status)


async def _handle_list_workspaces(req: Request) -> JSONResponse:
    entries = req.app.state.workspace_repo.list_all()
    all_jobs = req.app.state.job_queue.list_jobs()
    result = workspace_service.list_workspace_summaries(entries=entries, jobs=all_jobs)
    return JSONResponse(result)


async def _run_workspace_sync_and_reindex(daemon: Any, workspace_name: str) -> None:
    try:
        await asyncio.to_thread(daemon.handle_workspaces_sync, {})
        await asyncio.to_thread(daemon.handle_index_rebuild, {"name": workspace_name})
    except Exception as exc:
        log.error("Post-register sync/reindex failed for %s: %s", workspace_name, exc)


async def _handle_register_workspace(req: Request) -> JSONResponse:
    try:
        payload = await req.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON body"}, status_code=400)

    try:
        result = await workspace_service.register_workspace(
            payload=payload,
            data_dir=req.app.state.data_dir,
            workspace_repository=req.app.state.workspace_repo,
            git_client=req.app.state.git_client,
            mirror_service=req.app.state.mirror_service,
        )
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)

    asyncio.create_task(
        _run_workspace_sync_and_reindex(req.app.state.daemon, result.entry["name"])
    )

    return JSONResponse(
        {
            "status": "registered",
            "entry": result.entry,
            "notesGenerated": result.notes_generated,
            "notePaths": result.note_paths,
            "indexing": "running",
        },
        status_code=201,
    )


async def _handle_workspace_sync(req: Request) -> JSONResponse:
    workspace_id = req.path_params["id"]
    entries = req.app.state.workspace_repo.list_all()
    entry = next((w for w in entries if w["id"] == workspace_id), None)
    if not entry:
        return JSONResponse({"error": "Workspace not found"}, status_code=404)
    if entry.get("sourceType") != "gitlab":
        return JSONResponse({"error": "Only GitLab workspaces support sync"}, status_code=400)

    job = req.app.state.job_queue.enqueue_sync(entry["id"], entry["name"], "manual")
    return JSONResponse({"jobId": job.id, "status": job.status}, status_code=202)


async def _handle_workspace_index(req: Request) -> JSONResponse:
    workspace_id = req.path_params["id"]
    entries = req.app.state.workspace_repo.list_all()
    entry = next((w for w in entries if w["id"] == workspace_id), None)
    if not entry:
        return JSONResponse({"error": "Workspace not found"}, status_code=404)

    job = req.app.state.job_queue.enqueue_index(entry["id"], entry["name"], "manual")
    return JSONResponse({"jobId": job.id, "status": job.status}, status_code=202)


async def _handle_workspace_unregister(req: Request) -> JSONResponse:
    workspace_id = req.path_params["id"]
    result = workspace_service.unregister_workspace(
        workspace_id=workspace_id,
        data_dir=req.app.state.data_dir,
        workspace_repository=req.app.state.workspace_repo,
    )
    if not result.removed:
        return JSONResponse({"error": "Workspace not found"}, status_code=404)

    daemon_result = await asyncio.to_thread(
        req.app.state.daemon.handle_workspaces_sync, {}
    )
    return JSONResponse(
        {
            "status": "unregistered",
            "removed": result.removed,
            "deletedPaths": result.deleted_paths,
            "daemon": daemon_result,
        }
    )


async def _handle_list_jobs(req: Request) -> JSONResponse:
    return JSONResponse([
        {
            "id": j.id,
            "type": j.type,
            "workspaceName": j.workspace_name,
            "triggeredBy": j.triggered_by,
            "status": j.status,
            "error": j.error,
            "createdAt": j.created_at,
            "startedAt": j.started_at,
            "completedAt": j.completed_at,
        }
        for j in req.app.state.job_queue.list_jobs()
    ])


async def _handle_get_job(req: Request) -> JSONResponse:
    job = req.app.state.job_queue.get_job(req.path_params["id"])
    if not job:
        return JSONResponse({"error": "Job not found"}, status_code=404)
    return JSONResponse(job.to_dict())


async def _handle_status(req: Request) -> JSONResponse:
    try:
        health = await asyncio.to_thread(req.app.state.daemon.handle_daemon_health, {})
        return JSONResponse(health)
    except Exception:
        return JSONResponse({"status": "stopped"})


async def _handle_daemon_stop(req: Request) -> JSONResponse:
    await asyncio.to_thread(req.app.state.daemon.handle_daemon_shutdown, {})
    req.app.state.shutdown_fn()
    return JSONResponse({"status": "stopping"})


async def _handle_daemon_restart(req: Request) -> JSONResponse:
    req.app.state.shutdown_fn()
    return JSONResponse({"status": "restarting"})


async def _handle_config_reload(_req: Request) -> JSONResponse:
    from .config import reload_config
    reload_config()
    return JSONResponse({"status": "reloaded"})


async def _handle_activity(_req: Request) -> JSONResponse:
    return JSONResponse(list(_activity_ring))


async def _handle_api_retrieve(req: Request) -> JSONResponse:
    try:
        payload = await req.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON body"}, status_code=400)

    params: dict[str, Any] = {
        "query": payload.get("query"),
        "top_k": payload.get("top_k"),
        "workspace": payload.get("workspace"),
        "workspace_id": payload.get("workspace_id"),
    }
    if not params["query"]:
        return JSONResponse({"error": "query is required"}, status_code=400)

    try:
        result = await asyncio.to_thread(req.app.state.daemon.handle_query_retrieve, params)
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)
    return JSONResponse(result)


async def _handle_api_find_path(req: Request) -> JSONResponse:
    try:
        payload = await req.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON body"}, status_code=400)

    start = payload.get("start")
    end = payload.get("end")
    if not start or not end:
        return JSONResponse({"error": "start and end are required"}, status_code=400)

    params: dict[str, Any] = {
        "start": start,
        "end": end,
        "max_depth": payload.get("max_depth"),
        "edge_types": payload.get("edge_types"),
        "workspace": payload.get("workspace"),
        "workspace_id": payload.get("workspace_id"),
    }
    try:
        result = await asyncio.to_thread(req.app.state.daemon.handle_query_find_path, params)
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)
    return JSONResponse(result)


async def _handle_api_rate(req: Request) -> JSONResponse:
    try:
        payload = await req.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON body"}, status_code=400)

    retrieval_id = payload.get("retrieval_id")
    query = payload.get("query")
    score = payload.get("score")
    helpful = payload.get("helpful", "")
    missing = payload.get("missing", "")

    if not retrieval_id or not query:
        return JSONResponse({"error": "retrieval_id and query are required"}, status_code=400)
    if not isinstance(score, int) or score < 1 or score > 5:
        return JSONResponse({"error": "score must be an integer in range 1-5"}, status_code=400)

    _rate_ring.appendleft({
        "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "retrieval_id": retrieval_id,
        "query": query,
        "score": score,
        "helpful": helpful,
        "missing": missing,
    })
    return JSONResponse({"status": "recorded"})


async def _handle_api_note(req: Request) -> JSONResponse:
    relative_path = req.query_params.get("relative_path", "").strip()
    if not relative_path:
        return JSONResponse({"error": "relative_path is required"}, status_code=400)

    params: dict[str, Any] = {"relative_path": relative_path}
    workspace_id = req.query_params.get("workspace_id", "").strip()
    if workspace_id:
        params["workspace_id"] = workspace_id

    try:
        result = await asyncio.to_thread(req.app.state.daemon.handle_query_get_note_content, params)
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)
    return JSONResponse(result)


async def _handle_api_stats(req: Request) -> JSONResponse:
    params: dict[str, Any] = {}
    workspace = req.query_params.get("workspace", "").strip()
    workspace_id = req.query_params.get("workspace_id", "").strip()
    if workspace:
        params["workspace"] = workspace
    if workspace_id:
        params["workspace_id"] = workspace_id

    try:
        result = await asyncio.to_thread(req.app.state.daemon.handle_query_stats, params)
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)
    return JSONResponse(result)


# ── App factory ───────────────────────────────────────────────────────────────


def make_http_app(
    daemon: "_DaemonServer",
    data_dir: Path,
    shutdown_fn: "Callable[[], None] | None" = None,
) -> Starlette:
    job_queue = InProcJobQueue()
    event_bus = InProcEventBus()
    workspace_repo = WorkspaceJsonRepository(data_dir)
    webhook_inbox_repo = WebhookInboxSqliteRepository(data_dir)
    git_client = SubprocessGitClient()
    mirror_cli = resolve_mirror_cli(data_dir=data_dir)
    mirror_service = NodeMirrorService(
        data_dir=data_dir,
        node_bin=shutil.which("node") or "node",
        mirror_cli=mirror_cli,
    )

    async def _on_workspace_sync_requested(event: WorkspaceSyncRequested) -> None:
        job = job_queue.enqueue_sync(event.workspace_id, event.workspace_name, "webhook")
        event.job_id = job.id

    event_bus.subscribe(WorkspaceSyncRequested, _on_workspace_sync_requested)

    def _noop_shutdown() -> None:
        log.warning("Shutdown requested but no shutdown_fn configured")

    routes = [
        Route("/", _handle_index, methods=["GET"]),
        Route("/webhooks/gitlab", _handle_gitlab_webhook, methods=["POST"]),

        Route("/api/workspaces", _handle_register_workspace, methods=["POST"]),
        Route("/api/workspaces", _handle_list_workspaces, methods=["GET"]),
        Route("/workspaces", _handle_list_workspaces, methods=["GET"]),
        Route("/workspaces/{id}/sync", _handle_workspace_sync, methods=["POST"]),
        Route("/workspaces/{id}/index", _handle_workspace_index, methods=["POST"]),
        Route("/workspaces/{id}/unregister", _handle_workspace_unregister, methods=["POST"]),
        Route("/api/workspaces/{id}/unregister", _handle_workspace_unregister, methods=["POST"]),

        Route("/jobs", _handle_list_jobs, methods=["GET"]),
        Route("/jobs/{id}", _handle_get_job, methods=["GET"]),
        Route("/api/jobs", _handle_list_jobs, methods=["GET"]),
        Route("/api/jobs/{id}", _handle_get_job, methods=["GET"]),

        Route("/api/activity", _handle_activity, methods=["GET"]),
        Route("/api/retrieve", _handle_api_retrieve, methods=["POST"]),
        Route("/api/find-path", _handle_api_find_path, methods=["POST"]),
        Route("/api/rate", _handle_api_rate, methods=["POST"]),
        Route("/api/note", _handle_api_note, methods=["GET"]),
        Route("/api/stats", _handle_api_stats, methods=["GET"]),

        Route("/status", _handle_status, methods=["GET"]),
        Route("/api/status", _handle_status, methods=["GET"]),
        Route("/api/health", _handle_status, methods=["GET"]),

        Route("/daemon/stop", _handle_daemon_stop, methods=["POST"]),
        Route("/api/daemon/stop", _handle_daemon_stop, methods=["POST"]),
        Route("/api/daemon/restart", _handle_daemon_restart, methods=["POST"]),
        Route("/api/config/reload", _handle_config_reload, methods=["POST"]),
    ]

    try:
        from .mcp_tools import create_mcp_server
    except ModuleNotFoundError as exc:
        log.warning("MCP server disabled (optional dependency missing): %s", exc)
    else:
        mcp_server = create_mcp_server(
            daemon=daemon,
            data_dir=data_dir,
            job_queue=job_queue,
            workspace_repo=workspace_repo,
            mirror_service=mirror_service,
            activity_ring=_activity_ring,
            rate_ring=_rate_ring,
        )
        routes.append(Mount("/mcp", app=mcp_server.streamable_http_app()))

    app = Starlette(routes=routes)
    app.state.daemon = daemon
    app.state.data_dir = data_dir
    app.state.workspace_repo = workspace_repo
    app.state.webhook_inbox_repo = webhook_inbox_repo
    app.state.job_queue = job_queue
    app.state.event_bus = event_bus
    app.state.git_client = git_client
    app.state.mirror_service = mirror_service
    app.state.shutdown_fn = shutdown_fn or _noop_shutdown
    return app
