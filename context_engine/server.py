"""ContextGarden Python server — HTTP-only knowledge graph service.

Single process, single port (CG_WEBAPP_PORT, default 7433):
  /          — webapp SPA
  /api/*     — REST control plane
  /webhooks/ — GitLab push webhooks
  /mcp       — MCP StreamableHTTP (Claude Code / agents)

Process lifecycle is managed by Docker / the host supervisor (SIGTERM to stop).
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import queue as _queue
import shutil
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Optional

from .runtime_paths import resolve_mirror_cli

# ── Config ──────────────────────────────────────────────────────────────────

MAX_PENDING_JOBS = 50
DEBOUNCE_SECS = 0.5
METRICS_INTERVAL_SECS = 300
RUNTIME_READY_TIMEOUT_SECS = 90.0

DATA_DIR = Path.cwd()
STATE_DIR = DATA_DIR / ".context-garden"
WORKSPACES_JSON_PATH = STATE_DIR / "workspaces.json"
MD_DB_PATH = DATA_DIR / "md_db"

logging.basicConfig(
    stream=sys.stderr,
    level=logging.INFO,
    format="[contextgarden] %(levelname)s %(name)s: %(message)s",
)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger(__name__)


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def configure_paths(data_dir: str | Path) -> None:
    global DATA_DIR, STATE_DIR, WORKSPACES_JSON_PATH, MD_DB_PATH
    DATA_DIR = Path(data_dir).resolve()
    STATE_DIR = DATA_DIR / ".context-garden"
    WORKSPACES_JSON_PATH = STATE_DIR / "workspaces.json"
    MD_DB_PATH = DATA_DIR / "md_db"


# ── Workspace registry ───────────────────────────────────────────────────────


@dataclass
class WorkspaceRecord:
    workspace_id: str
    name: str
    root_path: str
    include_globs: list[str] = field(default_factory=lambda: ["**/*.md"])
    exclude_globs: list[str] = field(default_factory=list)
    registered_at: str = field(
        default_factory=lambda: time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    )

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @staticmethod
    def from_dict(d: dict[str, Any]) -> WorkspaceRecord:
        return WorkspaceRecord(**d)


def _read_workspaces_json() -> list[WorkspaceRecord]:
    if not WORKSPACES_JSON_PATH.exists():
        return []
    try:
        entries = json.loads(WORKSPACES_JSON_PATH.read_text("utf-8"))
        records: list[WorkspaceRecord] = []
        for entry in entries:
            if not entry.get("active", True):
                continue
            name = entry["name"]
            root_path = str(MD_DB_PATH / "code" / name)
            workspace_id = entry.get("id") or str(uuid.uuid5(uuid.NAMESPACE_DNS, name))
            records.append(WorkspaceRecord(
                workspace_id=workspace_id,
                name=name,
                root_path=root_path,
                registered_at=entry.get("registeredAt", _now_iso()),
            ))
        return records
    except Exception as exc:
        log.error("Failed to read workspaces.json: %s", exc)
        return []


# ── Per-workspace runtime ────────────────────────────────────────────────────


class WorkspaceRuntime:
    def __init__(self, record: WorkspaceRecord, engine: Any) -> None:
        self.record = record
        self.engine = engine

        self.state: str = "idle"
        self.last_indexed_at: Optional[str] = None

        self._queue: _queue.Queue[dict[str, Any] | None] = _queue.Queue(maxsize=MAX_PENDING_JOBS)
        self._thread: Optional[threading.Thread] = None
        self._thread_lock = threading.Lock()

        self._overflow_changed: list[str] = []
        self._overflow_deleted: list[str] = []
        self._overflow_count = 0
        self._overflow_lock = threading.Lock()

        self._engine_lock = threading.RLock()

        self._pause_event = threading.Event()
        self._pause_event.set()

    @property
    def doc_count(self) -> int:
        return len(self.engine._parsed_notes)


# ── Watchdog file watcher ────────────────────────────────────────────────────


class _WorkspaceWatcher:
    def __init__(self, runtime: WorkspaceRuntime) -> None:
        self._runtime = runtime
        self._pending_changed: set[str] = set()
        self._pending_deleted: set[str] = set()
        self._lock = threading.Lock()
        self._timer: Optional[threading.Timer] = None

    def on_created(self, event: Any) -> None:
        self._handle(event.src_path, deleted=False)

    def on_modified(self, event: Any) -> None:
        self._handle(event.src_path, deleted=False)

    def on_deleted(self, event: Any) -> None:
        self._handle(event.src_path, deleted=True)

    def on_moved(self, event: Any) -> None:
        self._handle(event.src_path, deleted=True)
        self._handle(event.dest_path, deleted=False)

    def _handle(self, abs_path: str, deleted: bool) -> None:
        if not abs_path.endswith(".md"):
            return
        root = self._runtime.record.root_path
        try:
            rel = os.path.relpath(abs_path, root).replace("\\", "/")
        except ValueError:
            return

        with self._lock:
            if deleted:
                self._pending_deleted.add(rel)
                self._pending_changed.discard(rel)
            else:
                self._pending_changed.add(rel)
                self._pending_deleted.discard(rel)

            if self._timer:
                self._timer.cancel()
            self._timer = threading.Timer(DEBOUNCE_SECS, self._flush)
            self._timer.daemon = True
            self._timer.start()

    def _flush(self) -> None:
        with self._lock:
            changed = list(self._pending_changed)
            deleted = list(self._pending_deleted)
            self._pending_changed.clear()
            self._pending_deleted.clear()
            self._timer = None

        if not changed and not deleted:
            return

        log.info(
            "Workspace '%s': watcher flush — %d changed, %d deleted",
            self._runtime.record.name, len(changed), len(deleted),
        )
        _enqueue_index_job(self._runtime, changed, deleted)


def _make_watchdog_handler(runtime: WorkspaceRuntime) -> Any:
    try:
        from watchdog.events import FileSystemEventHandler
    except ImportError:
        raise RuntimeError("watchdog is not installed. Run: pip install watchdog>=4.0.0")

    watcher = _WorkspaceWatcher(runtime)

    class _Handler(FileSystemEventHandler):  # type: ignore[misc]
        def on_created(self, event: Any) -> None:
            if not event.is_directory:
                watcher.on_created(event)

        def on_modified(self, event: Any) -> None:
            if not event.is_directory:
                watcher.on_modified(event)

        def on_deleted(self, event: Any) -> None:
            if not event.is_directory:
                watcher.on_deleted(event)

        def on_moved(self, event: Any) -> None:
            if not event.is_directory:
                watcher.on_moved(event)

    return _Handler()


# ── Background indexer ───────────────────────────────────────────────────────


def _enqueue_index_job(
    runtime: WorkspaceRuntime,
    changed_paths: list[str],
    deleted_paths: list[str],
) -> dict[str, Any]:
    _ensure_index_thread(runtime)

    job: dict[str, Any] = {
        "changed_paths": changed_paths,
        "deleted_paths": deleted_paths,
        "job_id": str(uuid.uuid4()),
    }

    try:
        runtime._queue.put_nowait(job)
        return {"job_id": job["job_id"], "queue_depth": runtime._queue.qsize(), "coalesced": False}
    except _queue.Full:
        with runtime._overflow_lock:
            runtime._overflow_changed.extend(changed_paths)
            runtime._overflow_deleted.extend(deleted_paths)
            runtime._overflow_count += 1
            n = runtime._overflow_count
        log.warning(
            "Workspace '%s' index queue full — coalescing to overflow (events=%d)",
            runtime.record.name, n,
        )
        return {"job_id": job["job_id"], "queue_depth": runtime._queue.qsize(), "coalesced": True}


def _flush_overflow(runtime: WorkspaceRuntime) -> None:
    with runtime._overflow_lock:
        if not runtime._overflow_changed and not runtime._overflow_deleted:
            return
        changed = list(runtime._overflow_changed)
        deleted = list(runtime._overflow_deleted)
        n = runtime._overflow_count
        runtime._overflow_changed.clear()
        runtime._overflow_deleted.clear()
        runtime._overflow_count = 0

    log.info(
        "Workspace '%s': overflow flush — %d changed + %d deleted from %d events",
        runtime.record.name, len(changed), len(deleted), n,
    )
    _run_incremental(runtime, changed, deleted)


def _run_incremental(
    runtime: WorkspaceRuntime,
    changed_paths: list[str],
    deleted_paths: list[str],
) -> None:
    runtime._pause_event.wait()

    with runtime._engine_lock:
        runtime.state = "indexing"
        try:
            runtime.engine.incremental_update(
                changed_paths=changed_paths,
                deleted_paths=deleted_paths,
                progress_cb=None,
            )
            runtime.last_indexed_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        except Exception as exc:
            log.error("Workspace '%s': incremental_update failed: %s", runtime.record.name, exc)
        finally:
            runtime.state = "idle"


def _index_worker(runtime: WorkspaceRuntime) -> None:
    while True:
        job = runtime._queue.get()
        if job is None:
            runtime._queue.task_done()
            _flush_overflow(runtime)
            break

        try:
            _run_incremental(runtime, job["changed_paths"], job["deleted_paths"])
        finally:
            runtime._queue.task_done()

        if runtime._queue.empty():
            _flush_overflow(runtime)


def _ensure_index_thread(runtime: WorkspaceRuntime) -> None:
    with runtime._thread_lock:
        if runtime._thread is None or not runtime._thread.is_alive():
            t = threading.Thread(
                target=_index_worker,
                args=(runtime,),
                name=f"indexer-{runtime.record.name}",
                daemon=True,
            )
            t.start()
            runtime._thread = t


def _stop_index_thread(runtime: WorkspaceRuntime) -> None:
    try:
        runtime._queue.put_nowait(None)
    except _queue.Full:
        with runtime._overflow_lock:
            runtime._overflow_changed.clear()
            runtime._overflow_deleted.clear()
        try:
            runtime._queue.put_nowait(None)
        except _queue.Full:
            log.warning(
                "Workspace '%s': failed to enqueue indexer shutdown sentinel (queue still full)",
                runtime.record.name,
            )


# ── Server state ─────────────────────────────────────────────────────────────


class _DaemonServer:
    def __init__(self) -> None:
        self._workspaces: dict[str, WorkspaceRecord] = {}
        self._runtimes: dict[str, WorkspaceRuntime] = {}
        self._observers: dict[str, Any] = {}
        self._lock = threading.RLock()
        self._runtime_ready = threading.Condition(self._lock)
        self._boot_errors: dict[str, str] = {}
        self._start_time = time.monotonic()

    def load_registry(self) -> None:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        legacy = STATE_DIR / "registry.json"
        if legacy.exists():
            legacy.unlink(missing_ok=True)
            log.info("Removed legacy registry.json (workspaces now read from workspaces.json)")
        records = _read_workspaces_json()
        for rec in records:
            self._workspaces[rec.workspace_id] = rec
        log.info("Loaded %d workspace(s) from workspaces.json", len(self._workspaces))

    def _index_dir(self, workspace_id: str) -> str:
        return str(STATE_DIR / "knowledge_graph" / workspace_id / "index")

    def _init_engine(self, record: WorkspaceRecord) -> WorkspaceRuntime:
        from .core.engine import KnowledgeGraphEngine

        engine = KnowledgeGraphEngine()
        index_dir = self._index_dir(record.workspace_id)
        os.makedirs(index_dir, exist_ok=True)

        engine.initialize(
            md_db_path=record.root_path,
            db_dir=index_dir,
            top_k=8,
        )

        return WorkspaceRuntime(record, engine)

    def _start_observer(self, runtime: WorkspaceRuntime) -> None:
        try:
            from watchdog.observers import Observer
        except ImportError as exc:
            log.warning(
                "watchdog.observers could not be imported (%s) — file watching disabled.",
                exc,
            )
            return

        handler = _make_watchdog_handler(runtime)
        observer = Observer()
        observer.schedule(handler, runtime.record.root_path, recursive=True)
        observer.start()
        self._observers[runtime.record.workspace_id] = observer
        log.info("Started file watcher for workspace '%s'", runtime.record.name)

    def _stop_observer(self, workspace_id: str) -> None:
        observer = self._observers.pop(workspace_id, None)
        if observer:
            observer.stop()
            observer.join(timeout=3)

    def boot_workspaces(self) -> None:
        failures: list[str] = []
        with self._lock:
            records = list(self._workspaces.items())

        for workspace_id, record in records:
            if not os.path.isdir(record.root_path):
                log.warning(
                    "Workspace '%s' root_path does not exist: %s - skipping",
                    record.name, record.root_path,
                )
                with self._runtime_ready:
                    self._boot_errors[workspace_id] = (
                        f"Workspace root_path does not exist: {record.root_path}"
                    )
                    self._runtime_ready.notify_all()
                continue

            with self._lock:
                if workspace_id in self._runtimes:
                    continue
                self._boot_errors.pop(workspace_id, None)

            try:
                log.info("Booting workspace '%s' (%s)...", record.name, record.root_path)
                runtime = self._init_engine(record)

                with self._lock:
                    if workspace_id not in self._workspaces:
                        log.info("Workspace '%s' removed while booting - discarding", record.name)
                        continue
                    if workspace_id in self._runtimes:
                        continue
                    self._runtimes[workspace_id] = runtime
                    self._boot_errors.pop(workspace_id, None)
                    self._runtime_ready.notify_all()

                _ensure_index_thread(runtime)
                self._start_observer(runtime)
                log.info("Workspace '%s' ready - %d docs", record.name, runtime.doc_count)
            except Exception as exc:
                log.error("Failed to boot workspace '%s': %s", record.name, exc)
                failures.append(f"{record.name}: {exc}")
                with self._runtime_ready:
                    self._boot_errors[workspace_id] = str(exc)
                    self._runtime_ready.notify_all()

        if failures:
            raise RuntimeError("Failed to boot one or more workspaces: " + "; ".join(failures))

    # ── RPC handlers ────────────────────────────────────────────────────

    def handle_workspaces_sync(self, params: dict[str, Any]) -> dict[str, Any]:
        records = _read_workspaces_json()
        new_by_id = {r.workspace_id: r for r in records}

        with self._lock:
            current_ids = set(self._workspaces.keys())

        removed_ids = current_ids - new_by_id.keys()
        for wid in removed_ids:
            self._stop_observer(wid)
            with self._runtime_ready:
                rt = self._runtimes.pop(wid, None)
                self._workspaces.pop(wid, None)
                self._boot_errors.pop(wid, None)
                self._runtime_ready.notify_all()
            if rt:
                _stop_index_thread(rt)

        added: list[WorkspaceRecord] = []
        for wid, rec in new_by_id.items():
            with self._lock:
                if wid not in self._workspaces:
                    self._workspaces[wid] = rec
                    added.append(rec)

        for rec in added:
            if not os.path.isdir(rec.root_path):
                log.warning("Workspace '%s' root_path does not exist: %s", rec.name, rec.root_path)
                with self._runtime_ready:
                    self._boot_errors[rec.workspace_id] = f"root_path does not exist: {rec.root_path}"
                    self._runtime_ready.notify_all()
                continue
            try:
                log.info("Booting workspace '%s' (%s)...", rec.name, rec.root_path)
                runtime = self._init_engine(rec)
                with self._runtime_ready:
                    if rec.workspace_id not in self._workspaces:
                        log.info("Workspace '%s' removed while booting — discarding", rec.name)
                        continue
                    self._runtimes[rec.workspace_id] = runtime
                    self._boot_errors.pop(rec.workspace_id, None)
                    self._runtime_ready.notify_all()
                _ensure_index_thread(runtime)
                self._start_observer(runtime)
                log.info("Workspace '%s' ready — %d docs", rec.name, runtime.doc_count)
            except Exception as exc:
                log.error("Failed to boot workspace '%s': %s", rec.name, exc)
                with self._runtime_ready:
                    self._boot_errors[rec.workspace_id] = str(exc)
                    self._runtime_ready.notify_all()

        with self._lock:
            return {
                "added": [r.name for r in added],
                "removed": list(removed_ids),
                "total": len(self._workspaces),
            }

    def handle_workspaces_register(self, params: dict[str, Any]) -> dict[str, Any]:
        return self.handle_workspaces_sync(params)

    def handle_workspaces_list(self, params: dict[str, Any]) -> dict[str, Any]:
        with self._lock:
            result = []
            for workspace_id, rec in self._workspaces.items():
                rt = self._runtimes.get(workspace_id)
                result.append({
                    "workspace_id": workspace_id,
                    "name": rec.name,
                    "root_path": rec.root_path,
                    "state": rt.state if rt else "uninitialized",
                    "last_indexed_at": rt.last_indexed_at if rt else None,
                    "doc_count": rt.doc_count if rt else 0,
                    "watcher_active": workspace_id in self._observers,
                    "registered_at": rec.registered_at,
                })
            return {"workspaces": result}

    def handle_workspaces_unregister(self, params: dict[str, Any]) -> dict[str, Any]:
        return self.handle_workspaces_sync(params)

    def handle_workspaces_status(self, params: dict[str, Any]) -> dict[str, Any]:
        workspace_id = params["workspace_id"]
        with self._lock:
            rec = self._workspaces.get(workspace_id)
            rt = self._runtimes.get(workspace_id)
        if not rec:
            raise KeyError(f"Workspace not found: {workspace_id}")
        return {
            "workspace_id": workspace_id,
            "name": rec.name,
            "state": rt.state if rt else "uninitialized",
            "queue_depth": rt._queue.qsize() if rt else 0,
            "doc_count": rt.doc_count if rt else 0,
            "last_indexed_at": rt.last_indexed_at if rt else None,
            "watcher_active": workspace_id in self._observers,
        }

    def handle_index_enqueue(self, params: dict[str, Any]) -> dict[str, Any]:
        workspace_id = params.get("workspace_id")
        name = params.get("name")
        with self._lock:
            if workspace_id:
                rt = self._runtimes.get(workspace_id)
            elif name:
                rt = next((r for r in self._runtimes.values() if r.record.name == name), None)
            else:
                rt = None
        if not rt:
            raise KeyError(f"Workspace not initialized: {workspace_id or name}")
        return _enqueue_index_job(
            rt,
            params.get("changed_paths", []),
            params.get("deleted_paths", []),
        )

    def handle_index_rebuild(self, params: dict[str, Any]) -> dict[str, Any]:
        workspace_id = params.get("workspace_id")
        name = params.get("name")
        force = bool(params.get("force", False))
        with self._lock:
            if workspace_id:
                targets = [self._runtimes[workspace_id]] if workspace_id in self._runtimes else []
            elif name:
                targets = [rt for rt in self._runtimes.values() if rt.record.name == name]
            else:
                targets = list(self._runtimes.values())
        if not targets:
            raise KeyError(
                f"Workspace not initialized: {workspace_id or name}" if (workspace_id or name)
                else "No initialized workspaces to rebuild"
            )

        job_id = str(uuid.uuid4())
        total_docs = 0
        for rt in targets:
            rt.state = "indexing"
            try:
                with rt._engine_lock:
                    rt.engine.reindex(force=force)
                    rt.last_indexed_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
                total_docs += rt.doc_count
            except Exception as exc:
                log.error("Workspace '%s': reindex failed: %s", rt.record.name, exc)
                raise
            finally:
                rt.state = "idle"
        return {"job_id": job_id, "doc_count": total_docs}

    def handle_index_pause(self, params: dict[str, Any]) -> dict[str, Any]:
        workspace_id = params.get("workspace_id")
        with self._lock:
            targets = (
                [self._runtimes[workspace_id]]
                if workspace_id and workspace_id in self._runtimes
                else list(self._runtimes.values())
            )
        for rt in targets:
            rt._pause_event.clear()
            rt.state = "paused"
        return {"ok": True, "paused": [rt.record.workspace_id for rt in targets]}

    def handle_index_resume(self, params: dict[str, Any]) -> dict[str, Any]:
        workspace_id = params.get("workspace_id")
        with self._lock:
            targets = (
                [self._runtimes[workspace_id]]
                if workspace_id and workspace_id in self._runtimes
                else list(self._runtimes.values())
            )
        for rt in targets:
            rt._pause_event.set()
            rt.state = "idle"
        return {"ok": True, "resumed": [rt.record.workspace_id for rt in targets]}

    def _get_runtime(
        self,
        params: dict[str, Any],
        *,
        wait_secs: float = 0.0,
    ) -> Optional[WorkspaceRuntime]:
        deadline = time.monotonic() + wait_secs

        with self._runtime_ready:
            while True:
                workspace_id = params.get("workspace_id")
                if workspace_id:
                    rt = self._runtimes.get(workspace_id)
                    if rt:
                        return rt
                    if workspace_id not in self._workspaces:
                        return None
                    if workspace_id in self._boot_errors:
                        raise RuntimeError(
                            f"Workspace failed to initialize: {self._boot_errors[workspace_id]}"
                        )
                else:
                    if len(self._runtimes) == 1:
                        return next(iter(self._runtimes.values()))
                    if len(self._runtimes) > 1:
                        return None
                    if len(self._workspaces) == 1:
                        only_id = next(iter(self._workspaces.keys()))
                        if only_id in self._boot_errors:
                            raise RuntimeError(
                                f"Workspace failed to initialize: {self._boot_errors[only_id]}"
                            )
                    elif len(self._workspaces) == 0:
                        return None

                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self._runtime_ready.wait(timeout=min(0.5, remaining))

    def handle_query_retrieve(self, params: dict[str, Any]) -> dict[str, Any]:
        workspace_filter = params.get("workspace")
        top_k = params.get("top_k")
        query = params["query"]

        # Resolve workspace name → workspace_id if needed
        resolved_params = dict(params)
        if not resolved_params.get("workspace_id") and workspace_filter:
            with self._lock:
                for rt in self._runtimes.values():
                    if rt.record.name == workspace_filter:
                        resolved_params["workspace_id"] = rt.record.workspace_id
                        break

        rt = self._get_runtime(resolved_params, wait_secs=RUNTIME_READY_TIMEOUT_SECS)
        if not rt:
            with self._lock:
                names = [r.record.name for r in self._runtimes.values()]
            if len(names) > 1:
                raise RuntimeError(
                    f"Multiple workspaces registered ({', '.join(names)}). "
                    f"Specify a workspace name in your query."
                )
            raise RuntimeError("Workspace is still initializing; try again shortly.")

        with rt._engine_lock:
            result = rt.engine.retrieve(query=query, top_k=top_k, workspace=workspace_filter)
        try:
            from . import http_server as _hs
            _hs.record_retrieval(query, result)
        except Exception:
            pass
        return result

    def handle_query_find_path(self, params: dict[str, Any]) -> dict[str, Any]:
        rt = self._get_runtime(params, wait_secs=RUNTIME_READY_TIMEOUT_SECS)
        if not rt:
            with self._lock:
                runtimes = list(self._runtimes.values())
            if not runtimes:
                raise RuntimeError("No workspaces are initialized yet; try again shortly.")
            last_result: Optional[dict[str, Any]] = None
            for candidate in runtimes:
                with candidate._engine_lock:
                    result = candidate.engine.find_path(
                        start_query=params["start"],
                        end_query=params["end"],
                        edge_types=params.get("edge_types"),
                        max_depth=params.get("max_depth", 8),
                    )
                last_result = result
                if not result.get("no_path", True):
                    return result
            return last_result  # type: ignore[return-value]
        with rt._engine_lock:
            return rt.engine.find_path(
                start_query=params["start"],
                end_query=params["end"],
                edge_types=params.get("edge_types"),
                max_depth=params.get("max_depth", 8),
            )

    def handle_query_stats(self, params: dict[str, Any]) -> dict[str, Any]:
        rt = self._get_runtime(params)
        if not rt:
            return {"doc_count": 0, "node_count": 0, "edge_count": 0}
        with rt._engine_lock:
            stats = rt.engine.get_graph_stats()
        return {
            "doc_count": stats.get("note_count", 0),
            "node_count": stats.get("note_count", 0),
            "edge_count": stats.get("edge_count", 0),
            "index_loaded": stats.get("index_loaded", False),
        }

    def handle_query_get_note_content(self, params: dict[str, Any]) -> dict[str, Any]:
        workspace_id = params.get("workspace_id")
        relative_path = params["relative_path"]
        with self._lock:
            rt = self._runtimes.get(workspace_id) if workspace_id else (
                next(iter(self._runtimes.values())) if len(self._runtimes) == 1 else None
            )
        body = None
        if rt:
            with rt._engine_lock:
                body = rt.engine.get_note_content(relative_path)
        return {"body": body}

    def handle_daemon_health(self, params: dict[str, Any]) -> dict[str, Any]:
        uptime = time.monotonic() - self._start_time
        rss_mb: Optional[float] = None
        try:
            import psutil
            rss_mb = psutil.Process().memory_info().rss / (1024.0 * 1024.0)
        except Exception:
            pass

        with self._lock:
            ws_summary = []
            for wid, rec in self._workspaces.items():
                rt = self._runtimes.get(wid)
                ws_summary.append({
                    "workspace_id": wid,
                    "name": rec.name,
                    "root_path": rec.root_path,
                    "state": rt.state if rt else "initializing",
                    "doc_count": rt.doc_count if rt else 0,
                    "watcher_active": wid in self._observers,
                    "last_indexed_at": rt.last_indexed_at if rt else None,
                })

        return {
            "status": "ok",
            "uptime_secs": round(uptime, 1),
            "rss_mb": round(rss_mb, 1) if rss_mb is not None else None,
            "data_dir": str(DATA_DIR),
            "workspaces": ws_summary,
        }

    def handle_daemon_shutdown(self, params: dict[str, Any]) -> dict[str, Any]:
        log.info("Shutdown requested")
        with self._lock:
            runtimes = list(self._runtimes.values())
        for rt in runtimes:
            _stop_index_thread(rt)
        with self._lock:
            ids = list(self._observers.keys())
        for wid in ids:
            self._stop_observer(wid)
        return {"ok": True}

    HANDLERS: dict[str, str] = {
        "workspaces.sync":          "handle_workspaces_sync",
        "workspaces.register":      "handle_workspaces_register",
        "workspaces.list":          "handle_workspaces_list",
        "workspaces.unregister":    "handle_workspaces_unregister",
        "workspaces.status":        "handle_workspaces_status",
        "index.enqueue":            "handle_index_enqueue",
        "index.rebuild":            "handle_index_rebuild",
        "index.pause":              "handle_index_pause",
        "index.resume":             "handle_index_resume",
        "query.retrieve":           "handle_query_retrieve",
        "query.find_path":          "handle_query_find_path",
        "query.stats":              "handle_query_stats",
        "query.get_note_content":   "handle_query_get_note_content",
        "daemon.health":            "handle_daemon_health",
        "daemon.shutdown":          "handle_daemon_shutdown",
    }

    _BLOCKING_METHODS = {
        "workspaces.sync",
        "workspaces.register",
        "index.rebuild",
        "query.retrieve",
        "query.find_path",
        "query.stats",
        "query.get_note_content",
    }

    def dispatch(self, method: str, params: dict[str, Any]) -> Any:
        handler_name = self.HANDLERS.get(method)
        if handler_name is None:
            raise ValueError(f"Unknown method: {method}")
        return getattr(self, handler_name)(params)


# ── asyncio entry point ──────────────────────────────────────────────────────

_server: _DaemonServer = _DaemonServer()


def _log_metrics() -> None:
    try:
        import psutil
        rss_mb = psutil.Process().memory_info().rss / (1024.0 * 1024.0)
    except Exception:
        rss_mb = None

    with _server._lock:
        workspaces = list(_server._runtimes.values())

    for rt in workspaces:
        q_depth = rt._queue.qsize()
        with rt._overflow_lock:
            overflow = len(rt._overflow_changed) + len(rt._overflow_deleted)
        if rss_mb is not None:
            log.info(
                "[metrics] workspace=%s queue=%d overflow=%d docs=%d rss_mb=%.1f",
                rt.record.name, q_depth, overflow, rt.doc_count, rss_mb,
            )
        else:
            log.info(
                "[metrics] workspace=%s queue=%d overflow=%d docs=%d",
                rt.record.name, q_depth, overflow, rt.doc_count,
            )


async def _periodic_metrics() -> None:
    while True:
        await asyncio.sleep(METRICS_INTERVAL_SECS)
        try:
            _log_metrics()
        except Exception as exc:
            log.debug("Metrics snapshot failed: %s", exc)


async def _boot_workspaces_in_background() -> None:
    try:
        await asyncio.to_thread(_server.boot_workspaces)
    except Exception as exc:
        log.error("Background workspace boot failed: %s", exc)


async def _run_server() -> None:
    import uvicorn
    from .http_server import make_http_app, HTTP_PORT

    # Use a holder so the shutdown fn can close over the server handle
    # even though make_http_app is called before the server is created.
    _uv_ref: list[uvicorn.Server] = []

    def _request_shutdown() -> None:
        if _uv_ref:
            _uv_ref[0].should_exit = True

    starlette_app = make_http_app(_server, DATA_DIR, shutdown_fn=_request_shutdown)
    uv_config = uvicorn.Config(
        starlette_app,
        host="0.0.0.0",
        port=HTTP_PORT,
        loop="none",
        log_level="warning",
    )
    uv_server = uvicorn.Server(uv_config)
    _uv_ref.append(uv_server)

    # SIGHUP reloads config without restart (Linux/container only)
    try:
        import signal as _signal
        from .config import reload_config as _reload_config

        def _on_sighup() -> None:
            _reload_config()
            log.info("Config reloaded via SIGHUP")

        asyncio.get_event_loop().add_signal_handler(_signal.SIGHUP, _on_sighup)
    except (AttributeError, NotImplementedError, OSError):
        pass  # SIGHUP unavailable on Windows

    asyncio.ensure_future(_periodic_metrics())
    asyncio.create_task(_boot_workspaces_in_background())

    log.info("ContextGarden server on http://0.0.0.0:%d (MCP at /mcp)", HTTP_PORT)
    await uv_server.serve()
    log.info("Server shutting down")


def main(data_dir: Optional[str] = None) -> None:
    chosen_data_dir = Path(data_dir).resolve() if data_dir else Path.cwd().resolve()
    configure_paths(chosen_data_dir)

    from .config import set_data_dir
    set_data_dir(DATA_DIR)

    STATE_DIR.mkdir(parents=True, exist_ok=True)

    log.info("ContextGarden server starting (data_dir=%s)", DATA_DIR)

    _server.load_registry()

    _node_bin = shutil.which("node") or "node"
    _mirror_cli = resolve_mirror_cli(data_dir=DATA_DIR)
    from .core import jobs as _jobs
    _jobs.init(DATA_DIR, _node_bin, _mirror_cli, lambda: _server)

    try:
        asyncio.run(_run_server())
    except KeyboardInterrupt:
        log.info("Server interrupted by user")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Run ContextGarden server")
    parser.add_argument(
        "--data-dir",
        default=str(Path.cwd()),
        help="Canonical data directory root (default: current working directory)",
    )
    cli_args = parser.parse_args()
    main(data_dir=cli_args.data_dir)
