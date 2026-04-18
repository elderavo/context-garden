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
from pathlib import Path
from typing import TYPE_CHECKING, Any

from aiohttp import web

if TYPE_CHECKING:
    from .daemon import _DaemonServer

log = logging.getLogger(__name__)

HTTP_PORT = int(os.environ.get("CG_WEBAPP_PORT", "7433"))

# Populated by make_http_app()
_daemon_ref: "_DaemonServer | None" = None
_data_dir_ref: Path = Path.cwd()
_static_dir: Path = Path(__file__).parent / "static"

# ── Retrieval activity ring buffer ────────────────────────────────────────────
_ACTIVITY_MAX = 50
_activity_ring: collections.deque[dict[str, Any]] = collections.deque(maxlen=_ACTIVITY_MAX)


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
    path = _data_dir_ref / ".context-garden" / "workspaces.json"
    if not path.exists():
        return []
    try:
        return json.loads(path.read_text("utf-8"))
    except Exception:
        return []


def _save_workspaces(entries: list[dict[str, Any]]) -> None:
    path = _data_dir_ref / ".context-garden" / "workspaces.json"
    path.write_text(json.dumps(entries, indent=2), "utf-8")


# ── Route handlers ────────────────────────────────────────────────────────────


async def _handle_index(_req: web.Request) -> web.Response:
    index_path = _static_dir / "index.html"
    if not index_path.exists():
        return web.Response(status=404, text="Dashboard not found")
    return web.Response(body=index_path.read_bytes(), content_type="text/html")


async def _handle_gitlab_webhook(req: web.Request) -> web.Response:
    from . import jobs

    token = req.headers.get("X-Gitlab-Token")
    if not token:
        return web.json_response({"error": "Missing X-Gitlab-Token header"}, status=401)

    workspaces = _load_workspaces()
    entry = next(
        (w for w in workspaces
         if w.get("sourceType") == "gitlab"
         and w.get("gitlabConfig", {}).get("webhookSecret") == token),
        None,
    )
    if not entry:
        return web.json_response({"error": "Unknown webhook token"}, status=401)

    try:
        payload = await req.json()
    except Exception:
        payload = {}

    expected_ref = f"refs/heads/{entry['gitlabConfig']['branch']}"
    push_ref = payload.get("ref")
    if push_ref and push_ref != expected_ref:
        return web.json_response({
            "status": "ignored",
            "reason": f"push was to {push_ref}, tracking {expected_ref}",
        })

    job = jobs.enqueue_sync(entry["id"], entry["name"], "webhook")
    return web.json_response(
        {"status": "accepted", "jobId": job.id, "workspaceName": entry["name"]},
        status=202,
    )


async def _handle_list_workspaces(_req: web.Request) -> web.Response:
    from . import jobs

    workspaces = _load_workspaces()
    all_jobs = jobs.list_jobs()

    result = []
    for w in workspaces:
        last_job = next((j for j in all_jobs if j.workspace_id == w["id"]), None)
        result.append({
            "id": w["id"],
            "name": w["name"],
            "sourceType": w.get("sourceType", "local"),
            "active": w.get("active", True),
            "registeredAt": w.get("registeredAt", ""),
            "source": w.get("gitlabConfig", {}).get("projectUrl") if w.get("sourceType") == "gitlab" else w.get("sourceDir"),
            "branch": w.get("gitlabConfig", {}).get("branch"),
            "lastSync": {"status": last_job.status, "completedAt": last_job.completed_at} if last_job else None,
        })
    return web.json_response(result)


async def _handle_workspace_sync(req: web.Request) -> web.Response:
    from . import jobs

    workspace_id = req.match_info["id"]
    workspaces = _load_workspaces()
    entry = next((w for w in workspaces if w["id"] == workspace_id), None)
    if not entry:
        return web.json_response({"error": "Workspace not found"}, status=404)
    if entry.get("sourceType") != "gitlab":
        return web.json_response({"error": "Only GitLab workspaces support sync"}, status=400)

    job = jobs.enqueue_sync(entry["id"], entry["name"], "manual")
    return web.json_response({"jobId": job.id, "status": job.status}, status=202)


async def _handle_workspace_index(req: web.Request) -> web.Response:
    from . import jobs

    workspace_id = req.match_info["id"]
    workspaces = _load_workspaces()
    entry = next((w for w in workspaces if w["id"] == workspace_id), None)
    if not entry:
        return web.json_response({"error": "Workspace not found"}, status=404)

    job = jobs.enqueue_index(entry["id"], entry["name"], "manual")
    return web.json_response({"jobId": job.id, "status": job.status}, status=202)


async def _handle_workspace_unregister(req: web.Request) -> web.Response:
    assert _daemon_ref is not None

    workspace_id = req.match_info["id"]
    workspaces = _load_workspaces()
    idx = next((i for i, w in enumerate(workspaces) if w["id"] == workspace_id), None)
    if idx is None:
        return web.json_response({"error": "Workspace not found"}, status=404)

    entry = workspaces[idx]
    updated = [w for w in workspaces if w["id"] != workspace_id]
    _save_workspaces(updated)

    import shutil
    mirror_dir = _data_dir_ref / ".context-garden" / "md_db" / "code" / entry["name"]
    shutil.rmtree(str(mirror_dir), ignore_errors=True)

    if entry.get("sourceType") == "gitlab":
        clone_dir = entry.get("gitlabConfig", {}).get("cloneDir")
        if clone_dir:
            shutil.rmtree(clone_dir, ignore_errors=True)

    import asyncio
    result = await asyncio.to_thread(
        _daemon_ref.handle_workspaces_sync, {}
    )
    return web.json_response(result)


async def _handle_list_jobs(_req: web.Request) -> web.Response:
    from . import jobs

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
        for j in jobs.list_jobs()
    ])


async def _handle_get_job(req: web.Request) -> web.Response:
    from . import jobs

    job = jobs.get_job(req.match_info["id"])
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


async def _handle_daemon_start(_req: web.Request) -> web.Response:
    assert _daemon_ref is not None
    import asyncio
    try:
        health = await asyncio.to_thread(_daemon_ref.handle_daemon_health, {})
        return web.json_response(health)
    except Exception as exc:
        return web.json_response({"error": str(exc)}, status=500)


# ── App factory ───────────────────────────────────────────────────────────────


def make_http_app(daemon: "_DaemonServer", data_dir: Path) -> web.Application:
    global _daemon_ref, _data_dir_ref
    _daemon_ref = daemon
    _data_dir_ref = data_dir

    app = web.Application()

    app.router.add_get("/", _handle_index)
    app.router.add_post("/webhooks/gitlab", _handle_gitlab_webhook)

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

    app.router.add_get("/status", _handle_status)
    app.router.add_get("/api/status", _handle_status)

    app.router.add_post("/daemon/stop", _handle_daemon_stop)
    app.router.add_post("/daemon/start", _handle_daemon_start)
    app.router.add_post("/api/daemon/stop", _handle_daemon_stop)
    app.router.add_post("/api/daemon/start", _handle_daemon_start)

    return app
