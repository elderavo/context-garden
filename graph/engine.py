"""KnowledgeGraphEngine — VectorStoreIndex + graph store lifecycle management.

Owns the vector index, embedding model, graph store, and persistence directory.
Called by the JSON-RPC server handlers.

This engine is fail-fast: embedding/provider initialization errors are surfaced
as startup failures instead of running in a partial mode.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Callable, Optional

from llama_index.core import VectorStoreIndex, Settings
from llama_index.core.graph_stores import SimplePropertyGraphStore
from llama_index.core.schema import TextNode

from .indexer import _scan_md_db, _build_graph_store, _build_suffix_index
from .keyword_retriever import KeywordRetriever
from .markdown_utils import normalize_token, IGNORED_DIRS
from .models import ParsedNote, RetrievedNote
from .providers import get_embed_config, check_reachable, create_embedding

log = logging.getLogger(__name__)


def _content_hash(text: str) -> str:
    """Fast MD5 hash of note body text for change detection."""
    return hashlib.md5(text.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# File manifest — mtime/size snapshot for fast boot-time change detection
# ---------------------------------------------------------------------------

_MANIFEST_VERSION = 1
_NOTES_CACHE_VERSION = 1


def _scan_workspace_mtimes(md_db_path: str) -> dict[str, dict]:
    """Walk workspace with os.scandir(), returning {rel_path: {mtime, size}}.

    Uses DirEntry.stat() which on Windows returns cached attributes from
    FindNextFile — no extra syscall per file, no content reads.
    """
    result: dict[str, dict] = {}

    def _walk(dir_path: str) -> None:
        try:
            with os.scandir(dir_path) as it:
                for entry in it:
                    if entry.is_dir(follow_symlinks=False):
                        if entry.name not in IGNORED_DIRS:
                            _walk(entry.path)
                    elif entry.name.endswith(".md"):
                        try:
                            st = entry.stat()
                            rel = os.path.relpath(entry.path, md_db_path).replace("\\", "/")
                            result[rel] = {"mtime": st.st_mtime, "size": st.st_size}
                        except OSError:
                            pass
        except OSError:
            pass

    _walk(md_db_path)
    return result


def _diff_manifest(
    current: dict[str, dict],
    manifest: dict[str, dict],
) -> tuple[list[str], list[str]]:
    """Compare current workspace scan against stored manifest.

    Returns (changed_paths, deleted_paths) as lists of relative paths.
    changed_paths = new files + files whose mtime or size changed.
    deleted_paths = files in manifest that no longer exist.
    """
    changed: list[str] = []
    deleted: list[str] = []

    for rel, info in current.items():
        prev = manifest.get(rel)
        if prev is None or prev["mtime"] != info["mtime"] or prev["size"] != info["size"]:
            changed.append(rel)

    manifest_set = set(manifest.keys())
    current_set = set(current.keys())
    deleted = list(manifest_set - current_set)

    return changed, deleted


def _load_manifest(db_dir: str) -> dict[str, dict]:
    """Load persisted file manifest. Returns empty dict if missing/corrupt."""
    path = os.path.join(db_dir, "_file_manifest.json")
    try:
        data = json.loads(Path(path).read_text("utf-8"))
        if data.get("v") == _MANIFEST_VERSION:
            return data.get("files", {})
    except Exception:
        pass
    return {}


def _save_manifest(db_dir: str, files: dict[str, dict]) -> None:
    """Persist file manifest atomically."""
    path = os.path.join(db_dir, "_file_manifest.json")
    tmp = path + ".tmp"
    try:
        Path(tmp).write_text(json.dumps({"v": _MANIFEST_VERSION, "files": files}), "utf-8")
        os.replace(tmp, path)
    except Exception as exc:
        log.warning("Failed to save file manifest: %s", exc)


def _load_notes_cache(db_dir: str) -> dict[str, ParsedNote]:
    """Load persisted parsed notes. Returns empty dict if missing/corrupt."""
    path = os.path.join(db_dir, "_notes_cache.json")
    try:
        data = json.loads(Path(path).read_text("utf-8"))
        if data.get("v") != _NOTES_CACHE_VERSION:
            return {}
        result: dict[str, ParsedNote] = {}
        for rel, d in data.get("notes", {}).items():
            try:
                result[rel] = ParsedNote(**d)
            except Exception:
                return {}  # schema mismatch — full rebuild
        return result
    except Exception:
        return {}


def _save_notes_cache(db_dir: str, notes_by_id: dict[str, ParsedNote]) -> None:
    """Persist parsed notes cache atomically."""
    path = os.path.join(db_dir, "_notes_cache.json")
    tmp = path + ".tmp"
    try:
        notes_data = {rel: dataclasses.asdict(note) for rel, note in notes_by_id.items()}
        Path(tmp).write_text(json.dumps({"v": _NOTES_CACHE_VERSION, "notes": notes_data}), "utf-8")
        os.replace(tmp, path)
    except Exception as exc:
        log.warning("Failed to save notes cache: %s", exc)


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------


class KnowledgeGraphEngine:
    """Manages VectorStoreIndex + SimplePropertyGraphStore for hybrid retrieval."""

    def __init__(self) -> None:
        self.index: Optional[VectorStoreIndex] = None
        self.embed_model: Optional[Any] = None
        self.graph_store: Optional[SimplePropertyGraphStore] = None
        self.keyword_retriever: Optional[KeywordRetriever] = None

        # Config (set during initialize)
        self.md_db_path: str = ""
        self.db_dir: str = ""
        self.top_k: int = 8

        # In-memory note cache: {relative_path: stripped_body}
        self.note_cache: dict[str, str] = {}

        # Parsed notes for retrieval metadata
        self._parsed_notes: dict[str, ParsedNote] = {}

        # Per-note content hash for incremental updates: {note_id: md5_hex}
        self._note_hashes: dict[str, str] = {}

    # ------------------------------------------------------------------
    # Initialize
    # ------------------------------------------------------------------

    def initialize(
        self,
        md_db_path: str,
        db_dir: str,
        top_k: int = 8,
        **kwargs: Any,
    ) -> dict[str, Any]:
        """Boot the engine: scan notes, build graph, and build/load vector index.

        Fast path (existing index + manifest): O(scandir) + load cached notes +
        load vector index + incremental_update for changed files only.

        Slow path (first boot or no index): full scan + embed + persist.

        Keyword args (backward compat):
            ollama_host: str — maps to embed config host override
            embed_model: str — maps to embed config model override

        Returns init metadata for the TS bridge.
        """
        t0 = time.time()
        self.md_db_path = md_db_path
        self.db_dir = db_dir
        self.top_k = top_k

        # Ensure persistence dir exists
        os.makedirs(db_dir, exist_ok=True)

        # ── Resolve embed config (needed for both paths) ─────────────────
        embed_config = get_embed_config()
        if "ollama_host" in kwargs and kwargs["ollama_host"]:
            embed_config["host"] = kwargs["ollama_host"]
        if "embed_model" in kwargs and kwargs["embed_model"]:
            embed_config["model"] = kwargs["embed_model"]

        self._embed_context_length: int = embed_config.get("context_length", 8192)

        embed_model_obj = create_embedding(embed_config)
        Settings.llm = None

        if embed_model_obj is None:
            provider = embed_config.get("provider", "unknown")
            raise RuntimeError(
                f"Embedding provider '{provider}' is not supported in strict mode; "
                "configure a reachable provider with embeddings enabled."
            )

        if not check_reachable(embed_config):
            host = embed_config.get("host", "")
            provider = embed_config.get("provider", "unknown")
            raise RuntimeError(f"Embedding provider '{provider}' unreachable at {host}")

        self.embed_model = embed_model_obj
        Settings.embed_model = embed_model_obj

        # ── Choose fast or slow path ──────────────────────────────────────
        manifest = _load_manifest(db_dir)
        notes_cache = _load_notes_cache(db_dir)
        has_index = self._has_persisted_index()

        if has_index and manifest and notes_cache:
            result = self._initialize_fast(embed_model_obj, manifest, notes_cache, t0)
        else:
            result = self._initialize_slow(embed_model_obj, t0)

        return result

    def _initialize_slow(self, embed_model_obj: Any, t0: float) -> dict[str, Any]:
        """Full rebuild: scan all files, embed all notes, persist everything."""
        md_db_path = self.md_db_path
        db_dir = self.db_dir

        # Scan + parse all notes
        notes = _scan_md_db(md_db_path)
        notes_by_id = {n.note_id: n for n in notes}
        self._parsed_notes = notes_by_id

        # Build graph store
        suffix_index = _build_suffix_index(notes_by_id)
        self.graph_store = _build_graph_store(notes, notes_by_id, suffix_index)
        self.graph_store.persist(
            persist_path=os.path.join(db_dir, "property_graph_store.json")
        )

        # Keyword retriever
        self.keyword_retriever = KeywordRetriever(notes_by_id)

        # Note cache + hashes
        self.note_cache = {n.note_id: n.body for n in notes}
        self._note_hashes = {n.note_id: _content_hash(n.body) for n in notes}

        # Build vector index (full embed)
        from .indexer import _build_vector_index
        self.index = _build_vector_index(notes, embed_model_obj, db_dir, self._embed_context_length)

        # Persist manifest + notes cache so next boot takes the fast path
        current_mtimes = _scan_workspace_mtimes(md_db_path)
        _save_manifest(db_dir, current_mtimes)
        _save_notes_cache(db_dir, notes_by_id)

        duration_ms = int((time.time() - t0) * 1000)
        log.info("Initialized (slow/full) in %dms — %d notes", duration_ms, len(notes))
        return {
            "path": "slow",
            "mode": "full",
            "duration_ms": duration_ms,
            "note_count": len(notes),
            "note_cache": self.note_cache,
        }

    def _initialize_fast(
        self,
        embed_model_obj: Any,
        manifest: dict[str, dict],
        notes_cache: dict[str, ParsedNote],
        t0: float,
    ) -> dict[str, Any]:
        """Fast path: scandir → diff → load index → incremental_update for changed files.

        No file content is read for unchanged files. Only changed/new files are
        re-parsed and re-embedded.
        """
        md_db_path = self.md_db_path
        db_dir = self.db_dir

        # Step 1: scandir (mtime/size only — no content reads)
        t_scan = time.time()
        current_mtimes = _scan_workspace_mtimes(md_db_path)
        changed_paths, deleted_paths = _diff_manifest(current_mtimes, manifest)
        log.info(
            "Boot diff in %.0fms: %d changed, %d deleted of %d files",
            (time.time() - t_scan) * 1000,
            len(changed_paths), len(deleted_paths), len(current_mtimes),
        )

        # Step 2: Populate engine state from notes cache (no file reads)
        self._parsed_notes = notes_cache
        self.note_cache = {rel: note.body for rel, note in notes_cache.items()}
        self._note_hashes = {rel: _content_hash(note.body) for rel, note in notes_cache.items()}

        # Step 3: Load persisted vector index
        self.index = self._load_vector_index_only(embed_model_obj)
        log.info("Loaded vector index from disk")

        # Step 4: Apply catch-up changes (files that changed while daemon was off).
        # incremental_update rebuilds graph store + keyword retriever internally,
        # so we defer those until after the update to avoid building them twice.
        if changed_paths or deleted_paths:
            log.info(
                "Applying %d changed + %d deleted files from last shutdown",
                len(changed_paths), len(deleted_paths),
            )
            # Stub graph store so incremental_update can proceed (it will rebuild it)
            self.graph_store = SimplePropertyGraphStore()
            self.keyword_retriever = KeywordRetriever(self._parsed_notes)
            self.incremental_update(
                changed_paths=changed_paths,
                deleted_paths=deleted_paths,
                progress_cb=None,
            )
            # incremental_update already saved manifest + notes cache
        else:
            # No catch-up needed — build graph store from cached notes
            all_notes = list(self._parsed_notes.values())
            suffix_index = _build_suffix_index(self._parsed_notes)
            self.graph_store = _build_graph_store(all_notes, self._parsed_notes, suffix_index)
            self.keyword_retriever = KeywordRetriever(self._parsed_notes)
            # Refresh manifest so mtime drift (touch without edit) doesn't re-read
            # those files on the next boot.
            _save_manifest(db_dir, current_mtimes)

        duration_ms = int((time.time() - t0) * 1000)
        changed_count = len(changed_paths) + len(deleted_paths)
        log.info(
            "Initialized (fast) in %dms — %d cached + %d updated notes",
            duration_ms, len(self._parsed_notes), changed_count,
        )
        return {
            "path": "fast",
            "mode": "full",
            "duration_ms": duration_ms,
            "note_count": len(self._parsed_notes),
            "note_cache": self.note_cache,
        }

    # ------------------------------------------------------------------
    # Retrieve
    # ------------------------------------------------------------------

    def retrieve(self, query: str, top_k: Optional[int] = None, workspace: Optional[str] = None) -> dict[str, Any]:
        """Run hybrid retrieval: vector seeds + graph expansion, with keyword fallback.

        If workspace is set, only notes from that workspace are returned.
        """
        k = top_k or self.top_k
        seed_notes: list[RetrievedNote] = []
        expanded_notes: list[RetrievedNote] = []

        # Try vector retrieval first
        if self.index is not None:
            try:
                from .retriever import ObsidiClawRetriever, WorkspaceScopeViolationError

                retriever = ObsidiClawRetriever(
                    index=self.index,
                    graph_store=self.graph_store,
                    embed_model=self.embed_model,
                    parsed_notes=self._parsed_notes,
                    similarity_top_k=k,
                )
                seed_notes, expanded_notes = retriever.retrieve(query, workspace=workspace)
            except WorkspaceScopeViolationError as exc:
                log.error("Vector retrieval violated workspace scope, falling back to keyword: %s", exc)
                seed_notes = []
                expanded_notes = []
            except Exception as exc:
                log.warning("Vector retrieval failed, falling back to keyword: %s", exc)
                seed_notes = []
                expanded_notes = []

        # Fallback to keyword retrieval if vector produced no results
        if not seed_notes and self.keyword_retriever is not None:
            seed_notes = self.keyword_retriever.retrieve(
                query,
                top_k=k,
                workspace=workspace,
            )
            expanded_notes = self._expand_via_graph(
                seed_notes,
                query=query,
                workspace=workspace,
            )
            # No query embedding available in keyword-only path — triple scoring skipped

        return {
            "seed_notes": [self._note_to_dict(n) for n in seed_notes],
            "expanded_notes": [self._note_to_dict(n) for n in expanded_notes],
        }

    # ------------------------------------------------------------------
    # Graph expansion (reusable)
    # ------------------------------------------------------------------

    def _expand_via_graph(
        self,
        seed_notes: list[RetrievedNote],
        query: str,
        workspace: Optional[str] = None,
    ) -> list[RetrievedNote]:
        """Expand seed notes via tier-aware graph expansion shared with retriever."""
        from .retriever import expand_graph_neighbors

        query_tokens = {
            normalize_token(w)
            for w in query.lower().split()
            if normalize_token(w)
        }

        return expand_graph_neighbors(
            graph_store=self.graph_store,
            parsed_notes=self._parsed_notes,
            seed_notes=seed_notes,
            query_tokens=query_tokens,
            workspace=workspace,
        )

    # ------------------------------------------------------------------
    # Note content
    # ------------------------------------------------------------------

    def get_note_content(self, relative_path: str) -> Optional[str]:
        """Get cached note body by relative path."""
        return self.note_cache.get(relative_path)

    # ------------------------------------------------------------------
    # Reindex
    # ------------------------------------------------------------------

    def reindex(self) -> dict[str, Any]:
        """Diff workspace against manifest and apply any changes.

        Replaces the old full-content-hash approach: uses mtime/size manifest
        for O(scandir) change detection, then incremental_update for changed files.
        Falls back to a full rebuild if the engine has no in-memory state.
        """
        if not self.md_db_path:
            raise RuntimeError("Engine not initialized")

        t0 = time.time()

        if not self._parsed_notes:
            # No in-memory state — treat as first boot
            return self._initialize_slow(self.embed_model, t0)

        # Diff workspace against persisted manifest
        manifest = _load_manifest(self.db_dir)
        current_mtimes = _scan_workspace_mtimes(self.md_db_path)
        changed_paths, deleted_paths = _diff_manifest(current_mtimes, manifest)

        if not changed_paths and not deleted_paths:
            return {
                "skipped": True,
                "duration_ms": int((time.time() - t0) * 1000),
                "note_count": len(self._parsed_notes),
                "note_cache": self.note_cache,
            }

        result = self.incremental_update(
            changed_paths=changed_paths,
            deleted_paths=deleted_paths,
            progress_cb=None,
        )
        result["skipped"] = False
        return result

    # ------------------------------------------------------------------
    # Incremental update
    # ------------------------------------------------------------------

    def incremental_update(
        self,
        changed_paths: list[str],
        deleted_paths: list[str],
        progress_cb: Optional[Callable[[int, int], None]] = None,
    ) -> dict[str, Any]:
        """Update only the notes that changed/were added/deleted.

        changed_paths: relative paths within md_db of files that were
                       added or modified (re-parsed and re-embedded).
        deleted_paths: relative paths of files that were removed.

        Returns update metadata for the TS bridge.
        """
        if not self.md_db_path:
            raise RuntimeError("Engine not initialized")

        t0 = time.time()
        added = 0
        updated = 0
        removed = 0

        # ── Handle deletions ─────────────────────────────────────────────
        for rel_path in deleted_paths:
            if rel_path not in self._parsed_notes:
                continue
            self._remove_note(rel_path)
            removed += 1

        # ── Handle additions/modifications ───────────────────────────────
        from .markdown_utils import (
            extract_parent_file,
            extract_parent_module,
            extract_symbol_kind,
            extract_tags,
            extract_tier,
            extract_title,
            extract_workspace,
            extract_wikilinks,
            infer_note_type,
            parse_frontmatter,
        )

        new_notes: list[ParsedNote] = []
        total_changed = len(changed_paths)
        for _idx, rel_path in enumerate(changed_paths):
            abs_path = os.path.join(self.md_db_path, rel_path.replace("/", os.sep))
            if not os.path.isfile(abs_path):
                # File listed as changed but doesn't exist — treat as delete
                if rel_path in self._parsed_notes:
                    self._remove_note(rel_path)
                    removed += 1
                if progress_cb:
                    progress_cb(_idx + 1, total_changed)
                continue

            try:
                content = Path(abs_path).read_text(encoding="utf-8")
            except Exception as exc:
                log.warning("Failed to read %s: %s", abs_path, exc)
                if progress_cb:
                    progress_cb(_idx + 1, total_changed)
                continue

            frontmatter, body = parse_frontmatter(content)
            new_hash = _content_hash(body)

            # Skip if content hasn't actually changed
            if rel_path in self._note_hashes and self._note_hashes[rel_path] == new_hash:
                if progress_cb:
                    progress_cb(_idx + 1, total_changed)
                continue

            note_type = infer_note_type(frontmatter, rel_path)
            title = extract_title(frontmatter, body, rel_path)
            tags = extract_tags(frontmatter)
            links = extract_wikilinks(body)

            tool_id: Optional[str] = None
            if note_type == "tool":
                tool_id = str(frontmatter.get("tool_id") or Path(rel_path).stem)

            note = ParsedNote(
                note_id=rel_path,
                path=rel_path,
                title=title,
                note_type=note_type,  # type: ignore[arg-type]
                body=body,
                frontmatter=frontmatter,
                links_out=links,
                tool_id=tool_id,
                time_created=None,
                last_edited=None,
                tags=tags,
                tier=extract_tier(frontmatter),
                symbol_kind=extract_symbol_kind(frontmatter),
                parent_file=extract_parent_file(frontmatter),
                parent_module=extract_parent_module(frontmatter),
                workspace=extract_workspace(frontmatter),
            )

            is_new = rel_path not in self._parsed_notes
            new_notes.append(note)

            # Update in-memory state
            self._parsed_notes[rel_path] = note
            self.note_cache[rel_path] = body
            self._note_hashes[rel_path] = new_hash

            # Update vector index (with chunking for long notes)
            if self.index is not None:
                from .indexer import _chunk_text
                full_text = f"{title}\n\n{body}"
                chunk_char_limit = self._embed_context_length * 3
                metadata_base = {
                    "file_path": rel_path,
                    "note_type": note_type,
                    "title": title,
                    "tool_id": tool_id or "",
                    "tags": ",".join(tags),
                    "workspace": note.workspace,
                }

                if not is_new:
                    # Delete old nodes (including any chunks)
                    try:
                        self.index.delete_ref_doc(rel_path, delete_from_docstore=True)
                    except Exception:
                        pass  # May not exist in docstore

                if len(full_text) <= chunk_char_limit:
                    nodes = [TextNode(text=full_text, id_=rel_path, metadata=metadata_base)]
                else:
                    chunks = _chunk_text(full_text, chunk_char_limit)
                    nodes = [
                        TextNode(
                            text=chunk,
                            id_=f"{rel_path}#chunk{i}",
                            metadata={**metadata_base, "parent_note_id": rel_path, "chunk_index": i},
                        )
                        for i, chunk in enumerate(chunks)
                    ]

                self.index.insert_nodes(nodes)
                if is_new:
                    added += 1
                else:
                    updated += 1
            else:
                if is_new:
                    added += 1
                else:
                    updated += 1

            if progress_cb:
                progress_cb(_idx + 1, total_changed)

        # ── Rebuild graph store + keyword index ──────────────────────────
        if added + updated + removed > 0:
            all_notes = list(self._parsed_notes.values())
            suffix_index = _build_suffix_index(self._parsed_notes)
            self.graph_store = _build_graph_store(all_notes, self._parsed_notes, suffix_index)
            self.graph_store.persist(
                persist_path=os.path.join(self.db_dir, "property_graph_store.json")
            )
            self.keyword_retriever = KeywordRetriever(self._parsed_notes)

            # Persist vector index
            if self.index is not None:
                self.index.storage_context.persist(persist_dir=self.db_dir)

            # Update manifest and notes cache so next boot stays on the fast path
            current_mtimes = _scan_workspace_mtimes(self.md_db_path)
            _save_manifest(self.db_dir, current_mtimes)
            _save_notes_cache(self.db_dir, self._parsed_notes)

        duration_ms = int((time.time() - t0) * 1000)
        total = added + updated + removed
        log.info(
            "Incremental update: +%d ~%d -%d (%d total) in %dms",
            added, updated, removed, total, duration_ms,
        )

        return {
            "added": added,
            "updated": updated,
            "removed": removed,
            "duration_ms": duration_ms,
            "note_count": len(self._parsed_notes),
            "note_cache": self.note_cache,
        }

    def _remove_note(self, note_id: str) -> None:
        """Remove a single note from all in-memory structures."""
        self._parsed_notes.pop(note_id, None)
        self.note_cache.pop(note_id, None)
        self._note_hashes.pop(note_id, None)

        # Remove from vector index
        if self.index is not None:
            try:
                self.index.delete_ref_doc(note_id, delete_from_docstore=True)
            except Exception as exc:
                log.warning("Failed to delete %s from vector index: %s", note_id, exc)

    # ------------------------------------------------------------------
    # Graph path retrieval
    # ------------------------------------------------------------------

    def find_path(
        self,
        start_query: str,
        end_query: str,
        edge_types: Optional[list[str]] = None,
        max_depth: int = 8,
    ) -> dict[str, Any]:
        """Find the shortest graph path between two endpoints.

        Each endpoint is resolved: exact note_id match → vector similarity
        (top-1) → keyword fallback. Returns path steps with edge labels
        and the notes along the path.
        """
        if not self.md_db_path:
            raise RuntimeError("Engine not initialized")
        if not self.graph_store:
            raise RuntimeError("Graph store not available")

        t0 = time.time()
        allowed = set(edge_types) if edge_types else None

        # ── Resolve endpoints ─────────────────────────────────────────────
        start_id, start_method = self._resolve_endpoint(start_query)
        end_id, end_method = self._resolve_endpoint(end_query)

        if start_id is None or end_id is None:
            return {
                "start_id": start_id or "",
                "end_id": end_id or "",
                "start_resolved_by": start_method,
                "end_resolved_by": end_method,
                "path_length": 0,
                "path_steps": [],
                "path_notes": [],
                "no_path": True,
                "duration_ms": int((time.time() - t0) * 1000),
            }

        # ── BFS shortest path ─────────────────────────────────────────────
        from .pathfinder import find_shortest_path

        steps = find_shortest_path(
            self.graph_store, start_id, end_id,
            allowed_edge_types=allowed,
            max_depth=max_depth,
        )

        if steps is None:
            return {
                "start_id": start_id,
                "end_id": end_id,
                "start_resolved_by": start_method,
                "end_resolved_by": end_method,
                "path_length": 0,
                "path_steps": [],
                "path_notes": [],
                "no_path": True,
                "duration_ms": int((time.time() - t0) * 1000),
            }

        # ── Collect path notes ────────────────────────────────────────────
        path_length = len(steps) - 1  # hops, not nodes
        path_steps_dicts: list[dict[str, str]] = []
        path_notes: list[dict[str, Any]] = []

        for i, step in enumerate(steps):
            path_steps_dicts.append({
                "nodeId": step.node_id,
                "edgeLabel": step.edge_label,
                "edgeDirection": step.edge_direction,
                "fromNodeId": step.from_node_id,
            })

            parsed = self._parsed_notes.get(step.node_id)
            if parsed is None:
                continue

            # Score: 1.0 at endpoints, decaying toward middle
            if path_length == 0:
                score = 1.0
            else:
                dist_from_end = min(i, path_length - i)
                score = max(0.5, 1.0 - (dist_from_end / path_length) * 0.5)

            note = RetrievedNote(
                note_id=parsed.note_id,
                path=parsed.path,
                content=parsed.body,
                score=score,
                type=parsed.note_type,
                retrieval_source="path",
                tool_id=parsed.tool_id,
                tags=parsed.tags,
                linked_from=[step.from_node_id] if step.from_node_id else None,
                depth=i,
                tier=parsed.tier,
                workspace=parsed.workspace,
                title=parsed.title,
            )
            path_notes.append(self._note_to_dict(note))

        return {
            "start_id": start_id,
            "end_id": end_id,
            "start_resolved_by": start_method,
            "end_resolved_by": end_method,
            "path_length": path_length,
            "path_steps": path_steps_dicts,
            "path_notes": path_notes,
            "no_path": False,
            "duration_ms": int((time.time() - t0) * 1000),
        }

    def _resolve_endpoint(self, query: str) -> tuple[Optional[str], str]:
        """Resolve a fuzzy query to a note_id.

        Cascade: exact match → vector similarity (top-1) → keyword fallback.
        Returns (note_id_or_None, resolution_method).
        """
        # 1. Exact match on note_id
        if query in self._parsed_notes:
            return query, "exact"

        # 2. Partial path match (e.g. "context-engine.ts" matches
        #    "code/obsidi-claw/knowledge/engine/context-engine.ts.md")
        query_lower = query.lower()
        for note_id in self._parsed_notes:
            if query_lower in note_id.lower():
                return note_id, "exact"

        # 3. Vector similarity (top-1)
        if self.index is not None:
            try:
                retriever = self.index.as_retriever(similarity_top_k=1)
                results = retriever.retrieve(query)
                if results:
                    file_path = results[0].metadata.get("file_path", "")
                    if file_path and file_path in self._parsed_notes:
                        return file_path, "vector"
            except Exception as exc:
                log.warning("Vector endpoint resolution failed for '%s': %s", query, exc)

        # 4. Keyword fallback
        if self.keyword_retriever is not None:
            kw_results = self.keyword_retriever.retrieve(query, top_k=1)
            if kw_results:
                return kw_results[0].note_id, "keyword"

        return None, "none"

    # ------------------------------------------------------------------
    # Graph stats
    # ------------------------------------------------------------------

    def get_graph_stats(self) -> dict[str, Any]:
        """Return basic graph statistics."""
        if self.graph_store is None:
            return {"note_count": 0, "edge_count": 0, "index_loaded": False}

        # Count edges from the internal graph data
        edge_count = 0
        if hasattr(self.graph_store, 'graph') and hasattr(self.graph_store.graph, 'relations'):
            for rels in self.graph_store.graph.relations.values():
                edge_count += len(rels) if isinstance(rels, list) else 1

        return {
            "note_count": len(self._parsed_notes),
            "edge_count": edge_count,
            "index_loaded": self.index is not None,
        }

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _has_persisted_index(self) -> bool:
        """Check if persisted index files exist on disk."""
        return (
            os.path.exists(os.path.join(self.db_dir, "property_graph_store.json"))
            and os.path.exists(os.path.join(self.db_dir, "docstore.json"))
        )

    def _load_vector_index_only(self, embed_model: Any) -> VectorStoreIndex:
        """Load persisted VectorStoreIndex from disk."""
        from llama_index.core import StorageContext, load_index_from_storage

        storage_context = StorageContext.from_defaults(persist_dir=self.db_dir)
        return load_index_from_storage(
            storage_context=storage_context,
            embed_model=embed_model,
        )

    @staticmethod
    def _note_to_dict(note: RetrievedNote) -> dict[str, Any]:
        return {
            "noteId": note.note_id,
            "path": note.path,
            "content": note.content,
            "score": note.score,
            "type": note.type,
            "toolId": note.tool_id,
            "tags": note.tags,
            "retrievalSource": note.retrieval_source,
            "linkedFrom": note.linked_from,
            "depth": note.depth,
            "workspace": note.workspace,
            "title": note.title,
            "viaEdge": note.via_edge or None,
            "viaSourceTitle": note.via_source_title or None,
        }
