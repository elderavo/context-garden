"""Indexer — md_db scanning, note parsing, vector index + graph store construction.

Architecture:
  - VectorStoreIndex: owns text embeddings for semantic search (via embedding provider)
  - SimplePropertyGraphStore: owns wikilink graph (EntityNode + Relation edges)

Both persist to .obsidi-claw/knowledge_graph/.

Ingestion uses markdown_utils (our own frontmatter/wikilink/title/type parser)
rather than LlamaIndex's ObsidianReader, so parsing is fully under our control
and the MD5 hash stays byte-identical across TS and Python.
"""

from __future__ import annotations

import logging
import os
import posixpath
from pathlib import Path
from typing import Any, Optional

from llama_index.core import VectorStoreIndex, StorageContext, load_index_from_storage
from llama_index.core.graph_stores import (
    EntityNode,
    Relation,
    SimplePropertyGraphStore,
)
from llama_index.core.schema import TextNode

from .markdown_utils import (
    collect_markdown_files,
    extract_tags,
    extract_tier,
    extract_parent_file,
    extract_parent_module,
    extract_symbol_kind,
    extract_title,
    extract_workspace,
    extract_wikilinks,
    infer_note_type,
    parse_frontmatter,
)
from .models import NoteType, ParsedNote

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Note label mapping
# ---------------------------------------------------------------------------

_LABEL_MAP: dict[str, str] = {
    "tool": "TOOL",
    "concept": "CONCEPT",
    "index": "INDEX",
    "codebase": "CODEBASE",
    "codeUnit": "CODEUNIT",
    "codeSymbol": "CODESYMBOL",  # tier-1
    "codeModule": "CODEMODULE",  # tier-3
}


# ---------------------------------------------------------------------------
# Scan + parse (via markdown_utils)
# ---------------------------------------------------------------------------


def _scan_md_db(md_db_path: str) -> list[ParsedNote]:
    """Scan md_db directory and parse all .md files into ParsedNote objects."""
    file_paths = collect_markdown_files(md_db_path)
    notes: list[ParsedNote] = []

    for abs_path in file_paths:
        try:
            content = Path(abs_path).read_text(encoding="utf-8")
        except Exception as exc:
            log.warning("Failed to read %s: %s", abs_path, exc)
            continue

        rel_path = os.path.relpath(abs_path, md_db_path).replace("\\", "/")
        frontmatter, body = parse_frontmatter(content)

        note_type = infer_note_type(frontmatter, rel_path)
        title = extract_title(frontmatter, body, rel_path)
        tags = extract_tags(frontmatter)
        links = extract_wikilinks(body)

        tool_id: Optional[str] = None
        if note_type == "tool":
            tool_id = str(frontmatter.get("tool_id") or Path(rel_path).stem)

        time_created = _first_str(
            frontmatter.get("time_created"),
            frontmatter.get("created"),
        )
        last_edited = _first_str(
            frontmatter.get("last_edited"),
            frontmatter.get("updated"),
        )

        notes.append(
            ParsedNote(
                note_id=rel_path,
                path=rel_path,
                title=title,
                note_type=note_type,  # type: ignore[arg-type]
                body=body,
                frontmatter=frontmatter,
                links_out=links,
                tool_id=tool_id,
                time_created=time_created,
                last_edited=last_edited,
                tags=tags,
                tier=extract_tier(frontmatter),
                symbol_kind=extract_symbol_kind(frontmatter),
                parent_file=extract_parent_file(frontmatter),
                parent_module=extract_parent_module(frontmatter),
                workspace=extract_workspace(frontmatter),
            )
        )

    log.info("Scanned %d notes from %s", len(notes), md_db_path)
    return notes


def _first_str(*values: object) -> Optional[str]:
    """Return the first non-empty string value, or None."""
    for v in values:
        if isinstance(v, str) and v.strip():
            return v.strip()
    return None


# ---------------------------------------------------------------------------
# Wikilink resolution
# ---------------------------------------------------------------------------


def _build_suffix_index(notes_by_id: dict[str, ParsedNote]) -> dict[str, list[str]]:
    """Pre-build a suffix → [note_id] lookup for O(1) wikilink resolution.

    Indexes each note by all of its path suffixes (e.g. "foo/bar.md", "bar.md",
    "bar") so that _resolve_link_fast never has to iterate the full note set.
    """
    index: dict[str, list[str]] = {}
    type_priority = {"tool": 0, "concept": 1, "codeUnit": 2, "codeSymbol": 3, "index": 4, "codebase": 5}
    for note_id, note in notes_by_id.items():
        norm = note_id.replace("\\", "/")
        parts = norm.split("/")
        # Register progressively shorter suffixes: full path, then each tail
        for i in range(len(parts)):
            suffix = "/".join(parts[i:])
            index.setdefault(suffix, []).append(note_id)
            # Also without .md extension
            if suffix.endswith(".md"):
                index.setdefault(suffix[:-3], []).append(note_id)
        # Stem-only key (basename without extension, lowercase for case-insensitive match)
        stem = Path(parts[-1]).stem.lower()
        index.setdefault(f"__stem__{stem}", []).append(note_id)

    # Sort each bucket by type priority so the best match is always first
    for key in index:
        if len(index[key]) > 1:
            index[key].sort(key=lambda nid: type_priority.get(notes_by_id[nid].note_type, 99))
    return index


def _resolve_link_fast(
    target: str,
    notes_by_id: dict[str, ParsedNote],
    suffix_index: dict[str, list[str]],
) -> Optional[str]:
    """Resolve a wikilink target to a note_id using the pre-built suffix index.

    O(1) lookups — no iteration over all notes.
    Priority: exact match > suffix/path match > stem match.
    """
    # Exact match
    if target in notes_by_id:
        return target

    norm_target = target.replace("\\", "/")

    # Try with .md
    if not norm_target.endswith(".md"):
        with_md = norm_target + ".md"
        if with_md in notes_by_id:
            return with_md

    # Suffix index lookup (includes path suffixes like "dir/foo.md" and "foo.md")
    candidates = suffix_index.get(norm_target) or suffix_index.get(norm_target + ".md")
    if candidates:
        return candidates[0]  # already sorted by type priority

    # Stem fallback (case-insensitive basename match)
    stem = Path(norm_target).stem.lower()
    stem_candidates = suffix_index.get(f"__stem__{stem}")
    if stem_candidates:
        return stem_candidates[0]

    return None


def _resolve_link(target: str, notes_by_id: dict[str, ParsedNote]) -> Optional[str]:
    """Resolve a wikilink target to a note_id.

    Legacy O(N) version kept for call sites that don't have a suffix index.
    Prefer _resolve_link_fast for bulk graph construction.
    """
    # Exact match
    if target in notes_by_id:
        return target

    # Try with .md
    if not target.endswith(".md"):
        with_md = target + ".md"
        if with_md in notes_by_id:
            return with_md

    # Suffix match
    candidates: list[ParsedNote] = []
    for note in notes_by_id.values():
        norm_path = note.path.replace("\\", "/")
        check_target = target if target.endswith(".md") else target + ".md"
        if norm_path.endswith("/" + check_target) or norm_path == check_target:
            candidates.append(note)

    if not candidates:
        # Try stem match
        target_stem = Path(target).stem.lower()
        for note in notes_by_id.values():
            if Path(note.path).stem.lower() == target_stem:
                candidates.append(note)

    if not candidates:
        return None

    if len(candidates) == 1:
        return candidates[0].note_id

    type_priority = {"tool": 0, "concept": 1, "codeUnit": 2, "index": 3, "codebase": 4}
    candidates.sort(key=lambda n: type_priority.get(n.note_type, 99))
    return candidates[0].note_id


def _module_source_dir(note: ParsedNote) -> str:
    """Return the source directory represented by a tier-3 module note."""
    if note.tier != "3":
        return ""
    raw = str(note.frontmatter.get("path") or "").strip().replace("\\", "/").strip("/")
    return raw or "."


def _is_direct_child_module(parent_dir: str, child_dir: str) -> bool:
    """True when child_dir is exactly one directory below parent_dir."""
    if not parent_dir or not child_dir or parent_dir == child_dir:
        return False
    if parent_dir == ".":
        return "/" not in child_dir and child_dir != "."
    return posixpath.dirname(child_dir) == parent_dir


# ---------------------------------------------------------------------------
# Graph store construction (separated from vector index)
# ---------------------------------------------------------------------------


def _build_graph_store(
    notes: list[ParsedNote],
    notes_by_id: dict[str, ParsedNote],
    suffix_index: Optional[dict[str, list[str]]] = None,
) -> SimplePropertyGraphStore:
    """Build a SimplePropertyGraphStore from parsed notes.

    Creates entity nodes and typed relations. Does NOT require an
    embedding provider — pure graph structure.

    Typed edges for code notes (tiers 1/2/3):
      DEFINED_IN      tier-1 → tier-2 (symbol lives in this file)
      BELONGS_TO      tier-2 → tier-3 (file belongs to this module)
      CONTAINS        tier-3 → tier-2 (module contains this file, inverse)
      CONTAINS_SYMBOL tier-2 → tier-1 (file contains this symbol, inverse)
      CALLS           tier-1 → tier-1 (symbol calls another symbol)
      IMPORTS         tier-2 → tier-2 (file imports another file)

    Non-code notes use LINKS_TO for all wikilinks.
    """
    graph_store = SimplePropertyGraphStore()

    # Build suffix index once upfront for O(1) link resolution
    _sidx = suffix_index if suffix_index is not None else _build_suffix_index(notes_by_id)

    def _resolve(target: str) -> Optional[str]:
        return _resolve_link_fast(target, notes_by_id, _sidx)

    entity_nodes: list[EntityNode] = []
    for note in notes:
        label = _LABEL_MAP.get(note.note_type, "CONCEPT")
        entity = EntityNode(
            name=note.note_id,
            label=label,
            properties={
                "path": note.path,
                "title": note.title,
                "tool_id": note.tool_id or "",
                "tags": ",".join(note.tags),
                "note_type": note.note_type,
                "tier": note.tier,
                "symbol_kind": note.symbol_kind,
                "parent_file": note.parent_file,
                "parent_module": note.parent_module,
                "workspace": note.workspace,
            },
        )
        entity_nodes.append(entity)

    graph_store.upsert_nodes(entity_nodes)

    relations: list[Relation] = []
    for note in notes:
        is_code_note = note.note_type in ("codeSymbol", "codeUnit", "codeModule")

        if is_code_note:
            # --- Structural edges from frontmatter (authoritative, always added) ---

            # Tier-1 (codeSymbol) → tier-2 (codeUnit): DEFINED_IN
            if note.tier == "1" and note.parent_file:
                resolved = _resolve(note.parent_file)
                if resolved and resolved != note.note_id:
                    relations.append(Relation(
                        label="DEFINED_IN",
                        source_id=note.note_id,
                        target_id=resolved,
                    ))
                    # Inverse: tier-2 CONTAINS_SYMBOL tier-1
                    relations.append(Relation(
                        label="CONTAINS_SYMBOL",
                        source_id=resolved,
                        target_id=note.note_id,
                    ))

            # Tier-2 (codeUnit) → tier-3 (codeModule): BELONGS_TO
            if note.tier == "2" and note.parent_module:
                resolved = _resolve(note.parent_module)
                if resolved and resolved != note.note_id:
                    relations.append(Relation(
                        label="BELONGS_TO",
                        source_id=note.note_id,
                        target_id=resolved,
                    ))
                    # Inverse: tier-3 CONTAINS tier-2
                    relations.append(Relation(
                        label="CONTAINS",
                        source_id=resolved,
                        target_id=note.note_id,
                    ))

            # --- Wikilink edges: CALLS (tier-1→tier-1) or IMPORTS (tier-2→tier-2) ---
            for link_target in note.links_out:
                resolved = _resolve(link_target)
                if not resolved or resolved == note.note_id:
                    continue
                target_note = notes_by_id.get(resolved)
                if target_note is None:
                    continue

                if note.tier == "1" and target_note.tier == "1":
                    edge_label = "CALLS"
                elif note.tier == "2" and target_note.tier == "2":
                    edge_label = "IMPORTS"
                elif note.tier == "3" and target_note.tier == "3":
                    # Module notes can link downward from ## Submodules or upward
                    # from ## Parent. Use source directory paths to orient edges.
                    source_dir = _module_source_dir(note)
                    target_dir = _module_source_dir(target_note)
                    if _is_direct_child_module(source_dir, target_dir):
                        relations.append(Relation(
                            label="CONTAINS",
                            source_id=note.note_id,
                            target_id=resolved,
                        ))
                        relations.append(Relation(
                            label="BELONGS_TO",
                            source_id=resolved,
                            target_id=note.note_id,
                        ))
                        continue
                    if _is_direct_child_module(target_dir, source_dir):
                        relations.append(Relation(
                            label="BELONGS_TO",
                            source_id=note.note_id,
                            target_id=resolved,
                        ))
                        relations.append(Relation(
                            label="CONTAINS",
                            source_id=resolved,
                            target_id=note.note_id,
                        ))
                        continue
                    edge_label = "LINKS_TO"
                else:
                    # Cross-tier wikilinks in code notes → generic
                    edge_label = "LINKS_TO"

                relations.append(Relation(
                    label=edge_label,
                    source_id=note.note_id,
                    target_id=resolved,
                ))
        else:
            # Non-code notes: all wikilinks → LINKS_TO
            for link_target in note.links_out:
                resolved = _resolve(link_target)
                if resolved and resolved != note.note_id:
                    relations.append(Relation(
                        label="LINKS_TO",
                        source_id=note.note_id,
                        target_id=resolved,
                    ))

    if relations:
        graph_store.upsert_relations(relations)
    log.info("Graph: %d nodes, %d relations", len(entity_nodes), len(relations))

    return graph_store


# ---------------------------------------------------------------------------
# Vector index construction (requires embedding provider)
# ---------------------------------------------------------------------------


def _chunk_text(text: str, max_chars: int, overlap: int = 200) -> list[str]:
    """Split text into overlapping chunks that fit the embedding context."""
    if len(text) <= max_chars:
        return [text]
    chunks: list[str] = []
    start = 0
    while start < len(text):
        end = start + max_chars
        chunks.append(text[start:end])
        start = end - overlap
    return chunks


def _build_vector_index(
    notes: list[ParsedNote],
    embed_model: Any,
    db_dir: str,
    context_length: int = 8192,
) -> VectorStoreIndex:
    """Build a VectorStoreIndex from parsed notes and persist to disk.

    Requires a working embedding provider (OllamaEmbedding, OpenAIEmbedding, etc).
    Long notes are split into overlapping chunks to fit within the embedding
    model's context window.
    """
    # Conservative chars-to-tokens ratio (most models ~4 chars/token)
    chunk_char_limit = context_length * 3

    text_nodes: list[TextNode] = []
    for note in notes:
        full_text = f"{note.title}\n\n{note.body}"
        metadata = {
            "file_path": note.path,
            "note_type": note.note_type,
            "title": note.title,
            "tool_id": note.tool_id or "",
            "tags": ",".join(note.tags),
            "workspace": note.workspace,
        }

        if len(full_text) <= chunk_char_limit:
            text_nodes.append(TextNode(
                text=full_text,
                id_=note.note_id,
                metadata=metadata,
            ))
        else:
            chunks = _chunk_text(full_text, chunk_char_limit)
            for i, chunk in enumerate(chunks):
                text_nodes.append(TextNode(
                    text=chunk,
                    id_=f"{note.note_id}#chunk{i}",
                    metadata={
                        **metadata,
                        "parent_note_id": note.note_id,
                        "chunk_index": i,
                    },
                ))

    vector_index = VectorStoreIndex(
        nodes=text_nodes,
        embed_model=embed_model,
        show_progress=False,
    )

    # Persist vector index
    vector_index.storage_context.persist(persist_dir=db_dir)
    log.info("Vector index persisted to %s", db_dir)

    return vector_index


# ---------------------------------------------------------------------------
# Combined build (legacy — kept for backward compat)
# ---------------------------------------------------------------------------


def build_index(
    md_db_path: str,
    db_dir: str,
    embed_model: Any,
) -> tuple[VectorStoreIndex, SimplePropertyGraphStore, list[ParsedNote]]:
    """Full rebuild: scan md_db → build VectorStoreIndex + graph store → persist.

    Note: prefer using _scan_md_db, _build_graph_store, and _build_vector_index
    separately for better control over degraded mode.
    """
    notes = _scan_md_db(md_db_path)
    notes_by_id = {n.note_id: n for n in notes}

    graph_store = _build_graph_store(notes, notes_by_id)
    graph_store.persist(persist_path=os.path.join(db_dir, "property_graph_store.json"))

    vector_index = _build_vector_index(notes, embed_model, db_dir)

    return vector_index, graph_store, notes


# ---------------------------------------------------------------------------
# Load index (fast path)
# ---------------------------------------------------------------------------


def load_index(
    db_dir: str,
    embed_model: Any,
    md_db_path: str,
) -> tuple[VectorStoreIndex, SimplePropertyGraphStore, list[ParsedNote]]:
    """Load persisted VectorStoreIndex + graph store from disk."""
    # Load graph store
    graph_store = SimplePropertyGraphStore.from_persist_path(
        os.path.join(db_dir, "property_graph_store.json")
    )

    # Load vector index
    storage_context = StorageContext.from_defaults(persist_dir=db_dir)
    vector_index = load_index_from_storage(
        storage_context=storage_context,
        embed_model=embed_model,
    )

    # Re-scan for note bodies (fast — no embedding, just file reads)
    notes = _scan_md_db(md_db_path)

    log.info("Loaded from %s — %d notes", db_dir, len(notes))
    return vector_index, graph_store, notes
