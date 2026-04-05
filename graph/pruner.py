"""Pruner — vector-similarity clustering for note deduplication.

Port of context_engine/prune/prune-builder.ts.
Uses VectorStoreIndex.as_retriever() for pairwise similarity, builds connected
components via DFS, returns PruneCluster dicts.
"""

from __future__ import annotations

import logging
import uuid
from typing import Any

from llama_index.core import VectorStoreIndex
from llama_index.embeddings.ollama import OllamaEmbedding

from .markdown_utils import normalize_token
from .models import (
    ParsedNote,
    PruneCluster,
    PruneClusterMember,
    PruneClusterStats,
    PruneConfig,
)

log = logging.getLogger(__name__)

DEFAULT_QUERY_SLICE = 1200


def build_prune_clusters(
    config: PruneConfig,
    index: VectorStoreIndex,
    embed_model: OllamaEmbedding,
    parsed_notes: dict[str, ParsedNote],
) -> list[PruneCluster]:
    """Build similarity-based clusters using connected components analysis."""
    allowed_types = set(config.include_note_types)
    excluded_tags = set(normalize_token(t) for t in config.exclude_tags)

    # Filter candidates
    candidates: list[ParsedNote] = []
    for note in parsed_notes.values():
        if note.note_type not in allowed_types:
            continue
        if any(t in excluded_tags for t in note.tags):
            continue
        candidates.append(note)

    note_by_id = {n.note_id: n for n in candidates}
    candidate_ids = set(note_by_id.keys())

    # Build similarity edges via vector retrieval
    edges: dict[tuple[str, str], float] = {}
    retriever = index.as_retriever(similarity_top_k=config.max_neighbors_per_note)

    for note in candidates:
        query = _build_query_from_note(note)
        results = retriever.retrieve(query)

        for r in results:
            metadata = getattr(r.node, "metadata", {}) or {}
            path = str(metadata.get("file_path", r.node.id_ or ""))
            if not path or path == note.note_id or path not in candidate_ids:
                continue

            score = r.score or 0.0
            if score < config.similarity_threshold:
                continue

            # Normalize edge key for dedup
            edge_key = (min(note.note_id, path), max(note.note_id, path))
            if edge_key not in edges or score > edges[edge_key]:
                edges[edge_key] = score

    if not edges:
        return []

    # Build adjacency
    adj: dict[str, dict[str, float]] = {}
    for (a, b), sim in edges.items():
        adj.setdefault(a, {})[b] = sim
        adj.setdefault(b, {})[a] = sim

    # DFS connected components
    visited: set[str] = set()
    clusters: list[PruneCluster] = []

    for start_node in adj:
        if start_node in visited:
            continue

        stack = [start_node]
        component: list[str] = []

        while stack:
            current = stack.pop()
            if current in visited:
                continue
            visited.add(current)
            component.append(current)
            for nbr in adj.get(current, {}):
                if nbr not in visited:
                    stack.append(nbr)

        if len(component) < config.min_cluster_size:
            continue

        # Representative: highest degree, ties by lex
        representative = min(
            component,
            key=lambda n: (-len(adj.get(n, {})), n),
        )

        # Similarity stats from unique edges within component
        component_set = set(component)
        sims: list[float] = []
        for (a, b), sim in edges.items():
            if a in component_set and b in component_set:
                sims.append(sim)

        if not sims:
            continue

        # Build members
        members: list[PruneClusterMember] = []
        for n in component:
            sim_to_rep = (
                1.0 if n == representative else adj.get(representative, {}).get(n, 0.0)
            )
            members.append(
                PruneClusterMember(
                    note_id=n,
                    similarity=sim_to_rep,
                    is_representative=(n == representative),
                )
            )

        clusters.append(
            PruneCluster(
                cluster_id=str(uuid.uuid4()),
                representative_note_id=representative,
                members=members,
                stats=PruneClusterStats(
                    size=len(component),
                    max_similarity=max(sims),
                    min_similarity=min(sims),
                    avg_similarity=sum(sims) / len(sims),
                ),
            )
        )

    log.info("Built %d prune clusters from %d candidates", len(clusters), len(candidates))
    return clusters


def _build_query_from_note(note: ParsedNote) -> str:
    """Build query from note title + first 1200 chars of body."""
    body = note.body[:DEFAULT_QUERY_SLICE]
    return f"{note.title}\n\n{body}"
