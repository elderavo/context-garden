"""JSON-RPC stdio server — entry point for the TS subprocess bridge.

Reads newline-delimited JSON from stdin, dispatches to KnowledgeGraphEngine,
writes responses to stdout. stderr is reserved for Python logging.

Handlers run synchronously in the main thread. incremental_update is the
exception: it queues work to a background thread and returns {queued: true}
immediately, so retrieve/find_path are never blocked by an ongoing reindex.
The background thread serializes index writes via a queue.Queue — no concurrent
writes to the LlamaIndex state. No stdout writes from the background thread
(avoids the stdout/engine lock inversion that caused the old ThreadPoolExecutor
deadlock).
"""

from __future__ import annotations

import logging
import queue as _queue_module
import sys
import threading
import traceback
from dataclasses import asdict
from typing import Any

from .engine import KnowledgeGraphEngine
from .protocol import RpcError, RpcRequest, RpcResponse, parse_request, send_response, send_notification

# Configure logging to stderr (never stdout — that's the RPC channel)
logging.basicConfig(
    stream=sys.stderr,
    level=logging.INFO,
    format="[knowledge_graph] %(levelname)s %(name)s: %(message)s",
)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Handler registry
# ---------------------------------------------------------------------------

engine = KnowledgeGraphEngine()

# ---------------------------------------------------------------------------
# Background indexer
# ---------------------------------------------------------------------------
# A single daemon thread serialises incremental_update calls so the main
# RPC loop never blocks on embedding. The queue holds param dicts; None is
# the shutdown sentinel.

_index_queue: _queue_module.Queue[dict[str, Any] | None] = _queue_module.Queue()
_index_thread: threading.Thread | None = None
_index_thread_lock = threading.Lock()


def _index_worker() -> None:
    while True:
        params = _index_queue.get()
        if params is None:
            _index_queue.task_done()
            break
        try:
            engine.incremental_update(
                changed_paths=params.get("changed_paths", []),
                deleted_paths=params.get("deleted_paths", []),
                progress_cb=None,  # no stdout writes from background thread
            )
        except Exception as exc:
            log.error("Background incremental_update failed: %s", exc)
        finally:
            _index_queue.task_done()


def _ensure_index_thread() -> None:
    global _index_thread
    with _index_thread_lock:
        if _index_thread is None or not _index_thread.is_alive():
            _index_thread = threading.Thread(target=_index_worker, name="indexer", daemon=True)
            _index_thread.start()


def handle_initialize(params: dict[str, Any]) -> dict[str, Any]:
    # ollama_host and embed_model are optional — env vars take precedence
    kwargs: dict[str, Any] = {}
    if "ollama_host" in params:
        kwargs["ollama_host"] = params["ollama_host"]
    if "embed_model" in params:
        kwargs["embed_model"] = params["embed_model"]

    return engine.initialize(
        md_db_path=params["md_db_path"],
        db_dir=params["db_dir"],
        top_k=params.get("top_k", 8),
        **kwargs,
    )


def handle_retrieve(params: dict[str, Any]) -> dict[str, Any]:
    return engine.retrieve(
        query=params["query"],
        top_k=params.get("top_k"),
        workspace=params.get("workspace"),
    )


def handle_get_note_content(params: dict[str, Any]) -> dict[str, Any]:
    body = engine.get_note_content(params["relative_path"])
    return {"body": body}


def handle_reindex(params: dict[str, Any]) -> dict[str, Any]:
    return engine.reindex()


def handle_incremental_update(params: dict[str, Any]) -> dict[str, Any]:
    """Queue an incremental index update and return immediately.

    The actual embedding + graph work runs in the background indexer thread.
    This keeps the main RPC loop free to serve retrieve/find_path requests.
    """
    _ensure_index_thread()
    _index_queue.put(params)
    return {"queued": True, "queue_depth": _index_queue.qsize()}


def handle_build_prune_clusters(params: dict[str, Any]) -> dict[str, Any]:
    from .pruner import build_prune_clusters
    from .models import PruneConfig

    config = PruneConfig(
        similarity_threshold=params.get("similarity_threshold", 0.85),
        max_neighbors_per_note=params.get("max_neighbors", 10),
        min_cluster_size=params.get("min_cluster_size", 2),
        include_note_types=params.get("include_types", ["concept", "tool"]),
        exclude_tags=params.get("exclude_tags", []),
    )
    clusters = build_prune_clusters(
        config=config,
        index=engine.index,
        embed_model=engine.embed_model,
        parsed_notes=engine._parsed_notes,
    )
    return {"clusters": [_cluster_to_dict(c) for c in clusters]}


def handle_get_graph_stats(params: dict[str, Any]) -> dict[str, Any]:
    return engine.get_graph_stats()


def handle_find_path(params: dict[str, Any]) -> dict[str, Any]:
    return engine.find_path(
        start_query=params["start"],
        end_query=params["end"],
        edge_types=params.get("edge_types"),
        max_depth=params.get("max_depth", 8),
    )


def handle_shutdown(params: dict[str, Any]) -> dict[str, Any]:
    # Signal the indexer thread to stop after draining its queue
    _index_queue.put(None)
    return {"ok": True}


# ---------------------------------------------------------------------------
# Dispatch table
# ---------------------------------------------------------------------------

HANDLERS: dict[str, Any] = {
    "initialize": handle_initialize,
    "retrieve": handle_retrieve,
    "get_note_content": handle_get_note_content,
    "reindex": handle_reindex,
    "incremental_update": handle_incremental_update,
    "build_prune_clusters": handle_build_prune_clusters,
    "get_graph_stats": handle_get_graph_stats,
    "find_path": handle_find_path,
    "shutdown": handle_shutdown,
}


# ---------------------------------------------------------------------------
# Server loop
# ---------------------------------------------------------------------------


def run_server(standalone: bool = False) -> None:
    """Run the JSON-RPC stdio loop.

    Handlers run synchronously in the main thread. incremental_update is the
    exception — it returns immediately after queuing work to the background
    indexer thread, so retrieve/find_path are never blocked by a reindex.
    """
    log.info("Knowledge graph server starting (standalone=%s)", standalone)

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue

        try:
            req = parse_request(line)
        except Exception as exc:
            log.error("Failed to parse request: %s", exc)
            continue

        if standalone:
            log.info("→ %s(%s)", req.method, req.params)

        handler = HANDLERS.get(req.method)
        if handler is None:
            send_response(RpcError(id=req.id, code=-1, message=f"Unknown method: {req.method}"))
            continue

        try:
            result = handler(req.params)
            send_response(RpcResponse(id=req.id, result=result))
        except Exception as exc:
            tb = traceback.format_exc()
            log.error("Handler %s failed:\n%s", req.method, tb)
            send_response(RpcError(id=req.id, code=-1, message=str(exc)))

        if req.method == "shutdown":
            log.info("Shutdown requested, exiting")
            break

    log.info("Server exiting")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _cluster_to_dict(cluster: Any) -> dict[str, Any]:
    """Convert a PruneCluster dataclass to a JSON-serializable dict."""
    from dataclasses import asdict

    return {
        "clusterId": cluster.cluster_id,
        "representativeNoteId": cluster.representative_note_id,
        "members": [
            {
                "noteId": m.note_id,
                "similarity": m.similarity,
                "isRepresentative": m.is_representative,
                "status": m.status,
            }
            for m in cluster.members
        ],
        "stats": {
            "size": cluster.stats.size,
            "maxSimilarity": cluster.stats.max_similarity,
            "minSimilarity": cluster.stats.min_similarity,
            "avgSimilarity": cluster.stats.avg_similarity,
        },
    }
