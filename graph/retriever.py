"""ObsidiClawRetriever — hybrid retrieval with vector seeds + tier-aware graph expansion.

Replaces the TS hybrid-retrieval.ts:
  - VectorStoreIndex.as_retriever() for semantic vector search (seeds)
  - SimplePropertyGraphStore.get_triplets() for graph expansion (depth-1 + depth-2 CALLS)
  - Tier-aware score multipliers
  - Tag boosting + index note filtering

Feature: Relational triple embedding (#2)
─────────────────────────────────────────
For each candidate hop, we build a short relational description:
    "{source.title} {edge_phrase} {neighbor.title}"
e.g. "ContextEngine calls resolvePythonPath"

This string is batch-embedded and its cosine similarity to the query embedding
is used as a re-ranking factor (±30% on the base score). The embed_model is
optional — falls back to pure-multiplier scoring when unavailable.

Feature: Path provenance tracking (#4)
───────────────────────────────────────
Every expanded note carries via_edge (edge label) and via_source_title (the
source note's human title) so the TS formatting layer can annotate each note
with the path that brought it into the result set.

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
import math
from pathlib import Path
from typing import Any, Optional

from llama_index.core import VectorStoreIndex
from llama_index.core.graph_stores import SimplePropertyGraphStore

from .markdown_utils import normalize_token
from .models import ParsedNote, RetrievedNote

log = logging.getLogger(__name__)


class WorkspaceScopeViolationError(RuntimeError):
    """Raised when scoped vector retrieval returns a note outside requested workspace."""

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

TAG_BOOST_PER_TAG = 0.10
TAG_BOOST_MAX = 0.30

_EDGE_MULTIPLIER: dict[str, float] = {
    "CALLS": 0.80,
    "CONTAINS_SYMBOL": 0.85,
    "DEFINED_IN": 0.70,
    "CONTAINS": 0.70,
    "IMPORTS": 0.60,
    "LINKS_TO": 0.50,
    "BELONGS_TO": 0.40,
}

# Human-readable edge phrases for triple strings
_EDGE_PHRASE: dict[str, str] = {
    "CALLS": "calls",
    "CONTAINS_SYMBOL": "contains",
    "DEFINED_IN": "is defined in",
    "CONTAINS": "contains file",
    "IMPORTS": "imports",
    "LINKS_TO": "links to",
    "BELONGS_TO": "belongs to",
}

DOWN_SEED_THRESHOLD = 0.60
SIDEWAYS_REQUIRE_QUERY_OVERLAP_IMPORTS = True
MAX_CALLS_DEPTH = 2

# Triple embedding scoring parameters
# triple_factor = clamp(TRIPLE_CENTER + triple_sim * TRIPLE_SCALE, TRIPLE_MIN, TRIPLE_MAX)
# At triple_sim=0.5 → factor≈1.0 (neutral); range [0.6, 1.4]
TRIPLE_CENTER = 0.50
TRIPLE_SCALE = 1.00
TRIPLE_MIN = 0.60
TRIPLE_MAX = 1.40


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _name_tokens(note: ParsedNote) -> set[str]:
    stem = Path(note.path).stem
    tokens: set[str] = set()
    for word in (note.title + " " + stem).split():
        t = normalize_token(word)
        if t:
            tokens.add(t)
    return tokens


def _overlaps_query(note: ParsedNote, query_tokens: set[str]) -> bool:
    return bool(_name_tokens(note) & query_tokens)


def _apply_tag_boost(score: float, note_tags: list[str], query_tokens: set[str]) -> float:
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
    if edge_label in ("CONTAINS_SYMBOL", "CONTAINS"):
        if is_outgoing and seed_score < DOWN_SEED_THRESHOLD:
            return True
    if edge_label == "CALLS":
        return False
    if edge_label == "IMPORTS" and SIDEWAYS_REQUIRE_QUERY_OVERLAP_IMPORTS:
        if not _overlaps_query(neighbor_parsed, query_tokens):
            return True
    return False


# ---------------------------------------------------------------------------
# Relational triple embedding (#2)
# ---------------------------------------------------------------------------


def _build_triple_string(
    source_title: str,
    edge_label: str,
    neighbor_title: str,
    is_outgoing: bool,
) -> str:
    """Build a natural-language relational description for embedding.

    Outgoing: "ContextEngine calls resolvePythonPath"
    Incoming: "resolvePythonPath called by ContextEngine"  (flip for readability)
    """
    phrase = _EDGE_PHRASE.get(edge_label, edge_label.lower())
    if is_outgoing:
        return f"{source_title} {phrase} {neighbor_title}"
    else:
        return f"{neighbor_title} {phrase} {source_title}"


def _cosine_sim(a: list[float], b: list[float]) -> float:
    """Cosine similarity between two embedding vectors."""
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a < 1e-10 or norm_b < 1e-10:
        return 0.0
    return dot / (norm_a * norm_b)


def _triple_factor(triple_sim: float) -> float:
    """Convert a cosine similarity to a score multiplier in [TRIPLE_MIN, TRIPLE_MAX]."""
    raw = TRIPLE_CENTER + triple_sim * TRIPLE_SCALE
    return max(TRIPLE_MIN, min(TRIPLE_MAX, raw))


def _batch_score_triples(
    query_embedding: list[float],
    triple_strings: list[str],
    embed_model: Any,
) -> list[float]:
    """Batch-embed triple strings and return per-triple cosine similarities."""
    try:
        # LlamaIndex embeddings use get_text_embedding_batch; our EmbedProvider
        # protocol uses get_text_embeddings — try both.
        if hasattr(embed_model, "get_text_embeddings"):
            triple_embeddings = embed_model.get_text_embeddings(triple_strings)
        else:
            triple_embeddings = embed_model.get_text_embedding_batch(triple_strings)
        return [_cosine_sim(query_embedding, te) for te in triple_embeddings]
    except Exception as exc:
        log.warning("Triple embedding failed, using neutral scores: %s", exc)
        return [0.5] * len(triple_strings)


# ---------------------------------------------------------------------------
# Core expansion
# ---------------------------------------------------------------------------


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
    query_embedding: list[float] | None = None,
    embed_model: Any | None = None,
) -> list[RetrievedNote]:
    """Expand a set of notes by one hop, returning new neighbors.

    If query_embedding + embed_model are provided, scores each candidate hop
    by the cosine similarity of the relational triple embedding to the query
    (feature #2). Also records via_edge + via_source_title on each result (feature #4).
    """
    # ── Pass 1: collect raw candidates ────────────────────────────────────
    candidates: list[tuple[str, str, float, ParsedNote, str, bool]] = []
    # (source_id, source_title, source_score, neighbor_parsed, edge_label, is_outgoing)
    seen_neighbors: set[str] = set()

    for source in source_notes:
        source_id = source.note_id
        source_score = source_scores.get(source_id, source.score)
        source_title = source.title or Path(source_id).stem

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

            if not neighbor_id or neighbor_id in exclude_ids or neighbor_id in seen_neighbors:
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

            candidates.append((source_id, source_title, source_score, neighbor_parsed, edge_label, is_outgoing))
            seen_neighbors.add(neighbor_id)

    if not candidates:
        return []

    # ── Pass 2: triple embedding scoring (#2) ─────────────────────────────
    if query_embedding is not None and embed_model is not None:
        triple_strings = [
            _build_triple_string(src_title, edge, nbr.title or Path(nbr.path).stem, outgoing)
            for _, src_title, _, nbr, edge, outgoing in candidates
        ]
        triple_sims = _batch_score_triples(query_embedding, triple_strings, embed_model)
        log.debug("Triple sims for %d candidates: min=%.3f max=%.3f avg=%.3f",
                  len(triple_sims),
                  min(triple_sims), max(triple_sims),
                  sum(triple_sims) / len(triple_sims))
    else:
        triple_sims = [0.5] * len(candidates)

    # ── Pass 3: build RetrievedNote results ───────────────────────────────
    expanded: list[RetrievedNote] = []

    for (source_id, source_title, source_score, neighbor_parsed, edge_label, is_outgoing), triple_sim in zip(candidates, triple_sims):
        multiplier = _EDGE_MULTIPLIER.get(edge_label, 0.50)
        base_score = source_score * multiplier
        triple_f = _triple_factor(triple_sim)
        neighbor_score = base_score * triple_f
        boosted = _apply_tag_boost(neighbor_score, neighbor_parsed.tags, query_tokens)

        expanded.append(
            RetrievedNote(
                note_id=neighbor_parsed.note_id,
                path=neighbor_parsed.path,
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
                via_edge=edge_label,
                via_source_title=source_title,
            )
        )

    expanded.sort(key=lambda n: n.score, reverse=True)
    return expanded


def expand_graph_neighbors(
    graph_store: Optional[SimplePropertyGraphStore],
    parsed_notes: dict[str, ParsedNote],
    seed_notes: list[RetrievedNote],
    query_tokens: set[str],
    workspace: str | None = None,
    max_calls_depth: int = MAX_CALLS_DEPTH,
    query_embedding: list[float] | None = None,
    embed_model: Any | None = None,
) -> list[RetrievedNote]:
    """Expand seed notes using tier-aware edge heuristics + optional triple scoring.

    Depth-1: all edge types (subject to skip rules), triple-embedding scored.
    Depth-2+: CALLS edges only — follow symbol call chains up to max_calls_depth.
    """
    if not graph_store or not seed_notes:
        return []

    seed_ids = {n.note_id for n in seed_notes}
    seed_scores = {n.note_id: n.score for n in seed_notes}
    all_seen = set(seed_ids)

    # ── Depth 1: full expansion ───────────────────────────────────────────
    depth1 = _expand_one_hop(
        graph_store, parsed_notes, seed_notes, seed_scores,
        query_tokens, exclude_ids=all_seen, workspace=workspace, depth=1,
        query_embedding=query_embedding, embed_model=embed_model,
    )
    all_seen.update(n.note_id for n in depth1)
    all_expanded = list(depth1)

    # ── Depth 2+: CALLS-only expansion ───────────────────────────────────
    current_frontier = [n for n in depth1 if n.tier == "1"]
    frontier_scores = {n.note_id: n.score for n in current_frontier}

    for d in range(2, max_calls_depth + 1):
        if not current_frontier:
            break
        deeper = _expand_one_hop(
            graph_store, parsed_notes, current_frontier, frontier_scores,
            query_tokens, exclude_ids=all_seen, workspace=workspace,
            depth=d, edge_filter={"CALLS"},
            query_embedding=query_embedding, embed_model=embed_model,
        )
        if not deeper:
            break
        all_seen.update(n.note_id for n in deeper)
        all_expanded.extend(deeper)
        current_frontier = [n for n in deeper if n.tier == "1"]
        frontier_scores = {n.note_id: n.score for n in current_frontier}

    all_expanded.sort(key=lambda n: n.score, reverse=True)

    log.info(
        "Graph expansion: %d depth-1 + %d deeper from %d seeds (triple_embed=%s)",
        len(depth1), len(all_expanded) - len(depth1), len(seed_notes),
        "yes" if query_embedding is not None else "no",
    )

    return all_expanded


# ---------------------------------------------------------------------------
# Retriever
# ---------------------------------------------------------------------------


class ObsidiClawRetriever:
    """Hybrid retrieval: vector seeds + tier-aware graph-expanded neighbors.

    1. VectorStoreIndex.as_retriever() for top-k semantic seeds
    2. SimplePropertyGraphStore.get_triplets() for depth-1 expansion
       with per-edge-type score multipliers + relational triple embedding (#2)
    3. Tag boosting applied to both
    4. Index notes filtered from both
    5. via_edge + via_source_title set on all graph-expanded notes (#4)
    """

    def __init__(
        self,
        index: VectorStoreIndex,
        graph_store: Optional[SimplePropertyGraphStore],
        embed_model: Any,
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
        """
        query_tokens = set(
            normalize_token(w) for w in query.lower().split() if normalize_token(w)
        )

        # ── Step 1: Embed query (reused for triple scoring) ───────────────
        query_embedding: list[float] | None = None
        if self.embed_model is not None:
            try:
                query_embedding = self.embed_model.get_text_embedding(query)
            except Exception as exc:
                log.warning("Query embedding failed, triple scoring disabled: %s", exc)

        # ── Step 2: Vector seeds ──────────────────────────────────────────
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

            if workspace and ws != workspace:
                raise WorkspaceScopeViolationError(
                    f"workspace scope violated: requested={workspace!r} got={ws!r} note={file_path!r}"
                )

            boosted_score = _apply_tag_boost(score, tags, query_tokens)

            if file_path in seed_ids:
                if boosted_score > seed_scores.get(file_path, 0.0):
                    seed_scores[file_path] = boosted_score
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

        # ── Step 3: Tier-aware graph expansion + triple scoring ───────────
        expanded = expand_graph_neighbors(
            graph_store=self.graph_store,
            parsed_notes=self.parsed_notes,
            seed_notes=seeds,
            query_tokens=query_tokens,
            workspace=workspace,
            query_embedding=query_embedding,
            embed_model=self.embed_model,
        )

        seeds.sort(key=lambda n: n.score, reverse=True)

        log.info(
            "Retrieved %d seeds + %d expanded for query: %.60s...",
            len(seeds), len(expanded), query,
        )

        return seeds, expanded
