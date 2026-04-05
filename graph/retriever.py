"""ObsidiClawRetriever — hybrid retrieval with vector seeds + tier-aware graph expansion.

Replaces the TS hybrid-retrieval.ts:
  - VectorStoreIndex.as_retriever() for semantic vector search (seeds)
  - SimplePropertyGraphStore.get_triplets() for graph expansion (depth-1 + depth-2 CALLS)
  - Tier-aware score multipliers
  - Tag boosting + index note filtering

Symbol-centric graph model
──────────────────────────
Classes and methods (tier-1 codeSymbol) are the primary interlinked core.
Files and modules (tier-2/3) form the outer containment layer.

Tier-aware expansion heuristics
────────────────────────────────
Going UP (always):
  DEFINED_IN   tier-1 → tier-2   0.70×   symbol's parent file
  BELONGS_TO   tier-2 → tier-3   0.40×   file's parent module

Going DOWN (selective — only when seed score ≥ DOWN_SEED_THRESHOLD):
  CONTAINS_SYMBOL  tier-2 → tier-1   0.85×   symbols inside a file
  CONTAINS         tier-3 → tier-2   0.70×   files inside a module

Going SIDEWAYS (unconditional for CALLS, overlap-gated for IMPORTS):
  CALLS    tier-1 → tier-1   0.80×   symbol calls another symbol
  IMPORTS  tier-2 → tier-2   0.60×   file imports another file

CALLS edges get depth-2 expansion: if seed A --CALLS--> B --CALLS--> C,
C is included at score = seed × 0.80 × 0.80.

Generic (non-code notes):
  LINKS_TO  any   0.50×
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from llama_index.core import VectorStoreIndex
from llama_index.core.graph_stores import SimplePropertyGraphStore
from llama_index.embeddings.ollama import OllamaEmbedding

from .markdown_utils import normalize_token
from .models import ParsedNote, RetrievedNote

log = logging.getLogger(__name__)


class WorkspaceScopeViolationError(RuntimeError):
    """Raised when scoped vector retrieval returns a note outside requested workspace."""

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

TAG_BOOST_PER_TAG = 0.10   # +10% per matching tag
TAG_BOOST_MAX = 0.30       # cap at +30%

# Score multipliers for each edge type — symbol-centric weighting
_EDGE_MULTIPLIER: dict[str, float] = {
    "CALLS": 0.80,              # sideways: symbol → symbol (primary axis)
    "CONTAINS_SYMBOL": 0.85,    # down: file → symbol
    "DEFINED_IN": 0.70,         # up: symbol → file
    "CONTAINS": 0.70,           # down: module → file
    "IMPORTS": 0.60,            # sideways: file → file
    "LINKS_TO": 0.50,           # generic non-code wikilink
    "BELONGS_TO": 0.40,         # up: file → module (outer layer, low weight)
}

# Only follow downward edges when the seed has a strong enough score
DOWN_SEED_THRESHOLD = 0.60

# Only gate IMPORTS by query overlap; CALLS are followed unconditionally
# (the whole point of graph traversal is finding things the user didn't name)
SIDEWAYS_REQUIRE_QUERY_OVERLAP_IMPORTS = True

# Maximum depth for CALLS-chain expansion (depth-1 always; depth-2 for CALLS only)
MAX_CALLS_DEPTH = 2


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _name_tokens(note: ParsedNote) -> set[str]:
    """Normalized tokens from a note's title and path stem."""
    stem = Path(note.path).stem
    tokens: set[str] = set()
    for word in (note.title + " " + stem).split():
        t = normalize_token(word)
        if t:
            tokens.add(t)
    return tokens


def _overlaps_query(note: ParsedNote, query_tokens: set[str]) -> bool:
    """True if any name token appears in the query."""
    return bool(_name_tokens(note) & query_tokens)


def _apply_tag_boost(score: float, note_tags: list[str], query_tokens: set[str]) -> float:
    """Boost score by +10% per matching tag, capped at +30%."""
    if not note_tags or not query_tokens:
        return score
    matching = sum(1 for t in note_tags if t in query_tokens)
    boost = min(matching * TAG_BOOST_PER_TAG, TAG_BOOST_MAX)
    return score * (1.0 + boost)


def _should_skip_edge(
    edge_label: str,
    is_outgoing: bool,
    seed_score: float,
    neighbor_parsed: ParsedNote,
    query_tokens: set[str],
) -> bool:
    """Return True if this expansion edge should be skipped."""
    # Downward edges: only follow when seed is strong
    if edge_label in ("CONTAINS_SYMBOL", "CONTAINS"):
        if not is_outgoing:
            # Incoming CONTAINS_SYMBOL/CONTAINS means neighbor is the container
            # — that's "going up", allow.
            return False
        if seed_score < DOWN_SEED_THRESHOLD:
            return True

    # CALLS edges: follow unconditionally (symbol-centric graph primary axis)
    if edge_label == "CALLS":
        return False

    # IMPORTS edges: only follow when neighbor name overlaps query
    if edge_label == "IMPORTS" and SIDEWAYS_REQUIRE_QUERY_OVERLAP_IMPORTS:
        if not _overlaps_query(neighbor_parsed, query_tokens):
            return True

    return False


def _expand_one_hop(
    graph_store: SimplePropertyGraphStore,
    parsed_notes: dict[str, ParsedNote],
    source_notes: list[RetrievedNote],
    source_scores: dict[str, float],
    query_tokens: set[str],
    exclude_ids: set[str],
    workspace: str | None,
    depth: int,
    edge_filter: set[str] | None = None,
) -> list[RetrievedNote]:
    """Expand a set of notes by one hop, returning new neighbors.

    Args:
        edge_filter: if set, only follow edges with these labels.
                     None = follow all (subject to _should_skip_edge).
    """
    expanded: list[RetrievedNote] = []
    expanded_ids: set[str] = set()

    for source in source_notes:
        source_id = source.note_id
        source_score = source_scores.get(source_id, source.score)

        try:
            triplets = graph_store.get_triplets(entity_names=[source_id])
        except Exception as exc:
            log.warning("get_triplets failed for %s: %s", source_id, exc)
            continue

        for source_node, relation, target_node in triplets:
            source_name = getattr(source_node, "name", "")
            target_name = getattr(target_node, "name", "")
            edge_label = getattr(relation, "label", "LINKS_TO")

            if edge_filter and edge_label not in edge_filter:
                continue

            is_outgoing = source_name == source_id
            neighbor_id = target_name if is_outgoing else source_name

            if not neighbor_id or neighbor_id in exclude_ids or neighbor_id in expanded_ids:
                continue

            neighbor_parsed = parsed_notes.get(neighbor_id)
            if not neighbor_parsed or neighbor_parsed.note_type == "index":
                continue

            if workspace and neighbor_parsed.workspace != workspace:
                continue

            if _should_skip_edge(
                edge_label=edge_label,
                is_outgoing=is_outgoing,
                seed_score=source_score,
                neighbor_parsed=neighbor_parsed,
                query_tokens=query_tokens,
            ):
                continue

            multiplier = _EDGE_MULTIPLIER.get(edge_label, 0.50)
            neighbor_score = source_score * multiplier
            boosted = _apply_tag_boost(neighbor_score, neighbor_parsed.tags, query_tokens)

            expanded.append(
                RetrievedNote(
                    note_id=neighbor_id,
                    path=neighbor_id,
                    content=neighbor_parsed.body,
                    score=boosted,
                    type=neighbor_parsed.note_type,  # type: ignore[arg-type]
                    tool_id=neighbor_parsed.tool_id,
                    tags=neighbor_parsed.tags,
                    retrieval_source="graph",
                    linked_from=[source_id],
                    depth=depth,
                    tier=neighbor_parsed.tier,
                    workspace=neighbor_parsed.workspace,
                    title=neighbor_parsed.title,
                )
            )
            expanded_ids.add(neighbor_id)

    expanded.sort(key=lambda n: n.score, reverse=True)
    return expanded


def expand_graph_neighbors(
    graph_store: Optional[SimplePropertyGraphStore],
    parsed_notes: dict[str, ParsedNote],
    seed_notes: list[RetrievedNote],
    query_tokens: set[str],
    workspace: str | None = None,
    max_calls_depth: int = MAX_CALLS_DEPTH,
) -> list[RetrievedNote]:
    """Expand seed notes using tier-aware edge heuristics.

    Depth-1: all edge types (subject to skip rules).
    Depth-2+: CALLS edges only — follow symbol call chains up to max_calls_depth.
    """
    if not graph_store or not seed_notes:
        return []

    seed_ids = {n.note_id for n in seed_notes}
    seed_scores = {n.note_id: n.score for n in seed_notes}
    all_seen = set(seed_ids)

    # ── Depth 1: full expansion (all edge types) ─────────────────────────
    depth1 = _expand_one_hop(
        graph_store, parsed_notes, seed_notes, seed_scores,
        query_tokens, exclude_ids=all_seen, workspace=workspace, depth=1,
    )
    all_seen.update(n.note_id for n in depth1)

    all_expanded = list(depth1)

    # ── Depth 2+: CALLS-only expansion ───────────────────────────────────
    # Follow symbol→symbol call chains beyond depth-1.
    current_frontier = [n for n in depth1 if n.tier == "1"]
    frontier_scores = {n.note_id: n.score for n in current_frontier}

    for d in range(2, max_calls_depth + 1):
        if not current_frontier:
            break

        deeper = _expand_one_hop(
            graph_store, parsed_notes, current_frontier, frontier_scores,
            query_tokens, exclude_ids=all_seen, workspace=workspace,
            depth=d, edge_filter={"CALLS"},
        )
        if not deeper:
            break

        all_seen.update(n.note_id for n in deeper)
        all_expanded.extend(deeper)
        current_frontier = [n for n in deeper if n.tier == "1"]
        frontier_scores = {n.note_id: n.score for n in current_frontier}

    all_expanded.sort(key=lambda n: n.score, reverse=True)

    log.info(
        "Graph expansion: %d depth-1 + %d deeper from %d seeds",
        len(depth1), len(all_expanded) - len(depth1), len(seed_notes),
    )

    return all_expanded


# ---------------------------------------------------------------------------
# Retriever
# ---------------------------------------------------------------------------


class ObsidiClawRetriever:
    """Hybrid retrieval: vector seeds + tier-aware graph-expanded neighbors.

    1. VectorStoreIndex.as_retriever() for top-k semantic seeds
    2. SimplePropertyGraphStore.get_triplets() for depth-1 expansion
       with per-edge-type score multipliers
    3. Tag boosting applied to both
    4. Index notes filtered from both
    """

    def __init__(
        self,
        index: VectorStoreIndex,
        graph_store: Optional[SimplePropertyGraphStore],
        embed_model: OllamaEmbedding,
        parsed_notes: dict[str, ParsedNote],
        similarity_top_k: int = 8,
    ) -> None:
        self.index = index
        self.graph_store = graph_store
        self.embed_model = embed_model
        self.parsed_notes = parsed_notes
        self.similarity_top_k = similarity_top_k

    def retrieve(self, query: str, workspace: str | None = None) -> tuple[list[RetrievedNote], list[RetrievedNote]]:
        """Run hybrid retrieval.

        Returns (seed_notes, expanded_notes) — both lists filtered and scored.
        If workspace is set, only notes from that workspace are returned as seeds.
        Graph-expanded notes are also filtered to the same workspace.
        """
        query_tokens = set(
            normalize_token(w) for w in query.lower().split() if normalize_token(w)
        )

        # ── Step 1: Vector seeds ──────────────────────────────────────────
        retriever_kwargs: dict = {"similarity_top_k": self.similarity_top_k}
        if workspace:
            from llama_index.core.vector_stores.types import (
                MetadataFilters,
                MetadataFilter,
                FilterOperator,
            )

            retriever_kwargs["filters"] = MetadataFilters(
                filters=[
                    MetadataFilter(
                        key="workspace", value=workspace, operator=FilterOperator.EQ
                    ),
                ]
            )

        retriever = self.index.as_retriever(**retriever_kwargs)
        raw_results = retriever.retrieve(query)

        seeds: list[RetrievedNote] = []
        seed_ids: set[str] = set()
        seed_scores: dict[str, float] = {}

        for r in raw_results:
            node = r.node
            score = r.score or 0.0
            raw_id = node.id_ or ""
            metadata = getattr(node, "metadata", {}) or {}

            # Chunk dedup: map chunk IDs back to parent note
            parent_id = str(metadata.get("parent_note_id", ""))
            note_id = parent_id if parent_id else raw_id
            file_path = str(metadata.get("file_path", note_id))
            note_type_str = str(metadata.get("note_type", "concept"))

            if note_type_str == "index":
                continue

            parsed = self.parsed_notes.get(file_path)
            tags = parsed.tags if parsed else []
            tool_id = parsed.tool_id if parsed else metadata.get("tool_id")
            content = parsed.body if parsed else getattr(node, "text", "")
            tier = parsed.tier if parsed else ""
            ws = parsed.workspace if parsed else str(metadata.get("workspace", ""))

            # Workspace scope is a hard contract for scoped retrieval.
            # If the vector backend violates metadata filtering, fail this path
            # and let the engine fall back to deterministic keyword retrieval.
            if workspace and ws != workspace:
                raise WorkspaceScopeViolationError(
                    f"workspace scope violated: requested={workspace!r} got={ws!r} note={file_path!r}"
                )

            boosted_score = _apply_tag_boost(score, tags, query_tokens)

            # If we already have a seed for this note (from another chunk), keep highest score
            if file_path in seed_ids:
                if boosted_score > seed_scores.get(file_path, 0.0):
                    seed_scores[file_path] = boosted_score
                    # Update the existing seed's score
                    for s in seeds:
                        if s.note_id == file_path:
                            s.score = boosted_score
                            break
                continue

            seeds.append(
                RetrievedNote(
                    note_id=file_path,
                    path=file_path,
                    content=content,
                    score=boosted_score,
                    type=note_type_str,  # type: ignore[arg-type]
                    tool_id=tool_id,
                    tags=tags,
                    retrieval_source="vector",
                    linked_from=None,
                    depth=0,
                    tier=tier,
                    workspace=ws,
                    title=parsed.title if parsed else str(metadata.get("title", "")),
                )
            )
            seed_ids.add(file_path)
            seed_scores[file_path] = boosted_score

        # ── Step 2: Tier-aware graph expansion ────────────────────────────
        expanded = expand_graph_neighbors(
            graph_store=self.graph_store,
            parsed_notes=self.parsed_notes,
            seed_notes=seeds,
            query_tokens=query_tokens,
            workspace=workspace,
        )

        seeds.sort(key=lambda n: n.score, reverse=True)

        log.info(
            "Retrieved %d seeds + %d expanded for query: %.60s...",
            len(seeds),
            len(expanded),
            query,
        )

        return seeds, expanded
