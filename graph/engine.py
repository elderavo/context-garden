"""KnowledgeGraphEngine — VectorStoreIndex + graph store lifecycle management.

Owns the vector index, embedding model, graph store, and persistence directory.
Called by the JSON-RPC server handlers.

Supports graceful degradation:
  - "full" mode: vector embeddings + graph + keyword (all available)
  - "degraded" mode: graph + keyword only (embedding provider unavailable or set to "local")
"""

from __future__ import annotations

import hashlib
import logging
import os
import time
from pathlib import Path
from typing import Any, Callable, Optional

from llama_index.core import VectorStoreIndex, Settings
from llama_index.core.graph_stores import SimplePropertyGraphStore
from llama_index.core.schema import TextNode, Document

from .indexer import build_index, load_index, _scan_md_db, _build_graph_store
from .keyword_retriever import KeywordRetriever
from .markdown_utils import compute_md_db_hash, normalize_token
from .models import ParsedNote, RetrievedNote
from .providers import get_embed_config, check_reachable, create_embedding

log = logging.getLogger(__name__)


def _content_hash(text: str) -> str:
    """Fast MD5 hash of note body text for change detection."""
    return hashlib.md5(text.encode("utf-8")).hexdigest()


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

        # Mode tracking
        self._mode: str = "degraded"  # "full" | "degraded"
        self._degraded_reason: str = ""

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
        """Boot the engine: scan notes, build graph, optionally build vector index.

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

        # ── Step 1: Scan + parse all notes (always — no provider needed) ─
        notes = _scan_md_db(md_db_path)
        notes_by_id = {n.note_id: n for n in notes}
        self._parsed_notes = notes_by_id

        # ── Step 2: Build graph store (always — no provider needed) ──────
        self.graph_store = _build_graph_store(notes, notes_by_id)
        # Persist graph store
        self.graph_store.persist(
            persist_path=os.path.join(db_dir, "property_graph_store.json")
        )

        # ── Step 3: Build keyword retriever (always) ────────────────────
        self.keyword_retriever = KeywordRetriever(notes_by_id)

        # ── Step 4: Build note cache + per-note hashes (always) ──────────
        self.note_cache = {n.note_id: n.body for n in notes}
        self._note_hashes = {n.note_id: _content_hash(n.body) for n in notes}

        # ── Step 5: Resolve embed config ────────────────────────────────
        embed_config = get_embed_config()

        # Backward compat: TS may pass ollama_host/embed_model via kwargs
        if "ollama_host" in kwargs and kwargs["ollama_host"]:
            embed_config["host"] = kwargs["ollama_host"]
        if "embed_model" in kwargs and kwargs["embed_model"]:
            embed_config["model"] = kwargs["embed_model"]

        # Store context_length for chunking in vector index builds
        self._embed_context_length: int = embed_config.get("context_length", 8192)

        # ── Step 6: Create embedding model ──────────────────────────────
        embed_model_obj = create_embedding(embed_config)

        # Disable LlamaIndex's default OpenAI LLM — we don't use it
        Settings.llm = None

        if embed_model_obj is None:
            # provider="local" — no vector index
            self._mode = "degraded"
            self._degraded_reason = "Embedding provider set to 'local'"
            self.embed_model = None
            self.index = None
            log.info("Initialized in degraded mode: %s", self._degraded_reason)

        elif check_reachable(embed_config):
            # Provider available — build or load vector index
            self.embed_model = embed_model_obj
            Settings.embed_model = embed_model_obj
            self._build_or_load_vector_index(notes, notes_by_id, embed_model_obj)
            self._mode = "full"
            self._degraded_reason = ""

        else:
            # Provider configured but unreachable
            host = embed_config.get("host", "")
            self.embed_model = embed_model_obj

            if self._has_persisted_index():
                # Try loading cached index (doesn't hit provider until query time)
                try:
                    self.index = self._load_vector_index_only(embed_model_obj)
                    Settings.embed_model = embed_model_obj
                    self._mode = "degraded"
                    self._degraded_reason = f"Embedding provider unreachable at {host}; loaded cached index"
                    log.info("Loaded cached vector index despite unreachable provider")
                except Exception as exc:
                    log.warning("Failed to load cached vector index: %s", exc)
                    self.index = None
                    self._mode = "degraded"
                    self._degraded_reason = f"Embedding provider unreachable at {host}"
            else:
                self.index = None
                self._mode = "degraded"
                self._degraded_reason = f"Embedding provider unreachable at {host}"

            log.info("Initialized in degraded mode: %s", self._degraded_reason)

        duration_ms = int((time.time() - t0) * 1000)
        path_type = "full" if self._mode == "full" else "degraded"
        log.info(
            "Initialized (%s) in %dms — %d notes",
            path_type,
            duration_ms,
            len(notes),
        )

        return {
            "path": path_type,
            "mode": self._mode,
            "degraded_reason": self._degraded_reason,
            "duration_ms": duration_ms,
            "note_count": len(notes),
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
        """Re-scan md_db and rebuild if changed."""
        if not self.md_db_path:
            raise RuntimeError("Engine not initialized")

        t0 = time.time()
        current_hash = compute_md_db_hash(self.md_db_path)
        hash_path = os.path.join(self.db_dir, ".md_db_hash")
        stored_hash = ""
        if os.path.exists(hash_path):
            stored_hash = Path(hash_path).read_text().strip()

        # Skip only if hash matches AND in-memory state is populated.
        # After unregister+reregister of the same workspace, the hash may
        # match but _parsed_notes was cleared — must rebuild in that case.
        if current_hash == stored_hash and len(self._parsed_notes) > 0:
            return {
                "skipped": True,
                "duration_ms": int((time.time() - t0) * 1000),
                "note_count": len(self.note_cache),
                "note_cache": self.note_cache,
            }

        # Always re-scan notes and rebuild graph + keyword retriever
        notes = _scan_md_db(self.md_db_path)
        notes_by_id = {n.note_id: n for n in notes}
        self._parsed_notes = notes_by_id
        self.graph_store = _build_graph_store(notes, notes_by_id)
        self.graph_store.persist(
            persist_path=os.path.join(self.db_dir, "property_graph_store.json")
        )
        self.keyword_retriever = KeywordRetriever(notes_by_id)
        self.note_cache = {n.note_id: n.body for n in notes}

        # Rebuild vector index if we have an embedding model
        if self.embed_model is not None:
            try:
                from .indexer import _build_vector_index
                self.index = _build_vector_index(notes, self.embed_model, self.db_dir, self._embed_context_length)
                self._mode = "full"
                self._degraded_reason = ""
            except Exception as exc:
                log.warning("Vector index rebuild failed: %s", exc)
                self.index = None
                self._mode = "degraded"
                self._degraded_reason = f"Vector index rebuild failed: {exc}"

        # Update hash
        Path(hash_path).write_text(current_hash)

        duration_ms = int((time.time() - t0) * 1000)
        return {
            "skipped": False,
            "duration_ms": duration_ms,
            "note_count": len(notes),
            "note_cache": self.note_cache,
        }

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

        # ── Rebuild graph store (cheap — no embeddings) ──────────────────
        # Graph edges depend on cross-note wikilinks, so after any note
        # changes we rebuild the full graph. This is fast (~10ms for 70
        # notes) since it's just in-memory data structure manipulation.
        if added + updated + removed > 0:
            all_notes = list(self._parsed_notes.values())
            self.graph_store = _build_graph_store(all_notes, self._parsed_notes)
            self.graph_store.persist(
                persist_path=os.path.join(self.db_dir, "property_graph_store.json")
            )
            self.keyword_retriever = KeywordRetriever(self._parsed_notes)

            # Persist vector index if it exists
            if self.index is not None:
                self.index.storage_context.persist(persist_dir=self.db_dir)

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

        # Invalidate persisted hash so next reindex() doesn't skip
        hash_path = os.path.join(self.db_dir, ".md_db_hash")
        try:
            if os.path.exists(hash_path):
                os.remove(hash_path)
        except OSError:
            pass

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

    def _build_or_load_vector_index(
        self,
        notes: list[ParsedNote],
        notes_by_id: dict[str, ParsedNote],
        embed_model: Any,
    ) -> None:
        """Hash-based fast/slow path for vector index."""
        current_hash = compute_md_db_hash(self.md_db_path)
        hash_path = os.path.join(self.db_dir, ".md_db_hash")
        stored_hash = ""
        if os.path.exists(hash_path):
            stored_hash = Path(hash_path).read_text().strip()

        if current_hash == stored_hash and self._has_persisted_index():
            # Fast path: load from disk
            self.index = self._load_vector_index_only(embed_model)
            log.info("Loaded vector index from cache (fast path)")
        else:
            # Slow path: full rebuild
            from .indexer import _build_vector_index
            self.index = _build_vector_index(notes, embed_model, self.db_dir, self._embed_context_length)
            Path(hash_path).write_text(current_hash)
            log.info("Built vector index (slow path)")

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
        }
