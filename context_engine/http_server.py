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
import json
import logging
import os
import shutil
from pathlib import Path
from typing import TYPE_CHECKING, Any

from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Mount, Route

from .app.services import workspace_service
from .app.services.events import WorkspaceSyncRequested
from .infra.git.subprocess_git_client import SubprocessGitClient
from .infra.mirror.mirror_service_legacy import MirrorServiceLegacy
from .infra.repo.workspace_json_repo import WorkspaceJsonRepository
from .infra.repo.webhook_inbox_sqlite import WebhookInboxSqliteRepository
from .infra.queue.inproc_event_bus import InProcEventBus
from .infra.queue.inproc_job_queue import InProcJobQueue

if TYPE_CHECKING:
    from .daemon import _DaemonServer

log = logging.getLogger(__name__)

HTTP_PORT = int(os.environ.get("CG_WEBAPP_PORT", "7433"))

# Populated by make_http_app()
_daemon_ref: "_DaemonServer | None" = None
_data_dir_ref: Path = Path.cwd()
_static_dir: Path = Path(__file__).parent / "static"
_job_queue_ref = InProcJobQueue()
_workspace_repo_ref: WorkspaceJsonRepository | None = None
_webhook_inbox_repo_ref: WebhookInboxSqliteRepository | None = None
_event_bus_ref = InProcEventBus()
_event_bus_ready = False
_git_client_ref = SubprocessGitClient()
_mirror_service_ref: MirrorServiceLegacy | None = None

# ── Retrieval activity ring buffer ────────────────────────────────────────────
_ACTIVITY_MAX = 50
_activity_ring: collections.deque[dict[str, Any]] = collections.deque(maxlen=_ACTIVITY_MAX)
_RATE_MAX = 200
_rate_ring: collections.deque[dict[str, Any]] = collections.deque(maxlen=_RATE_MAX)


def record_retrieval(query: str, result: dict[str, Any]) -> None:
    """Called by daemon after each query.retrieve to record which notes were hit."""
    seed_notes = result.get("seed_notes", [])
    expanded_notes = result.get("expanded_notes", [])
    notes = [
        {"title": n.get("title") or Path(n.get("noteId", "")).stem, "workspace": n.get("workspace", ""), "score": round(n.get("score", 0), 3), "via": None}
        for n in seed_notes[:8]
    ] + [
        {"title": n.get("title") or Path(n.get("noteId", "")).stem, "workspace": n.get("workspace", ""), "score": round(n.get("score", 0), 3), "via": n.get("viaEdge")}
        for n in expanded_notes[:4]
    ]
    _activity_ring.appendleft({
        "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "query": query,
        "noteCount": len(seed_notes) + len(expanded_notes),
        "notes": notes,
    })


# ── Workspace helpers ────────────────────────────────────────────────────────


def _load_workspaces() -> list[dict[str, Any]]:
    if _workspace_repo_ref is not None:
        return _workspace_repo_ref.list_all()

    path = _data_dir_ref / ".context-garden" / "workspaces.json"
    if not path.exists():
        return []
    try:
        return json.loads(path.read_text("utf-8"))
    except Exception:
        return []


def _save_workspaces(entries: list[dict[str, Any]]) -> None:
    if _workspace_repo_ref is not None:
        _workspace_repo_ref.save_all(entries)
        return

    path = _data_dir_ref / ".context-garden" / "workspaces.json"
    path.write_text(json.dumps(entries, indent=2), "utf-8")


# ── Route handlers ────────────────────────────────────────────────────────────


async def _handle_index(_req: Request) -> Response:
    index_path = _static_dir / "index.html"
    if not index_path.exists():
        return Response("Dashboard not found", status_code=404, media_type="text/plain")
    return Response(content=index_path.read_bytes(), media_type="text/html")


async def _handle_gitlab_webhook(req: Request) -> JSONResponse:
    from .app.services import webhook_service

    token = req.headers.get("X-Gitlab-Token")
    workspaces = _load_workspaces()
    headers = dict(req.headers)

    try:
        payload = await req.json()
    except Exception:
        payload = {}

    result = await webhook_service.handle_gitlab_webhook(
        token=token,
        payload=payload,
        workspaces=workspaces,
        enqueue_sync=_job_queue_ref.enqueue_sync,
        headers=headers,
        event_bus=_event_bus_ref,
        inbox_repository=_webhook_inbox_repo_ref,
    )
    return JSONResponse(result.body, status_code=result.status)


async def _handle_list_workspaces(_req: Request) -> JSONResponse:
    entries = _load_workspaces()
    all_jobs = _job_queue_ref.list_jobs()
    result = workspace_service.list_workspace_summaries(entries=entries, jobs=all_jobs)
    return JSONResponse(result)


async def _run_workspace_sync_and_reindex(workspace_name: str) -> None:
    if _daemon_ref is None:
        return
    try:
        await asyncio.to_thread(_daemon_ref.handle_workspaces_sync, {})
        await asyncio.to_thread(_daemon_ref.handle_index_rebuild, {"name": workspace_name})
    except Exception as exc:
        log.error("Workspace post-register sync/reindex failed for %s: %s", workspace_name, exc)


async def _handle_register_workspace(req: Request) -> JSONResponse:
    assert _workspace_repo_ref is not None
    assert _mirror_service_ref is not None

    try:
        payload = await req.json()
    except Exception:
        return JSONResponse({"error": "Invalid JSON body"}, status_code=400)

    try:
        result = await workspace_service.register_workspace(
            payload=payload,
            data_dir=_data_dir_ref,
            workspace_repository=_workspace_repo_ref,
            git_client=_git_client_ref,
            mirror_service=_mirror_service_ref,
        )
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)

    asyncio.create_task(_run_workspace_sync_and_reindex(result.entry["name"]))

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
    workspaces = _load_workspaces()
    entry = next((w for w in workspaces if w["id"] == workspace_id), None)
    if not entry:
        return JSONResponse({"error": "Workspace not found"}, status_code=404)
    if entry.get("sourceType") != "gitlab":
        return JSONResponse({"error": "Only GitLab workspaces support sync"}, status_code=400)

    job = _job_queue_ref.enqueue_sync(entry["id"], entry["name"], "manual")
    return JSONResponse({"jobId": job.id, "status": job.status}, status_code=202)


async def _handle_workspace_index(req: Request) -> JSONResponse:
    workspace_id = req.path_params["id"]
    workspaces = _load_workspaces()
    entry = next((w for w in workspaces if w["id"] == workspace_id), None)
    if not entry:
        return JSONResponse({"error": "Workspace not found"}, status_code=404)

    job = _job_queue_ref.enqueue_index(entry["id"], entry["name"], "manual")
    return JSONResponse({"jobId": job.id, "status": job.status}, status_code=202)


async def _handle_workspace_unregister(req: Request) -> JSONResponse:
    assert _daemon_ref is not None
    assert _workspace_repo_ref is not None

    workspace_id = req.path_params["id"]
    result = workspace_service.unregister_workspace(
        workspace_id=workspace_id,
        data_dir=_data_dir_ref,
        workspace_repository=_workspace_repo_ref,
    )
    if not result.removed:
        return JSONResponse({"error": "Workspace not found"}, status_code=404)

    daemon_result = await asyncio.to_thread(
        _daemon_ref.handle_workspaces_sync, {}
    )
    return JSONResponse(
        {
            "status": "unregistered",
            "removed": result.removed,
            "deletedPaths": result.deleted_paths,
            "daemon": daemon_result,
        }
    )


async def _handle_list_jobs(_req: Request) -> JSONResponse:
    return JSONResponse([
        {
            "id": j.id,
            "type": j.type,
            "workspaceName": j.workspace_name,
            "triggeredBy": j.triggered_by,
            "status": j.status,
            "createdAt": j.created_at,
            "startedAt": j.started_at,
            "completedAt": j.completed_at,
        }
        for j in _job_queue_ref.list_jobs()
    ])


async def _handle_get_job(req: Request) -> JSONResponse:
    job = _job_queue_ref.get_job(req.path_params["id"])
    if not job:
        return JSONResponse({"error": "Job not found"}, status_code=404)
    return JSONResponse(job.to_dict())


async def _handle_status(_req: Request) -> JSONResponse:
    assert _daemon_ref is not None
    try:
        health = await asyncio.to_thread(_daemon_ref.handle_daemon_health, {})
        return JSONResponse(health)
    except Exception:
        return JSONResponse({"status": "stopped"})


async def _handle_daemon_stop(_req: Request) -> JSONResponse:
    assert _daemon_ref is not None
    result = await asyncio.to_thread(_daemon_ref.handle_daemon_shutdown, {})
    return JSONResponse(result)


async def _handle_activity(_req: Request) -> JSONResponse:
    return JSONResponse(list(_activity_ring))


async def _handle_api_retrieve(req: Request) -> JSONResponse:
    assert _daemon_ref is not None

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
        result = await asyncio.to_thread(_daemon_ref.handle_query_retrieve, params)
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)
    return JSONResponse(result)


async def _handle_api_find_path(req: Request) -> JSONResponse:
    assert _daemon_ref is not None

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
        result = await asyncio.to_thread(_daemon_ref.handle_query_find_path, params)
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

    item = {
        "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "retrieval_id": retrieval_id,
        "query": query,
        "score": score,
        "helpful": helpful,
        "missing": missing,
    }
    _rate_ring.appendleft(item)
    return JSONResponse({"status": "recorded"})


async def _handle_api_note(req: Request) -> JSONResponse:
    assert _daemon_ref is not None

    relative_path = req.query_params.get("relative_path", "").strip()
    if not relative_path:
        return JSONResponse({"error": "relative_path is required"}, status_code=400)

    params: dict[str, Any] = {"relative_path": relative_path}
    workspace_id = req.query_params.get("workspace_id", "").strip()
    if workspace_id:
        params["workspace_id"] = workspace_id

    try:
        result = await asyncio.to_thread(_daemon_ref.handle_query_get_note_content, params)
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)
    return JSONResponse(result)


async def _handle_api_stats(req: Request) -> JSONResponse:
    assert _daemon_ref is not None

    params: dict[str, Any] = {}
    workspace = req.query_params.get("workspace", "").strip()
    workspace_id = req.query_params.get("workspace_id", "").strip()
    if workspace:
        params["workspace"] = workspace
    if workspace_id:
        params["workspace_id"] = workspace_id

    try:
        result = await asyncio.to_thread(_daemon_ref.handle_query_stats, params)
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=500)
    return JSONResponse(result)


async def _handle_daemon_restart(_req: Request) -> JSONResponse:
    asyncio.get_running_loop().call_later(0.25, lambda: os._exit(0))
    return JSONResponse({"status": "restarting"})


async def _on_workspace_sync_requested(event: WorkspaceSyncRequested) -> None:
    job = _job_queue_ref.enqueue_sync(event.workspace_id, event.workspace_name, "webhook")
    event.job_id = job.id


# ── App factory ───────────────────────────────────────────────────────────────


def make_http_app(daemon: "_DaemonServer", data_dir: Path) -> Starlette:
    global _daemon_ref, _data_dir_ref, _workspace_repo_ref, _webhook_inbox_repo_ref, _event_bus_ready
    global _mirror_service_ref
    _daemon_ref = daemon
    _data_dir_ref = data_dir
    _workspace_repo_ref = WorkspaceJsonRepository(data_dir)
    _webhook_inbox_repo_ref = WebhookInboxSqliteRepository(data_dir)
    _node_bin = shutil.which("node") or "node"
    _mirror_cli = str(data_dir / "dist" / "src" / "mirror" / "mirror-cli.js")
    _mirror_service_ref = MirrorServiceLegacy(
        data_dir=data_dir,
        node_bin=_node_bin,
        mirror_cli=_mirror_cli,
    )
    if not _event_bus_ready:
        _event_bus_ref.subscribe(WorkspaceSyncRequested, _on_workspace_sync_requested)
        _event_bus_ready = True

    from .mcp_tools import create_mcp_server
    mcp_server = create_mcp_server(
        daemon=daemon,
        data_dir=data_dir,
        job_queue=_job_queue_ref,
        workspace_repo=_workspace_repo_ref,
        mirror_service=_mirror_service_ref,
        activity_ring=_activity_ring,
    )

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

        Route("/daemon/stop", _handle_daemon_stop, methods=["POST"]),
        Route("/api/daemon/stop", _handle_daemon_stop, methods=["POST"]),
        Route("/api/daemon/restart", _handle_daemon_restart, methods=["POST"]),

        Mount("/mcp", app=mcp_server.streamable_http_app()),
    ]

    return Starlette(routes=routes)
