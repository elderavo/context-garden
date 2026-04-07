"""ContextGarden Python Daemon — persistent TCP server for knowledge graph operations.

Replaces the stdio subprocess model. Runs persistently (started at OS login, or
spawned by the TS gateway as a fallback). Exposes JSON-RPC over localhost TCP.

Architecture
────────────
  asyncio TCP server on 127.0.0.1:7432 (CG_DAEMON_PORT)
  Per-workspace KnowledgeGraphEngine instances
  Per-workspace watchdog file watchers (debounced 500 ms, .md files only)
  Workspace registry persisted to ~/.contextgarden/registry.json (CG_DATA_DIR)
  Per-workspace index persisted to ~/.contextgarden/<workspace_id>/index/
  file_hashes.json drift detection on boot — only re-embeds changed files

RPC API (v1, newline-delimited JSON)
────────────────────────────────────
  workspaces.register   { name, root_path, include_globs?, exclude_globs? }
  workspaces.list       {}
  workspaces.unregister { workspace_id, delete_data? }
  workspaces.status     { workspace_id }

  index.enqueue  { workspace_id, changed_paths, deleted_paths }
  index.rebuild  { workspace_id }
  index.pause    { workspace_id? }
  index.resume   { workspace_id? }

  query.retrieve      { workspace_id?, query, top_k?, max_chars? }
  query.find_path     { workspace_id?, start, end, max_depth?, edge_types? }
  query.stats         { workspace_id? }
  query.get_note_content { workspace_id, relative_path }

  daemon.health   {}
  daemon.shutdown {}
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

# ── Config ──────────────────────────────────────────────────────────────────

DAEMON_PORT = int(os.environ.get("CG_DAEMON_PORT", "7432"))
DATA_DIR = Path(os.environ.get("CG_DATA_DIR", str(Path.home() / ".contextgarden")))
MAX_PENDING_JOBS = int(os.environ.get("CG_MAX_PENDING_JOBS", "50"))
DEBOUNCE_SECS = 0.5
METRICS_INTERVAL_SECS = 300

logging.basicConfig(
    stream=sys.stderr,
    level=getattr(logging, os.environ.get("CG_LOG_LEVEL", "INFO"), logging.INFO),
    format="[contextgarden] %(levelname)s %(name)s: %(message)s",
)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger(__name__)


# ── Workspace registry ───────────────────────────────────────────────────────


@dataclass
class WorkspaceRecord:
    workspace_id: str
    name: str
    root_path: str  # absolute path to the notes directory (md_db)
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


# ── Per-workspace runtime ────────────────────────────────────────────────────


class WorkspaceRuntime:
    """Holds live state for one registered workspace."""

    def __init__(self, record: WorkspaceRecord, engine: Any) -> None:
        self.record = record
        self.engine = engine  # KnowledgeGraphEngine

        self.state: str = "idle"  # idle | indexing | paused | error
        self.last_indexed_at: Optional[str] = None

        # Background indexer (same bounded-queue + overflow pattern as server.py)
        self._queue: _queue.Queue[dict[str, Any] | None] = _queue.Queue(maxsize=MAX_PENDING_JOBS)
        self._thread: Optional[threading.Thread] = None
        self._thread_lock = threading.Lock()

        # Overflow coalescing
        self._overflow_changed: list[str] = []
        self._overflow_deleted: list[str] = []
        self._overflow_count = 0
        self._overflow_lock = threading.Lock()

        # Engine-level lock: guards _parsed_notes / index during concurrent reads + writes
        self._engine_lock = threading.RLock()

        # Pause gate: workers block here when paused
        self._pause_event = threading.Event()
        self._pause_event.set()  # not paused initially

    @property
    def doc_count(self) -> int:
        return len(self.engine._parsed_notes)


# ── Watchdog file watcher ────────────────────────────────────────────────────


class _WorkspaceWatcher:
    """Debounced watchdog handler for a single workspace."""

    def __init__(self, runtime: WorkspaceRuntime) -> None:
        self._runtime = runtime
        self._pending_changed: set[str] = set()
        self._pending_deleted: set[str] = set()
        self._lock = threading.Lock()
        self._timer: Optional[threading.Timer] = None

    # watchdog calls these methods
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
            return  # different drive on Windows

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
    """Return a watchdog FileSystemEventHandler wrapping _WorkspaceWatcher."""
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
    """Queue an incremental index job; coalesce into overflow if queue is full."""
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
    """Run any coalesced overflow as a single reconcile job. Called from worker thread."""
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
    """Run incremental_update under the engine lock."""
    runtime._pause_event.wait()  # block while paused

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
    """Background thread: drains the job queue and runs incremental_update."""
    while True:
        job = runtime._queue.get()
        if job is None:  # shutdown sentinel
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
            pass


# ── Daemon server state ──────────────────────────────────────────────────────


class _DaemonServer:
    def __init__(self) -> None:
        self._workspaces: dict[str, WorkspaceRecord] = {}   # id → record
        self._runtimes: dict[str, WorkspaceRuntime] = {}    # id → runtime
        self._observers: dict[str, Any] = {}                # id → watchdog Observer
        self._lock = threading.RLock()
        self._start_time = time.monotonic()

    # ── Registry persistence ─────────────────────────────────────────────

    def load_registry(self) -> None:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        reg_path = DATA_DIR / "registry.json"
        if not reg_path.exists():
            return
        try:
            data = json.loads(reg_path.read_text("utf-8"))
            for entry in data:
                rec = WorkspaceRecord.from_dict(entry)
                self._workspaces[rec.workspace_id] = rec
            log.info("Loaded %d workspace(s) from registry", len(self._workspaces))
        except Exception as exc:
            log.error("Failed to load registry: %s", exc)

    def _save_registry(self) -> None:
        reg_path = DATA_DIR / "registry.json"
        data = [rec.to_dict() for rec in self._workspaces.values()]
        reg_path.write_text(json.dumps(data, indent=2), "utf-8")

    # ── Workspace lifecycle ──────────────────────────────────────────────

    def _index_dir(self, workspace_id: str) -> str:
        return str(DATA_DIR / workspace_id / "index")

    def _init_engine(self, record: WorkspaceRecord) -> WorkspaceRuntime:
        """Initialize (or load) a KnowledgeGraphEngine for a workspace."""
        from .engine import KnowledgeGraphEngine

        engine = KnowledgeGraphEngine()
        index_dir = self._index_dir(record.workspace_id)
        os.makedirs(index_dir, exist_ok=True)

        engine.initialize(
            md_db_path=record.root_path,
            db_dir=index_dir,
            top_k=8,
        )

        runtime = WorkspaceRuntime(record, engine)
        return runtime

    def _start_observer(self, runtime: WorkspaceRuntime) -> None:
        try:
            from watchdog.observers import Observer
        except ImportError as exc:
            log.warning(
                "watchdog.observers could not be imported (%s) — file watching disabled. "
                "Run: pip install 'watchdog>=4.0.0'",
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
        """Initialize all registered workspaces (called on daemon startup)."""
        with self._lock:
            for workspace_id, record in list(self._workspaces.items()):
                if not os.path.isdir(record.root_path):
                    log.warning(
                        "Workspace '%s' root_path does not exist: %s — skipping",
                        record.name, record.root_path,
                    )
                    continue
                try:
                    log.info("Booting workspace '%s' (%s)…", record.name, record.root_path)
                    runtime = self._init_engine(record)
                    self._runtimes[workspace_id] = runtime
                    _ensure_index_thread(runtime)
                    self._start_observer(runtime)
                    log.info(
                        "Workspace '%s' ready — %d docs", record.name, runtime.doc_count
                    )
                except Exception as exc:
                    log.error("Failed to boot workspace '%s': %s", record.name, exc)

    # ── RPC handlers ────────────────────────────────────────────────────

    def handle_workspaces_register(self, params: dict[str, Any]) -> dict[str, Any]:
        name = params["name"]
        root_path = str(Path(params["root_path"]).resolve())
        include_globs = params.get("include_globs", ["**/*.md"])
        exclude_globs = params.get("exclude_globs", [])

        with self._lock:
            # Idempotent: return existing if root_path already registered
            for rec in self._workspaces.values():
                if rec.root_path == root_path:
                    log.info("Workspace '%s' already registered (id=%s)", rec.name, rec.workspace_id)
                    return {"workspace_id": rec.workspace_id, "existed": True}

            if not os.path.isdir(root_path):
                raise ValueError(f"root_path does not exist or is not a directory: {root_path}")

            workspace_id = str(uuid.uuid4())
            record = WorkspaceRecord(
                workspace_id=workspace_id,
                name=name,
                root_path=root_path,
                include_globs=include_globs,
                exclude_globs=exclude_globs,
            )
            self._workspaces[workspace_id] = record
            self._save_registry()

        # Engine init (slow — embeddings) outside the lock
        try:
            log.info("Registering workspace '%s' at %s…", name, root_path)
            runtime = self._init_engine(record)
            with self._lock:
                self._runtimes[workspace_id] = runtime
            _ensure_index_thread(runtime)
            self._start_observer(runtime)
            log.info("Workspace '%s' registered — %d docs", name, runtime.doc_count)
            return {"workspace_id": workspace_id, "existed": False, "doc_count": runtime.doc_count}
        except Exception as exc:
            # Roll back
            with self._lock:
                self._workspaces.pop(workspace_id, None)
                self._save_registry()
            raise RuntimeError(f"Engine initialization failed: {exc}") from exc

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
        workspace_id = params["workspace_id"]
        delete_data = params.get("delete_data", False)

        with self._lock:
            if workspace_id not in self._workspaces:
                raise KeyError(f"Workspace not found: {workspace_id}")

            # Stop observer
            self._stop_observer(workspace_id)

            # Stop indexer thread
            rt = self._runtimes.pop(workspace_id, None)
            if rt:
                _stop_index_thread(rt)

            self._workspaces.pop(workspace_id)
            self._save_registry()

        if delete_data:
            data_path = DATA_DIR / workspace_id
            if data_path.exists():
                shutil.rmtree(data_path, ignore_errors=True)
                log.info("Deleted workspace data: %s", data_path)

        return {"ok": True}

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
        workspace_id = params["workspace_id"]
        with self._lock:
            rt = self._runtimes.get(workspace_id)
        if not rt:
            raise KeyError(f"Workspace not initialized: {workspace_id}")
        return _enqueue_index_job(
            rt,
            params.get("changed_paths", []),
            params.get("deleted_paths", []),
        )

    def handle_index_rebuild(self, params: dict[str, Any]) -> dict[str, Any]:
        workspace_id = params["workspace_id"]
        with self._lock:
            rt = self._runtimes.get(workspace_id)
        if not rt:
            raise KeyError(f"Workspace not initialized: {workspace_id}")

        job_id = str(uuid.uuid4())
        rt.state = "indexing"
        try:
            with rt._engine_lock:
                rt.engine.reindex()
                rt.last_indexed_at = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        except Exception as exc:
            log.error("Workspace '%s': reindex failed: %s", rt.record.name, exc)
            raise
        finally:
            rt.state = "idle"
        return {"job_id": job_id, "doc_count": rt.doc_count}

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

    def _get_runtime(self, params: dict[str, Any]) -> Optional[WorkspaceRuntime]:
        """Resolve workspace_id from params; None if not specified and there's exactly one workspace."""
        workspace_id = params.get("workspace_id")
        with self._lock:
            if workspace_id:
                return self._runtimes.get(workspace_id)
            # Implicit: use the only workspace if there's exactly one
            if len(self._runtimes) == 1:
                return next(iter(self._runtimes.values()))
        return None

    def handle_query_retrieve(self, params: dict[str, Any]) -> dict[str, Any]:
        rt = self._get_runtime(params)
        if not rt:
            raise RuntimeError("No workspace available for query. Register a workspace first.")
        workspace_filter = params.get("workspace")  # frontmatter workspace field filter
        top_k = params.get("top_k")
        query = params["query"]

        with rt._engine_lock:
            result = rt.engine.retrieve(
                query=query,
                top_k=top_k,
                workspace=workspace_filter,
            )
        return result

    def handle_query_find_path(self, params: dict[str, Any]) -> dict[str, Any]:
        rt = self._get_runtime(params)
        if not rt:
            raise RuntimeError("No workspace available for query. Register a workspace first.")
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
            ws_summary = [
                {"workspace_id": wid, "name": rt.record.name, "state": rt.state, "doc_count": rt.doc_count}
                for wid, rt in self._runtimes.items()
            ]

        return {
            "status": "ok",
            "uptime_secs": round(uptime, 1),
            "rss_mb": round(rss_mb, 1) if rss_mb is not None else None,
            "workspaces": ws_summary,
        }

    def handle_daemon_shutdown(self, params: dict[str, Any]) -> dict[str, Any]:
        log.info("Shutdown requested via RPC")
        # Signal all indexer threads to stop
        with self._lock:
            runtimes = list(self._runtimes.values())
        for rt in runtimes:
            _stop_index_thread(rt)
        # Stop all observers
        with self._lock:
            ids = list(self._observers.keys())
        for wid in ids:
            self._stop_observer(wid)
        return {"ok": True}

    # ── Dispatch ─────────────────────────────────────────────────────────

    HANDLERS: dict[str, str] = {
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

    # These methods can block (embedding, indexing, retrieval) and must run in a thread executor
    _BLOCKING_METHODS = {
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


# ── asyncio TCP server ───────────────────────────────────────────────────────


_daemon: _DaemonServer = _DaemonServer()
_shutdown_event: asyncio.Event


async def _handle_client(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    peer = writer.get_extra_info("peername")
    log.debug("Client connected: %s", peer)

    loop = asyncio.get_event_loop()

    try:
        while True:
            try:
                line = await reader.readline()
            except asyncio.IncompleteReadError:
                break
            if not line:
                break

            line_str = line.decode("utf-8", errors="replace").strip()
            if not line_str:
                continue

            try:
                req = json.loads(line_str)
            except json.JSONDecodeError as exc:
                log.warning("JSON parse error from %s: %s", peer, exc)
                continue

            req_id = req.get("id")
            method = req.get("method", "")
            params = req.get("params", {})

            try:
                if method in _daemon._BLOCKING_METHODS:
                    result = await loop.run_in_executor(None, _daemon.dispatch, method, params)
                else:
                    result = _daemon.dispatch(method, params)
                resp = {"id": req_id, "result": result}
            except Exception as exc:
                log.error("RPC %s failed: %s", method, exc)
                resp = {"id": req_id, "error": {"code": -1, "message": str(exc)}}

            try:
                writer.write((json.dumps(resp) + "\n").encode("utf-8"))
                await writer.drain()
            except Exception:
                break

            if method == "daemon.shutdown":
                _shutdown_event.set()
                break

    finally:
        log.debug("Client disconnected: %s", peer)
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:
            pass


def _log_metrics() -> None:
    try:
        import psutil
        rss_mb = psutil.Process().memory_info().rss / (1024.0 * 1024.0)
    except Exception:
        rss_mb = None

    with _daemon._lock:
        workspaces = list(_daemon._runtimes.values())

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


async def _preflight_check() -> bool:
    """Return True if our daemon is already running on DAEMON_PORT, exit(1) if another process owns it."""
    try:
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection("127.0.0.1", DAEMON_PORT), timeout=1.0
        )
    except (ConnectionRefusedError, asyncio.TimeoutError, OSError):
        return False  # Port is free

    # Something is listening — probe for our daemon
    alive = False
    try:
        writer.write(
            (json.dumps({"id": "preflight", "method": "daemon.health", "params": {}}) + "\n").encode()
        )
        await writer.drain()
        line = await asyncio.wait_for(reader.readline(), timeout=2.0)
        resp = json.loads(line.decode())
        alive = resp.get("result", {}).get("status") == "ok"
    except Exception:
        pass
    finally:
        try:
            writer.close()
            await writer.wait_closed()
        except Exception:
            pass

    if alive:
        log.info("ContextGarden daemon already running on port %d — exiting.", DAEMON_PORT)
        return True

    log.error(
        "Port %d is in use by a non-daemon process. "
        "Stop that process or set CG_DAEMON_PORT to a different value.",
        DAEMON_PORT,
    )
    sys.exit(1)


async def _run_server() -> None:
    global _shutdown_event
    _shutdown_event = asyncio.Event()

    if await _preflight_check():
        return  # Another daemon instance is already running

    server = await asyncio.start_server(
        _handle_client,
        host="127.0.0.1",
        port=DAEMON_PORT,
    )

    addrs = ", ".join(str(s.getsockname()) for s in server.sockets)
    log.info("ContextGarden daemon listening on %s", addrs)

    asyncio.ensure_future(_periodic_metrics())

    async with server:
        await _shutdown_event.wait()

    log.info("Daemon shutting down")


# ── Entry point ──────────────────────────────────────────────────────────────


def _sync_port_check() -> bool:
    """Quick synchronous check: is something already on DAEMON_PORT?"""
    import socket as _socket
    try:
        s = _socket.create_connection(("127.0.0.1", DAEMON_PORT), timeout=1.0)
        s.close()
        return True
    except OSError:
        return False


def main() -> None:
    log.info("ContextGarden daemon starting (data_dir=%s, port=%d)", DATA_DIR, DAEMON_PORT)

    # Fast pre-check before expensive workspace boot — avoids wasting time
    # loading 2000+ notes only to discover the port is already taken.
    if _sync_port_check():
        log.info("Port %d is already in use — running full preflight via asyncio.", DAEMON_PORT)
        asyncio.run(_run_server())
        return

    _daemon.load_registry()
    _daemon.boot_workspaces()

    try:
        asyncio.run(_run_server())
    except KeyboardInterrupt:
        log.info("Daemon interrupted by user")


if __name__ == "__main__":
    main()
