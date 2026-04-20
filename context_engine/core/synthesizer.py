"""Context synthesizer — format raw notes into tiered markdown, then call LLM to refine.

Ported from the TypeScript ContextReviewer + formatContext in src/engine/.
Two entry points:
  format_context()  — builds tiered markdown from RetrievedNote lists (no LLM)
  synthesize()      — calls the configured LLM with the personality prompt
"""

from __future__ import annotations

import json
import logging
import re
import urllib.request
import urllib.error
from pathlib import Path
from typing import Any, Optional

from .models import RetrievedNote

log = logging.getLogger(__name__)

MIN_SEED_SCORE = 0.45
DEFAULT_TIMEOUT = 20.0

_EDGE_READABLE: dict[str, str] = {
    "CALLS": "calls",
    "CONTAINS_SYMBOL": "contains",
    "DEFINED_IN": "defined in",
    "CONTAINS": "contains",
    "IMPORTS": "imports",
    "LINKS_TO": "links to",
    "BELONGS_TO": "belongs to",
}

NOTHING_FOUND = (
    "<!-- ContextGarden: no relevant knowledge base context found for this query -->"
)

_FALLBACK_SYSTEM_PROMPT = (
    "You synthesize retrieved context into focused, query-relevant summaries."
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _edge_arrow(label: str) -> str:
    readable = _EDGE_READABLE.get(label, label.lower())
    return f"—[{readable}]→"


# ---------------------------------------------------------------------------
# format_context — tier-aware markdown builder
# ---------------------------------------------------------------------------


def format_context(
    seed_notes: list[RetrievedNote],
    expanded_notes: list[RetrievedNote],
    path_traces: Optional[list[dict]] = None,
) -> str:
    """Build tiered markdown context from retrieved notes.

    path_traces: list of dicts with keys startId, endId, steps
      steps: list of dicts with nodeId, edgeLabel, edgeDirection
    """
    all_notes = [*seed_notes, *expanded_notes]
    if not all_notes:
        return NOTHING_FOUND

    lines: list[str] = [
        "<!-- ContextGarden Knowledge Base Context -->",
        "",
        "# Knowledge Base Context",
        "",
    ]

    tier3 = [n for n in all_notes if n.tier == "3"]
    tier2 = [n for n in all_notes if n.tier == "2"]
    tier1 = [n for n in all_notes if n.tier == "1"]
    concepts = [n for n in all_notes if not n.tier and n.type == "concept"]
    tools = [n for n in all_notes if n.type == "tool"]
    other = [n for n in all_notes if not n.tier and n.type not in ("concept", "tool")]

    note_by_id = {n.note_id: n for n in all_notes}

    def render_note(note: RetrievedNote, label_suffix: str = "", extra_meta: str = "") -> None:
        seed_mark = "*" if (note.depth == 0 or note.retrieval_source == "vector") else ""
        parts = [p for p in [seed_mark, f"score: {note.score:.3f}", extra_meta] if p]
        meta = " | ".join(parts)
        lines.append(f"### {note.path}{label_suffix} ({meta})")
        if note.via_edge and note.via_source_title:
            arrow = _edge_arrow(note.via_edge)
            lines.append(f"_via: {note.via_source_title} {arrow} {note.via_edge} {arrow} here_")
        lines.append(note.content or "")
        lines.append("")

    if tier3:
        lines.append("## Module Context")
        lines.append("_Architecture-level: what each subsystem/directory does._")
        lines.append("")
        for n in tier3:
            render_note(n)

    if tier2:
        lines.append("## File Context")
        lines.append("_File-level: exports, imports, and role within the module._")
        lines.append("")
        for n in tier2:
            render_note(n)

    if tier1:
        tier1_ids = {n.note_id for n in tier1}
        call_related = [
            n for n in tier1
            if n.linked_from and any(lid in tier1_ids for lid in n.linked_from)
        ]
        call_related_ids = {n.note_id for n in call_related}
        direct = [n for n in tier1 if n.note_id not in call_related_ids]

        lines.append("## Symbol Details")
        lines.append("_Symbol-level: specific function/class/type signatures and behavior._")
        lines.append("")
        for n in direct:
            kind = f" [{n.symbol_kind}]" if getattr(n, "symbol_kind", None) else ""
            render_note(n, kind)

        if call_related:
            lines.append("## Call Relationships")
            lines.append("_Symbols reached via CALLS/IMPORTS edges from the above._")
            lines.append("")
            for n in call_related:
                kind = f" [{n.symbol_kind}]" if getattr(n, "symbol_kind", None) else ""
                callers = [lid for lid in (n.linked_from or []) if lid in tier1_ids]
                caller_meta = f"called by: {', '.join(callers)}" if callers else ""
                render_note(n, kind, caller_meta)

    if path_traces:
        lines.append("## Path Traces")
        lines.append("_How top results connect:_")
        lines.append("")
        for trace in path_traces:
            start_note = note_by_id.get(trace["startId"])
            end_note = note_by_id.get(trace["endId"])
            start_path = start_note.path if start_note else trace["startId"]
            end_path = end_note.path if end_note else trace["endId"]
            lines.append(f"**{start_path} → {end_path}**")
            trace_parts: list[str] = [start_path]
            for step in trace.get("steps", []):
                edge_label = step.get("edgeLabel", "")
                if edge_label:
                    direction = step.get("edgeDirection", "outgoing")
                    arrow = "→" if direction == "outgoing" else "←"
                    readable = _EDGE_READABLE.get(edge_label, edge_label.lower())
                    trace_parts.append(f"{arrow}[{readable}]{arrow}")
                    step_note = note_by_id.get(step.get("nodeId", ""))
                    trace_parts.append(step_note.path if step_note else step.get("nodeId", ""))
            lines.append(" ".join(trace_parts))
            lines.append("")

    if concepts:
        lines.append("## Concepts & Patterns")
        lines.append("_Design heuristics, failure modes, best practices._")
        lines.append("")
        for n in concepts:
            render_note(n)

    if other:
        lines.append("## Additional Context")
        lines.append("")
        for n in other:
            render_note(n)

    if tools:
        lines.append("## Tools")
        lines.append("")
        for n in tools:
            render_note(n)

    lines.append("<!-- End ContextGarden Context -->")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Personality loader
# ---------------------------------------------------------------------------


def _load_personality(personalities_dir: Optional[str] = None) -> str:
    """Load the context-synthesizer personality system prompt from disk."""
    candidates: list[Path] = []

    if personalities_dir:
        candidates.append(Path(personalities_dir) / "context-synthesizer.md")

    # Walk up from this file to find src/data/
    here = Path(__file__).resolve()
    for parent in here.parents:
        candidate = parent / "src" / "data" / "context-synthesizer.md"
        if candidate.exists():
            candidates.append(candidate)
            break

    for path in candidates:
        try:
            raw = path.read_text(encoding="utf-8")
            # Strip YAML frontmatter
            body = re.sub(r"^---\n.*?\n---\n", "", raw, flags=re.DOTALL).strip()
            if body:
                log.debug("Loaded context-synthesizer personality from %s", path)
                return body
        except Exception as exc:
            log.warning("Failed to load personality from %s: %s", path, exc)

    log.warning("context-synthesizer personality not found, using fallback prompt")
    return _FALLBACK_SYSTEM_PROMPT


# ---------------------------------------------------------------------------
# LLM chat — provider-agnostic
# ---------------------------------------------------------------------------


def _llm_chat(
    messages: list[dict[str, str]],
    synth_config: dict[str, Any],
    timeout: float = DEFAULT_TIMEOUT,
) -> str:
    """Call configured LLM provider and return the response content string."""
    provider = synth_config.get("provider", "ollama").lower()
    model = synth_config.get("model", "")
    host = synth_config.get("host", "").rstrip("/").removesuffix("/v1")
    api_key = synth_config.get("api_key", "")
    max_tokens = synth_config.get("max_tokens", 4096)
    temperature = 0.1

    if provider == "ollama":
        url = f"{host}/api/chat"
        payload = {
            "model": model,
            "messages": messages,
            "stream": False,
            "options": {"temperature": temperature, "num_predict": max_tokens},
        }
        headers: dict[str, str] = {"Content-Type": "application/json"}

    elif provider == "openai":
        url = f"{host}/v1/chat/completions"
        payload = {
            "model": model,
            "messages": messages,
            "max_tokens": max_tokens,
            "temperature": temperature,
        }
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        }

    elif provider == "anthropic":
        url = "https://api.anthropic.com/v1/messages"
        system_msgs = [m["content"] for m in messages if m["role"] == "system"]
        user_msgs = [m for m in messages if m["role"] != "system"]
        payload = {
            "model": model,
            "max_tokens": max_tokens,
            "system": "\n\n".join(system_msgs) if system_msgs else None,
            "messages": user_msgs,
        }
        if payload["system"] is None:
            del payload["system"]
        headers = {
            "Content-Type": "application/json",
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
        }

    else:
        raise ValueError(f"Unknown LLM provider: {provider!r}")

    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")

    with urllib.request.urlopen(req, timeout=timeout) as resp:
        result = json.loads(resp.read().decode("utf-8"))

    if provider == "ollama":
        return result["message"]["content"]
    elif provider == "openai":
        return result["choices"][0]["message"]["content"]
    elif provider == "anthropic":
        return result["content"][0]["text"]

    raise ValueError(f"Unhandled provider: {provider!r}")


# ---------------------------------------------------------------------------
# Public synthesize() entry point
# ---------------------------------------------------------------------------


def synthesize(
    query: str,
    seed_notes: list[RetrievedNote],
    raw_context: str,
    synth_config: dict[str, Any],
    personalities_dir: Optional[str] = None,
    min_seed_score: float = MIN_SEED_SCORE,
    timeout: float = DEFAULT_TIMEOUT,
) -> Optional[str]:
    """Call the synthesis LLM to transform raw_context into a focused context pack.

    Returns the synthesized string, or None if skipped/failed (caller should
    fall back to raw_context).
    """
    if not synth_config.get("provider") or not synth_config.get("model"):
        log.debug("Synthesis skipped: no LLM provider/model configured")
        return None

    if not seed_notes:
        log.debug("Synthesis skipped: no seed notes")
        return None

    max_seed_score = max((n.score for n in seed_notes if n.depth == 0), default=0.0)
    if max_seed_score < min_seed_score:
        log.debug("Synthesis skipped: best seed score %.3f < threshold %.3f", max_seed_score, min_seed_score)
        return None

    system_prompt = _load_personality(personalities_dir)

    user_prompt = "\n".join([
        "## Query",
        f'"{query}"',
        "",
        "## Retrieved Context",
        raw_context,
    ])

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]

    try:
        result = _llm_chat(messages, synth_config, timeout=timeout)
        if not result.strip():
            log.warning("Synthesis returned empty response")
            return None
        log.info("Synthesis complete: %d chars", len(result))
        return result.strip()
    except Exception as exc:
        log.warning("Synthesis failed, using raw context: %s", exc)
        return None
