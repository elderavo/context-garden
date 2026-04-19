"""KeywordRetriever — TF-based fallback retriever when no embedding provider is available.

Uses tokenized title, tags, path segments, and body text to rank notes by
keyword overlap with the query. Zero external dependencies — works offline.
"""

from __future__ import annotations

import logging
from pathlib import Path

from .markdown_utils import normalize_token
from .models import ParsedNote, RetrievedNote

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Scoring weights
# ---------------------------------------------------------------------------

TITLE_WEIGHT = 0.4
TAG_WEIGHT = 0.3
PATH_WEIGHT = 0.2
BODY_WEIGHT = 0.05
BODY_CAP = 0.3


# ---------------------------------------------------------------------------
# KeywordRetriever
# ---------------------------------------------------------------------------


class KeywordRetriever:
    """Score-based keyword retriever using tokenized note metadata."""

    def __init__(self, parsed_notes: dict[str, ParsedNote]) -> None:
        self._notes = parsed_notes
        self._title_tokens: dict[str, set[str]] = {}
        self._tag_tokens: dict[str, set[str]] = {}
        self._path_tokens: dict[str, set[str]] = {}
        self._body_tokens: dict[str, set[str]] = {}

        for note_id, note in parsed_notes.items():
            # Title tokens
            self._title_tokens[note_id] = {
                normalize_token(w) for w in note.title.split() if normalize_token(w)
            }

            # Tag tokens (already normalized in ParsedNote)
            self._tag_tokens[note_id] = set(note.tags)

            # Path segment tokens
            path_parts = Path(note.path).with_suffix("").parts
            self._path_tokens[note_id] = {
                normalize_token(p) for p in path_parts if normalize_token(p)
            }

            # Body tokens (unique set for overlap scoring)
            self._body_tokens[note_id] = {
                normalize_token(w) for w in note.body.split() if normalize_token(w)
            }

    def retrieve(
        self,
        query: str,
        top_k: int = 8,
        workspace: str | None = None,
    ) -> list[RetrievedNote]:
        """Retrieve notes ranked by keyword overlap with the query.

        Returns at most top_k RetrievedNote objects with scores normalized
        so the best match has score 1.0.

        If workspace is set, only notes with matching workspace are considered.
        """
        query_tokens = {
            normalize_token(w) for w in query.lower().split() if normalize_token(w)
        }

        if not query_tokens:
            return []

        scored: list[tuple[str, float]] = []

        for note_id, note in self._notes.items():
            # Skip index notes
            if note.note_type == "index":
                continue

            if workspace and note.workspace != workspace:
                continue

            title_overlap = len(query_tokens & self._title_tokens.get(note_id, set()))
            tag_overlap = len(query_tokens & self._tag_tokens.get(note_id, set()))
            path_overlap = len(query_tokens & self._path_tokens.get(note_id, set()))
            body_overlap = len(query_tokens & self._body_tokens.get(note_id, set()))

            score = (
                title_overlap * TITLE_WEIGHT
                + tag_overlap * TAG_WEIGHT
                + path_overlap * PATH_WEIGHT
                + min(body_overlap * BODY_WEIGHT, BODY_CAP)
            )

            if score > 0:
                scored.append((note_id, score))

        if not scored:
            return []

        # Normalize so best match = 1.0
        max_score = max(s for _, s in scored)
        scored = [(nid, s / max_score) for nid, s in scored]

        # Sort descending, take top_k
        scored.sort(key=lambda x: x[1], reverse=True)
        top = scored[:top_k]

        results: list[RetrievedNote] = []
        for note_id, score in top:
            note = self._notes[note_id]
            results.append(
                RetrievedNote(
                    note_id=note_id,
                    path=note.path,
                    content=note.body,
                    score=score,
                    type=note.note_type,
                    retrieval_source="keyword",
                    tool_id=note.tool_id,
                    tags=note.tags,
                    linked_from=None,
                    depth=0,
                    tier=note.tier,
                    workspace=note.workspace,
                    title=note.title,
                )
            )

        log.info(
            "Keyword retrieved %d notes for query: %.60s...",
            len(results),
            query,
        )
        return results
