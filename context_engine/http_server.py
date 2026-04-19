"""HTTP control plane for the ContextGarden daemon.

Serves on port 7433 (CG_WEBAPP_PORT) alongside the TCP JSON-RPC server.
Routes mirror webapp/server.ts 1-to-1.
"""

from __future__ import annotations

import collections
import datetime
import json
import logging
import os
import shutil
from pathlib import Path
from typing import TYPE_CHECKING, Any

from aiohttp import web
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


async def _handle_index(_req: web.Request) -> web.Response:
    index_path = _static_dir / "index.html"
    if not index_path.exists():
        return web.Response(status=404, text="Dashboard not found")
    return web.Response(body=index_path.read_bytes(), content_type="text/html")


async def _handle_gitlab_webhook(req: web.Request) -> web.Response:
    from .app.services import webhook_service

    token = req.headers.get("X-Gitlab-Token")
    workspaces = _load_workspaces()
    headers = {key: value for key, value in req.headers.items()}

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
    return web.json_response(result.body, status=result.status)


async def _handle_list_workspaces(_req: web.Request) -> web.Response:
    entries = _load_workspaces()
    all_jobs = _job_queue_ref.list_jobs()
    result = workspace_service.list_workspace_summaries(entries=entries, jobs=all_jobs)
    return web.json_response(result)


async def _run_workspace_sync_and_reindex(workspace_name: str) -> None:
    if _daemon_ref is None:
        return
    import asyncio
    try:
        await asyncio.to_thread(_daemon_ref.handle_workspaces_sync, {})
        await asyncio.to_thread(_daemon_ref.handle_index_rebuild, {"name": workspace_name})
    except Exception as exc:
        log.error("Workspace post-register sync/reindex failed for %s: %s", workspace_name, exc)


async def _handle_register_workspace(req: web.Request) -> web.Response:
    assert _workspace_repo_ref is not None
    assert _mirror_service_ref is not None

    try:
        payload = await req.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON body"}, status=400)

    try:
        result = await workspace_service.register_workspace(
            payload=payload,
            data_dir=_data_dir_ref,
            workspace_repository=_workspace_repo_ref,
            git_client=_git_client_ref,
            mirror_service=_mirror_service_ref,
        )
    except Exception as exc:
        return web.json_response({"error": str(exc)}, status=400)

    import asyncio
    asyncio.create_task(_run_workspace_sync_and_reindex(result.entry["name"]))

    return web.json_response(
        {
            "status": "registered",
            "entry": result.entry,
            "notesGenerated": result.notes_generated,
            "notePaths": result.note_paths,
            "indexing": "running",
        },
        status=201,
    )


async def _handle_workspace_sync(req: web.Request) -> web.Response:
    workspace_id = req.match_info["id"]
    workspaces = _load_workspaces()
    entry = next((w for w in workspaces if w["id"] == workspace_id), None)
    if not entry:
        return web.json_response({"error": "Workspace not found"}, status=404)
    if entry.get("sourceType") != "gitlab":
        return web.json_response({"error": "Only GitLab workspaces support sync"}, status=400)

    job = _job_queue_ref.enqueue_sync(entry["id"], entry["name"], "manual")
    return web.json_response({"jobId": job.id, "status": job.status}, status=202)


async def _handle_workspace_index(req: web.Request) -> web.Response:
    workspace_id = req.match_info["id"]
    workspaces = _load_workspaces()
    entry = next((w for w in workspaces if w["id"] == workspace_id), None)
    if not entry:
        return web.json_response({"error": "Workspace not found"}, status=404)

    job = _job_queue_ref.enqueue_index(entry["id"], entry["name"], "manual")
    return web.json_response({"jobId": job.id, "status": job.status}, status=202)


async def _handle_workspace_unregister(req: web.Request) -> web.Response:
    assert _daemon_ref is not None
    assert _workspace_repo_ref is not None

    workspace_id = req.match_info["id"]
    result = workspace_service.unregister_workspace(
        workspace_id=workspace_id,
        data_dir=_data_dir_ref,
        workspace_repository=_workspace_repo_ref,
    )
    if not result.removed:
        return web.json_response({"error": "Workspace not found"}, status=404)

    import asyncio
    daemon_result = await asyncio.to_thread(
        _daemon_ref.handle_workspaces_sync, {}
    )
    return web.json_response(
        {
            "status": "unregistered",
            "removed": result.removed,
            "deletedPaths": result.deleted_paths,
            "daemon": daemon_result,
        }
    )


async def _handle_list_jobs(_req: web.Request) -> web.Response:
    return web.json_response([
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


async def _handle_get_job(req: web.Request) -> web.Response:
    job = _job_queue_ref.get_job(req.match_info["id"])
    if not job:
        return web.json_response({"error": "Job not found"}, status=404)
    return web.json_response(job.to_dict())


async def _handle_status(_req: web.Request) -> web.Response:
    assert _daemon_ref is not None
    import asyncio
    try:
        health = await asyncio.to_thread(_daemon_ref.handle_daemon_health, {})
        return web.json_response(health)
    except Exception:
        return web.json_response({"status": "stopped"})


async def _handle_daemon_stop(_req: web.Request) -> web.Response:
    assert _daemon_ref is not None
    import asyncio
    result = await asyncio.to_thread(_daemon_ref.handle_daemon_shutdown, {})
    return web.json_response(result)


async def _handle_activity(_req: web.Request) -> web.Response:
    return web.json_response(list(_activity_ring))


async def _handle_api_retrieve(req: web.Request) -> web.Response:
    assert _daemon_ref is not None
    import asyncio

    try:
        payload = await req.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON body"}, status=400)

    params: dict[str, Any] = {
        "query": payload.get("query"),
        "top_k": payload.get("top_k"),
        "workspace": payload.get("workspace"),
        "workspace_id": payload.get("workspace_id"),
    }
    if not params["query"]:
        return web.json_response({"error": "query is required"}, status=400)

    try:
        result = await asyncio.to_thread(_daemon_ref.handle_query_retrieve, params)
    except Exception as exc:
        return web.json_response({"error": str(exc)}, status=500)
    return web.json_response(result)


async def _handle_api_find_path(req: web.Request) -> web.Response:
    assert _daemon_ref is not None
    import asyncio

    try:
        payload = await req.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON body"}, status=400)

    start = payload.get("start")
    end = payload.get("end")
    if not start or not end:
        return web.json_response({"error": "start and end are required"}, status=400)

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
        return web.json_response({"error": str(exc)}, status=500)
    return web.json_response(result)


async def _handle_api_rate(req: web.Request) -> web.Response:
    try:
        payload = await req.json()
    except Exception:
        return web.json_response({"error": "Invalid JSON body"}, status=400)

    retrieval_id = payload.get("retrieval_id")
    query = payload.get("query")
    score = payload.get("score")
    helpful = payload.get("helpful", "")
    missing = payload.get("missing", "")

    if not retrieval_id or not query:
        return web.json_response({"error": "retrieval_id and query are required"}, status=400)
    if not isinstance(score, int) or score < 1 or score > 5:
        return web.json_response({"error": "score must be an integer in range 1-5"}, status=400)

    item = {
        "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "retrieval_id": retrieval_id,
        "query": query,
        "score": score,
        "helpful": helpful,
        "missing": missing,
    }
    _rate_ring.appendleft(item)
    return web.json_response({"status": "recorded"})


async def _handle_api_note(req: web.Request) -> web.Response:
    assert _daemon_ref is not None
    import asyncio

    relative_path = req.query.get("relative_path", "").strip()
    if not relative_path:
        return web.json_response({"error": "relative_path is required"}, status=400)

    params: dict[str, Any] = {
        "relative_path": relative_path,
    }
    workspace_id = req.query.get("workspace_id", "").strip()
    if workspace_id:
        params["workspace_id"] = workspace_id

    try:
        result = await asyncio.to_thread(_daemon_ref.handle_query_get_note_content, params)
    except Exception as exc:
        return web.json_response({"error": str(exc)}, status=500)
    return web.json_response(result)


async def _handle_api_stats(req: web.Request) -> web.Response:
    assert _daemon_ref is not None
    import asyncio

    params: dict[str, Any] = {}
    workspace = req.query.get("workspace", "").strip()
    workspace_id = req.query.get("workspace_id", "").strip()
    if workspace:
        params["workspace"] = workspace
    if workspace_id:
        params["workspace_id"] = workspace_id

    try:
        result = await asyncio.to_thread(_daemon_ref.handle_query_stats, params)
    except Exception as exc:
        return web.json_response({"error": str(exc)}, status=500)
    return web.json_response(result)


async def _handle_daemon_restart(_req: web.Request) -> web.Response:
    import asyncio
    # Just exit — the TS daemon-client detects the TCP drop and respawns.
    # Spawning our own subprocess here races with that and causes double-starts.
    asyncio.get_running_loop().call_later(0.25, lambda: os._exit(0))
    return web.json_response({"status": "restarting"})


async def _on_workspace_sync_requested(event: WorkspaceSyncRequested) -> None:
    job = _job_queue_ref.enqueue_sync(event.workspace_id, event.workspace_name, "webhook")
    event.job_id = job.id


# ── App factory ───────────────────────────────────────────────────────────────


def make_http_app(daemon: "_DaemonServer", data_dir: Path) -> web.Application:
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

    app = web.Application()

    app.router.add_get("/", _handle_index)
    app.router.add_post("/webhooks/gitlab", _handle_gitlab_webhook)

    app.router.add_post("/api/workspaces", _handle_register_workspace)
    app.router.add_get("/api/workspaces", _handle_list_workspaces)
    app.router.add_get("/workspaces", _handle_list_workspaces)
    app.router.add_post("/workspaces/{id}/sync", _handle_workspace_sync)
    app.router.add_post("/workspaces/{id}/index", _handle_workspace_index)
    app.router.add_post("/workspaces/{id}/unregister", _handle_workspace_unregister)
    app.router.add_post("/api/workspaces/{id}/unregister", _handle_workspace_unregister)

    app.router.add_get("/jobs", _handle_list_jobs)
    app.router.add_get("/jobs/{id}", _handle_get_job)
    app.router.add_get("/api/jobs", _handle_list_jobs)
    app.router.add_get("/api/jobs/{id}", _handle_get_job)

    app.router.add_get("/api/activity", _handle_activity)
    app.router.add_post("/api/retrieve", _handle_api_retrieve)
    app.router.add_post("/api/find-path", _handle_api_find_path)
    app.router.add_post("/api/rate", _handle_api_rate)
    app.router.add_get("/api/note", _handle_api_note)
    app.router.add_get("/api/stats", _handle_api_stats)

    app.router.add_get("/status", _handle_status)
    app.router.add_get("/api/status", _handle_status)

    app.router.add_post("/daemon/stop", _handle_daemon_stop)
    app.router.add_post("/api/daemon/stop", _handle_daemon_stop)
    app.router.add_post("/api/daemon/restart", _handle_daemon_restart)

    return app
