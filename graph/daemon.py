"""ContextGarden Python Daemon — persistent TCP server for knowledge graph operations.

Replaces the stdio subprocess model. Runs persistently (started at OS login, or
spawned by the TS gateway as a fallback). Exposes JSON-RPC over localhost TCP.

Architecture
------------
  asyncio TCP server on 127.0.0.1:7432
  Per-workspace KnowledgeGraphEngine instances
  Per-workspace watchdog file watchers (debounced 500 ms, .md files only)
  Workspace registry persisted to <dataDir>/.context-garden/registry.json
  Per-workspace index persisted to <dataDir>/.context-garden/knowledge_graph/<workspace_id>/index/
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
import signal
import socket
import sqlite3
import subprocess
import shutil
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Optional

# ── Config ──────────────────────────────────────────────────────────────────

DAEMON_HOST = "127.0.0.1"
DAEMON_PORT = 7432
MAX_PENDING_JOBS = 50
DEBOUNCE_SECS = 0.5
METRICS_INTERVAL_SECS = 300
RUNTIME_READY_TIMEOUT_SECS = 90.0

DATA_DIR = Path.cwd()
STATE_DIR = DATA_DIR / ".context-garden"
REGISTRY_PATH = STATE_DIR / "registry.json"

logging.basicConfig(
    stream=sys.stderr,
    level=logging.INFO,
    format="[contextgarden] %(levelname)s %(name)s: %(message)s",
)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger(__name__)


# ── Structured SQLite logger ─────────────────────────────────────────────────


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


class DaemonLogger:
    """Writes connection and RPC events to a SQLite database on a background thread.

    All public methods are fire-and-forget: they enqueue a write and return
    immediately.  The asyncio event loop never touches sqlite3 directly.

    Schema
    ------
    connections(conn_id, peer, opened_at, closed_at, calls)
    rpc_calls(id, ts, conn_id, method, req_id, workspace_id, workspace,
              duration_ms, event, error)
    """

    _SENTINEL = object()

    def __init__(self) -> None:
        self._q: _queue.Queue[Any] = _queue.Queue()
        self._thread: Optional[threading.Thread] = None
        self._db_path: Optional[Path] = None

    # ── Init ─────────────────────────────────────────────────────────────

    def start(self, db_path: Path) -> None:
        """Open (or create) the log DB and start the writer thread."""
        self._db_path = db_path
        self._thread = threading.Thread(
            target=self._writer_loop,
            name="daemon-logger",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        """Flush remaining writes and stop the writer thread."""
        self._q.put(self._SENTINEL)
        if self._thread:
            self._thread.join(timeout=5)

    # ── Public API (fire-and-forget) ──────────────────────────────────────

    def log_conn_open(self, conn_id: str, peer: str) -> None:
        self._q.put(("conn_open", conn_id, peer, _now_iso()))

    def log_conn_close(self, conn_id: str, calls: int) -> None:
        self._q.put(("conn_close", conn_id, calls, _now_iso()))

    def log_rpc(
        self,
        conn_id: str,
        method: str,
        req_id: Any,
        workspace_id: Optional[str],
        workspace: Optional[str],
        duration_ms: float,
        event: str,           # 'rpc.ok' | 'rpc.error'
        error: Optional[str],
    ) -> None:
        self._q.put((
            "rpc", _now_iso(), conn_id, method, str(req_id) if req_id is not None else None,
            workspace_id, workspace, duration_ms, event, error,
        ))

    # ── Writer thread ─────────────────────────────────────────────────────

    def _writer_loop(self) -> None:
        assert self._db_path is not None
        try:
            con = sqlite3.connect(str(self._db_path), check_same_thread=False)
        except Exception as exc:
            log.error("DaemonLogger: failed to open %s: %s", self._db_path, exc)
            return

        try:
            con.execute("PRAGMA journal_mode=WAL")
            con.execute("PRAGMA synchronous=NORMAL")
            self._migrate(con)
            con.commit()

            while True:
                item = self._q.get()
                if item is self._SENTINEL:
                    break
                try:
                    self._handle(con, item)
                    con.commit()
                except Exception as exc:
                    log.warning("DaemonLogger write error: %s", exc)
        finally:
            try:
                con.close()
            except Exception:
                pass

    def _migrate(self, con: sqlite3.Connection) -> None:
        con.executescript("""
            CREATE TABLE IF NOT EXISTS connections (
                conn_id   TEXT PRIMARY KEY,
                peer      TEXT,
                opened_at TEXT,
                closed_at TEXT,
                calls     INTEGER DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS rpc_calls (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                ts           TEXT,
                conn_id      TEXT,
                method       TEXT,
                req_id       TEXT,
                workspace_id TEXT,
                workspace    TEXT,
                duration_ms  REAL,
                event        TEXT,
                error        TEXT
            );

            CREATE INDEX IF NOT EXISTS rpc_calls_ts     ON rpc_calls(ts);
            CREATE INDEX IF NOT EXISTS rpc_calls_method ON rpc_calls(method);
            CREATE INDEX IF NOT EXISTS rpc_calls_event  ON rpc_calls(event);
        """)

    def _handle(self, con: sqlite3.Connection, item: Any) -> None:
        kind = item[0]
        if kind == "conn_open":
            _, conn_id, peer, opened_at = item
            con.execute(
                "INSERT OR IGNORE INTO connections(conn_id, peer, opened_at) VALUES (?,?,?)",
                (conn_id, peer, opened_at),
            )
        elif kind == "conn_close":
            _, conn_id, calls, closed_at = item
            con.execute(
                "UPDATE connections SET closed_at=?, calls=? WHERE conn_id=?",
                (closed_at, calls, conn_id),
            )
        elif kind == "rpc":
            (_, ts, conn_id, method, req_id, workspace_id,
             workspace, duration_ms, event, error) = item
            con.execute(
                """INSERT INTO rpc_calls
                   (ts, conn_id, method, req_id, workspace_id, workspace,
                    duration_ms, event, error)
                   VALUES (?,?,?,?,?,?,?,?,?)""",
                (ts, conn_id, method, req_id, workspace_id, workspace,
                 duration_ms, event, error),
            )


_logger = DaemonLogger()


def configure_paths(data_dir: str | Path) -> None:
    global DATA_DIR, STATE_DIR, REGISTRY_PATH
    DATA_DIR = Path(data_dir).resolve()
    STATE_DIR = DATA_DIR / ".context-garden"
    REGISTRY_PATH = STATE_DIR / "registry.json"


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
            log.warning(
                "Workspace '%s': failed to enqueue indexer shutdown sentinel (queue still full)",
                runtime.record.name,
            )


# ── Daemon server state ──────────────────────────────────────────────────────


class _DaemonServer:
    def __init__(self) -> None:
        self._workspaces: dict[str, WorkspaceRecord] = {}   # id → record
        self._runtimes: dict[str, WorkspaceRuntime] = {}    # id → runtime
        self._observers: dict[str, Any] = {}                # id → watchdog Observer
        self._lock = threading.RLock()
        self._runtime_ready = threading.Condition(self._lock)
        self._boot_errors: dict[str, str] = {}
        self._start_time = time.monotonic()
        self._active_connections: dict[str, dict[str, Any]] = {}  # conn_id → info

    # ── Connection tracking (called from asyncio handler) ────────────────

    def _conn_open(self, conn_id: str, peer: str, opened_at: str) -> None:
        with self._lock:
            self._active_connections[conn_id] = {
                "conn_id": conn_id,
                "peer": peer,
                "opened_at": opened_at,
                "call_count": 0,
            }

    def _conn_close(self, conn_id: str) -> None:
        with self._lock:
            self._active_connections.pop(conn_id, None)

    def _conn_inc(self, conn_id: str) -> None:
        with self._lock:
            if conn_id in self._active_connections:
                self._active_connections[conn_id]["call_count"] += 1

    # ── Registry persistence ─────────────────────────────────────────────

    def load_registry(self) -> None:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        if not REGISTRY_PATH.exists():
            return
        try:
            data = json.loads(REGISTRY_PATH.read_text("utf-8"))
            for entry in data:
                rec = WorkspaceRecord.from_dict(entry)
                self._workspaces[rec.workspace_id] = rec
            log.info("Loaded %d workspace(s) from registry", len(self._workspaces))
        except Exception as exc:
            log.error("Failed to load registry: %s", exc)

    def _save_registry(self) -> None:
        data = [rec.to_dict() for rec in self._workspaces.values()]
        REGISTRY_PATH.parent.mkdir(parents=True, exist_ok=True)
        REGISTRY_PATH.write_text(json.dumps(data, indent=2), "utf-8")

    # ── Workspace lifecycle ──────────────────────────────────────────────

    def _index_dir(self, workspace_id: str) -> str:
        return str(STATE_DIR / "knowledge_graph" / workspace_id / "index")

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
                        log.info(
                            "Workspace '%s' was removed while booting - discarding runtime",
                            record.name,
                        )
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
                self._boot_errors.pop(workspace_id, None)
                self._runtime_ready.notify_all()
            _ensure_index_thread(runtime)
            self._start_observer(runtime)
            log.info("Workspace '%s' registered — %d docs", name, runtime.doc_count)
            return {"workspace_id": workspace_id, "existed": False, "doc_count": runtime.doc_count}
        except Exception as exc:
            # Roll back
            with self._lock:
                self._workspaces.pop(workspace_id, None)
                self._boot_errors.pop(workspace_id, None)
                self._save_registry()
                self._runtime_ready.notify_all()
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
        workspace_id = params.get("workspace_id")
        name = params.get("name")
        delete_data = params.get("delete_data", False)

        # Allow lookup by name when workspace_id (UUID) is not provided
        if not workspace_id and name:
            with self._lock:
                for wid, rec in self._workspaces.items():
                    if rec.name == name:
                        workspace_id = wid
                        break
            if not workspace_id:
                log.debug("workspaces.unregister: '%s' not found — already gone", name)
                return {"ok": True, "existed": False}
        elif not workspace_id:
            raise ValueError("workspace_id or name is required")

        with self._lock:
            if workspace_id not in self._workspaces:
                log.debug("workspaces.unregister: %s not found — already gone", workspace_id)
                return {"ok": True, "existed": False}

            # Stop observer
            self._stop_observer(workspace_id)

            # Stop indexer thread
            rt = self._runtimes.pop(workspace_id, None)
            if rt:
                _stop_index_thread(rt)

            self._workspaces.pop(workspace_id)
            self._boot_errors.pop(workspace_id, None)
            self._save_registry()
            self._runtime_ready.notify_all()

        if delete_data:
            data_path = STATE_DIR / "knowledge_graph" / workspace_id
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
        workspace_id = params.get("workspace_id")
        with self._lock:
            if workspace_id:
                targets = [self._runtimes[workspace_id]] if workspace_id in self._runtimes else []
            else:
                targets = list(self._runtimes.values())
        if not targets:
            raise KeyError(
                f"Workspace not initialized: {workspace_id}" if workspace_id
                else "No initialized workspaces to rebuild"
            )

        job_id = str(uuid.uuid4())
        total_docs = 0
        for rt in targets:
            rt.state = "indexing"
            try:
                with rt._engine_lock:
                    rt.engine.reindex()
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
        """Resolve workspace_id, optionally waiting for background boot to finish."""
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
                    # Implicit: use the only workspace if there's exactly one.
                    if len(self._runtimes) == 1:
                        return next(iter(self._runtimes.values()))
                    if len(self._runtimes) > 1:
                        return None  # caller must fan-out across all workspaces
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
        workspace_filter = params.get("workspace")  # frontmatter workspace field filter
        top_k = params.get("top_k")
        query = params["query"]

        # Multi-workspace fan-out: when no workspace_id is specified and multiple
        # workspaces are registered, search all and merge by score.
        if not params.get("workspace_id"):
            with self._lock:
                runtimes = list(self._runtimes.values())
            if len(runtimes) > 1:
                all_seeds: list[dict] = []
                all_expanded: list[dict] = []
                for rt in runtimes:
                    with rt._engine_lock:
                        res = rt.engine.retrieve(query=query, top_k=top_k, workspace=workspace_filter)
                    all_seeds.extend(res.get("seed_notes", []))
                    all_expanded.extend(res.get("expanded_notes", []))

                def _dedup_by_score(notes: list[dict]) -> list[dict]:
                    seen: dict[str, dict] = {}
                    for n in notes:
                        nid = n.get("noteId", "")
                        if nid not in seen or n.get("score", 0) > seen[nid].get("score", 0):
                            seen[nid] = n
                    return sorted(seen.values(), key=lambda x: x.get("score", 0), reverse=True)

                return {
                    "seed_notes": _dedup_by_score(all_seeds),
                    "expanded_notes": _dedup_by_score(all_expanded),
                }

        rt = self._get_runtime(params, wait_secs=RUNTIME_READY_TIMEOUT_SECS)
        if not rt:
            raise RuntimeError("Workspace is still initializing; try again shortly.")
        with rt._engine_lock:
            result = rt.engine.retrieve(
                query=query,
                top_k=top_k,
                workspace=workspace_filter,
            )
        return result

    def handle_query_find_path(self, params: dict[str, Any]) -> dict[str, Any]:
        rt = self._get_runtime(params, wait_secs=RUNTIME_READY_TIMEOUT_SECS)
        if not rt:
            raise RuntimeError("Workspace is still initializing; try again shortly.")
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
        except Exception as exc:
            log.debug("psutil unavailable for daemon.health memory stats: %s", exc)

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
            active_conns = list(self._active_connections.values())

        return {
            "status": "ok",
            "uptime_secs": round(uptime, 1),
            "rss_mb": round(rss_mb, 1) if rss_mb is not None else None,
            "data_dir": str(DATA_DIR),
            "workspaces": ws_summary,
            "active_connections": active_conns,
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
    conn_id = str(uuid.uuid4())[:8]
    call_count = 0

    log.info("conn.open  peer=%s conn_id=%s", peer, conn_id)
    _logger.log_conn_open(conn_id, str(peer))
    _daemon._conn_open(conn_id, str(peer), _now_iso())

    loop = asyncio.get_event_loop()

    try:
        while True:
            try:
                line = await reader.readline()
            except (asyncio.IncompleteReadError, ConnectionResetError, OSError):
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

            t0 = time.monotonic()
            try:
                if method in _daemon._BLOCKING_METHODS:
                    result = await loop.run_in_executor(None, _daemon.dispatch, method, params)
                else:
                    result = _daemon.dispatch(method, params)

                dur_ms = round((time.monotonic() - t0) * 1000, 1)
                log.info(
                    "rpc.ok   conn=%s method=%s duration_ms=%.1f",
                    conn_id, method, dur_ms,
                )
                _logger.log_rpc(
                    conn_id, method, req_id,
                    params.get("workspace_id"), params.get("workspace"),
                    dur_ms, "rpc.ok", None,
                )
                resp = {"id": req_id, "result": result}
            except Exception as exc:
                dur_ms = round((time.monotonic() - t0) * 1000, 1)
                log.error(
                    "rpc.error conn=%s method=%s duration_ms=%.1f error=%s",
                    conn_id, method, dur_ms, exc,
                )
                _logger.log_rpc(
                    conn_id, method, req_id,
                    params.get("workspace_id"), params.get("workspace"),
                    dur_ms, "rpc.error", str(exc),
                )
                resp = {"id": req_id, "error": {"code": -1, "message": str(exc)}}

            call_count += 1
            _daemon._conn_inc(conn_id)

            try:
                writer.write((json.dumps(resp) + "\n").encode("utf-8"))
                await writer.drain()
            except Exception as exc:
                log.debug("Client write failed for %s: %s", peer, exc)
                break

            if method == "daemon.shutdown":
                _shutdown_event.set()
                break

    finally:
        log.info("conn.close peer=%s conn_id=%s calls=%d", peer, conn_id, call_count)
        _logger.log_conn_close(conn_id, call_count)
        _daemon._conn_close(conn_id)
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


async def _boot_workspaces_in_background() -> None:
    try:
        await asyncio.to_thread(_daemon.boot_workspaces)
    except Exception as exc:
        log.error("Background workspace boot failed: %s", exc)


class DaemonAlreadyRunning(Exception):
    """Raised when a healthy daemon with the same data-dir is already on the port."""


def _is_addr_in_use(exc: OSError) -> bool:
    return getattr(exc, "winerror", None) == 10048 or getattr(exc, "errno", None) in (48, 98)


def _rpc_request(method: str, params: dict[str, Any], timeout_secs: float = 2.0) -> Optional[dict[str, Any]]:
    payload = (json.dumps({"id": "lifecycle", "method": method, "params": params}) + "\n").encode("utf-8")
    try:
        with socket.create_connection((DAEMON_HOST, DAEMON_PORT), timeout=timeout_secs) as sock:
            sock.settimeout(timeout_secs)
            sock.sendall(payload)
            buf = b""
            while b"\n" not in buf:
                chunk = sock.recv(4096)
                if not chunk:
                    break
                buf += chunk
        line = buf.split(b"\n", 1)[0].decode("utf-8", errors="replace").strip()
        if not line:
            return None
        parsed = json.loads(line)
        return parsed if isinstance(parsed, dict) else None
    except Exception as exc:
        log.debug("RPC probe failed for %s: %s", method, exc)
        return None


def _port_is_open() -> bool:
    try:
        with socket.create_connection((DAEMON_HOST, DAEMON_PORT), timeout=0.6):
            return True
    except OSError:
        return False


def _is_contextgarden_daemon() -> bool:
    response = _rpc_request("daemon.health", {})
    return bool(response and isinstance(response.get("result"), dict) and response["result"].get("status") == "ok")


def _request_existing_shutdown() -> bool:
    response = _rpc_request("daemon.shutdown", {})
    return bool(response and isinstance(response.get("result"), dict) and response["result"].get("ok") is True)


def _wait_port_free(timeout_secs: float) -> bool:
    deadline = time.time() + timeout_secs
    while time.time() < deadline:
        if not _port_is_open():
            return True
        time.sleep(0.2)
    return not _port_is_open()


def _find_listener_pid() -> Optional[int]:
    try:
        import psutil
    except Exception as exc:
        log.warning("psutil unavailable; cannot resolve listener PID for forced restart: %s", exc)
        return None

    try:
        for conn in psutil.net_connections(kind="tcp"):
            laddr = getattr(conn, "laddr", None)
            if not laddr:
                continue
            if getattr(laddr, "ip", None) != DAEMON_HOST:
                continue
            if getattr(laddr, "port", None) != DAEMON_PORT:
                continue
            if conn.status != "LISTEN":
                continue
            if conn.pid:
                return int(conn.pid)
    except Exception as exc:
        log.warning("Failed to inspect listener PID: %s", exc)
    return None


def _force_kill_pid(pid: int) -> None:
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/PID", str(pid), "/T", "/F"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )
        return

    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        return
    time.sleep(0.5)
    if _port_is_open():
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            return


def _restart_existing_daemon_if_needed() -> None:
    if not _port_is_open():
        return

    response = _rpc_request("daemon.health", {})
    result = response.get("result") if response else None
    if not (isinstance(result, dict) and result.get("status") == "ok"):
        raise RuntimeError(
            f"Port {DAEMON_PORT} is already in use by a non-ContextGarden process; refusing to kill it."
        )

    # Same data-dir → a peer process already started the daemon we'd become.
    # Exit cleanly so the caller's _spawnDaemon() probe finds the running instance.
    existing_data_dir = result.get("data_dir")
    if existing_data_dir:
        try:
            if Path(existing_data_dir).resolve() == DATA_DIR.resolve():
                raise DaemonAlreadyRunning(
                    f"healthy daemon already running with same data_dir={existing_data_dir}"
                )
        except DaemonAlreadyRunning:
            raise
        except Exception:
            pass  # path comparison failed; fall through to restart

    log.info("Existing daemon detected on %s:%d (different data_dir); requesting graceful shutdown", DAEMON_HOST, DAEMON_PORT)
    _request_existing_shutdown()
    if _wait_port_free(10.0):
        log.info("Existing daemon stopped gracefully")
        return

    pid = _find_listener_pid()
    if pid is None:
        raise RuntimeError(
            f"Existing daemon did not stop within 10s and listener PID could not be resolved on port {DAEMON_PORT}"
        )

    log.warning("Existing daemon did not stop within 10s; force killing pid=%d", pid)
    _force_kill_pid(pid)
    if not _wait_port_free(5.0):
        raise RuntimeError(f"Failed to free port {DAEMON_PORT} after force kill")
    log.info("Forced restart complete; port %d is free", DAEMON_PORT)


async def _run_server() -> None:
    global _shutdown_event
    _shutdown_event = asyncio.Event()

    try:
        server = await asyncio.start_server(
            _handle_client,
            host=DAEMON_HOST,
            port=DAEMON_PORT,
        )
    except OSError as exc:
        if not _is_addr_in_use(exc):
            raise
        _restart_existing_daemon_if_needed()
        server = await asyncio.start_server(
            _handle_client,
            host=DAEMON_HOST,
            port=DAEMON_PORT,
        )

    addrs = ", ".join(str(s.getsockname()) for s in server.sockets)
    log.info("ContextGarden daemon listening on %s", addrs)

    asyncio.ensure_future(_periodic_metrics())
    asyncio.create_task(_boot_workspaces_in_background())

    async with server:
        await _shutdown_event.wait()

    log.info("Daemon shutting down")


# Entry point


def main(data_dir: Optional[str] = None) -> None:
    chosen_data_dir = Path(data_dir).resolve() if data_dir else Path.cwd().resolve()
    configure_paths(chosen_data_dir)

    from .config import set_data_dir
    set_data_dir(DATA_DIR)

    STATE_DIR.mkdir(parents=True, exist_ok=True)
    _logger.start(STATE_DIR / "daemon.log.db")

    log.info("ContextGarden daemon starting (data_dir=%s, port=%d)", DATA_DIR, DAEMON_PORT)

    try:
        _restart_existing_daemon_if_needed()
    except DaemonAlreadyRunning as exc:
        log.info("Daemon already running with same config — exiting: %s", exc)
        return

    _daemon.load_registry()

    try:
        asyncio.run(_run_server())
    except KeyboardInterrupt:
        log.info("Daemon interrupted by user")
    except DaemonAlreadyRunning as exc:
        # Late-race: another process bound the port between our check and asyncio.start_server
        log.info("Daemon already running (late-race) — exiting: %s", exc)
    finally:
        _logger.stop()


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Run ContextGarden daemon")
    parser.add_argument(
        "--data-dir",
        default=str(Path.cwd()),
        help="Canonical data directory root (default: current working directory)",
    )
    cli_args = parser.parse_args()
    main(data_dir=cli_args.data_dir)
