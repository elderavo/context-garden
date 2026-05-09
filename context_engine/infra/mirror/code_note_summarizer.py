from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import urllib.error
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from ...config import get_code_summary_config

log = logging.getLogger(__name__)

_SUMMARY_HEADER = "## Summary"
_SUMMARY_META_START = "<!-- cg:summary-meta"
_SUMMARY_META_RE = re.compile(r"<!-- cg:summary-meta\s*\n(.*?)\n-->", re.DOTALL)
_FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---\n?", re.DOTALL)

_PROMPT_VERSION = "code-note-v1"
_SYSTEM_PROMPTS = {
    "1": (
        "You summarize symbol-level code notes for retrieval. "
        "Explain what the symbol does, its role in the codebase, its important "
        "inputs/outputs or contracts, and notable call relationships. "
        "Keep the summary concise, factual, and specific. "
        "Do not emit a heading. Do not restate raw metadata unless it matters."
    ),
    "2": (
        "You summarize file-level code notes for retrieval. "
        "Explain the file's purpose, the main responsibilities it owns, how its "
        "exports fit together, and the most relevant dependencies. "
        "Use child symbol summaries as supporting context when provided. "
        "Keep the summary concise, factual, and specific. "
        "Do not emit a heading."
    ),
    "3": (
        "You summarize directory-level code module notes for retrieval. "
        "Explain the module's role, the responsibilities grouped here, and how "
        "the contained files fit together architecturally. "
        "Use child file summaries as supporting context when provided. "
        "Keep the summary concise, factual, and specific. "
        "Do not emit a heading."
    ),
}
_TIER_LIMITS: dict[str, dict[str, int]] = {
    "1": {"max_tokens": 384, "child_chars": 0},
    "2": {"max_tokens": 512, "child_chars": 12000},
    "3": {"max_tokens": 768, "child_chars": 16000},
}


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _parse_scalar(value: str) -> Any:
    raw = value.strip()
    if not raw:
        return ""
    if raw[0] == raw[-1] and raw[0] in ('"', "'") and len(raw) >= 2:
        return raw[1:-1]
    lowered = raw.lower()
    if lowered == "true":
        return True
    if lowered == "false":
        return False
    return raw


def _parse_frontmatter(text: str) -> tuple[dict[str, Any], str, str]:
    match = _FRONTMATTER_RE.match(text)
    if not match:
        return {}, "", text

    frontmatter: dict[str, Any] = {}
    lines = match.group(1).splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        if not line.strip():
            i += 1
            continue
        if ":" not in line:
            i += 1
            continue
        key, _, remainder = line.partition(":")
        key = key.strip()
        remainder = remainder.strip()
        if remainder:
            frontmatter[key] = _parse_scalar(remainder)
            i += 1
            continue

        items: list[str] = []
        i += 1
        while i < len(lines):
            nested = lines[i]
            stripped = nested.strip()
            if not stripped:
                i += 1
                continue
            if nested.startswith("  - "):
                items.append(_parse_scalar(stripped[2:].strip()))
                i += 1
                continue
            if nested.startswith(" ") or nested.startswith("\t"):
                i += 1
                continue
            break
        frontmatter[key] = items

    return frontmatter, text[: match.end()], text[match.end():]


def _split_summary(body: str) -> tuple[str, str, dict[str, str], str]:
    marker = f"\n{_SUMMARY_HEADER}"
    idx = body.find(marker)
    if idx == -1:
        stripped = body.rstrip()
        return stripped, "", {}, ""

    before = body[:idx].rstrip()
    summary_section = body[idx + 1 :].strip()
    meta_match = _SUMMARY_META_RE.search(summary_section)
    meta: dict[str, str] = {}
    if meta_match:
        for line in meta_match.group(1).splitlines():
            if ":" not in line:
                continue
            key, _, value = line.partition(":")
            meta[key.strip()] = value.strip()
        summary_text = (summary_section[: meta_match.start()] + summary_section[meta_match.end() :]).strip()
    else:
        summary_text = summary_section

    if summary_text.startswith(_SUMMARY_HEADER):
        summary_text = summary_text[len(_SUMMARY_HEADER) :].strip()

    return before, summary_section, meta, summary_text


def _render_summary_section(summary_text: str, meta: dict[str, str]) -> str:
    meta_lines = "\n".join(f"{key}: {value}" for key, value in meta.items())
    return (
        f"{_SUMMARY_HEADER}\n\n"
        f"{_SUMMARY_META_START}\n"
        f"{meta_lines}\n"
        f"-->\n\n"
        f"{summary_text.strip()}\n"
    )


@dataclass
class NoteRecord:
    file_path: Path
    relative_path: str
    link_target: str
    tier: str
    frontmatter: dict[str, Any]
    frontmatter_block: str
    body_without_summary: str
    summary_section: str
    summary_meta: dict[str, str]
    summary_text: str

    @property
    def source_hash(self) -> str:
        return _sha256(self.body_without_summary.strip())


@dataclass
class TierStats:
    summarized: int = 0
    skipped: int = 0
    failed: int = 0


class CodeNoteSummarizer:
    def __init__(self, *, data_dir: Path) -> None:
        self._data_dir = data_dir

    async def summarize_workspace(
        self,
        *,
        workspace_name: str,
        log_fn: Callable[[str], None],
    ) -> dict[str, Any]:
        return await asyncio.to_thread(self._summarize_workspace_sync, workspace_name, log_fn)

    def _summarize_workspace_sync(self, workspace_name: str, log_fn: Callable[[str], None]) -> dict[str, Any]:
        summary_config = get_code_summary_config()
        if not summary_config.get("enabled"):
            log_fn("Summary stage skipped: code-note summarization is disabled.")
            return {"summarized": {"1": 0, "2": 0, "3": 0}, "skipped": {"1": 0, "2": 0, "3": 0}}

        synth_config = dict(summary_config)
        if not synth_config.get("provider") or not synth_config.get("model"):
            log_fn("Summary stage skipped: no LLM provider/model configured.")
            return {"summarized": {"1": 0, "2": 0, "3": 0}, "skipped": {"1": 0, "2": 0, "3": 0}}

        mirror_root = self._data_dir / "md_db" / "code" / workspace_name
        if not mirror_root.exists():
            log_fn(f"Summary stage skipped: mirror directory missing for {workspace_name}.")
            return {"summarized": {"1": 0, "2": 0, "3": 0}, "skipped": {"1": 0, "2": 0, "3": 0}}

        notes = self._load_notes(mirror_root=mirror_root, workspace_name=workspace_name)
        if not notes:
            log_fn(f"Summary stage skipped: no notes found for {workspace_name}.")
            return {"summarized": {"1": 0, "2": 0, "3": 0}, "skipped": {"1": 0, "2": 0, "3": 0}}

        children_by_file = self._build_children_by_parent(notes, child_tier="1", parent_key="parentFile")
        children_by_module = self._build_children_by_parent(notes, child_tier="2", parent_key="parentModule")

        tier_stats: dict[str, TierStats] = {"1": TierStats(), "2": TierStats(), "3": TierStats()}

        tier1 = self._process_tier(
            tier="1",
            notes=sorted((n for n in notes.values() if n.tier == "1"), key=lambda n: n.link_target),
            child_map={},
            synth_config=synth_config,
        )
        tier_stats["1"] = tier1
        log_fn(f"Tier 1 summaries: wrote {tier1.summarized}, skipped {tier1.skipped}.")
        if tier1.failed:
            raise RuntimeError(f"Tier 1 summarization failed for {tier1.failed} notes; tier 2 skipped.")

        tier2 = self._process_tier(
            tier="2",
            notes=sorted((n for n in notes.values() if n.tier == "2"), key=lambda n: n.link_target),
            child_map=children_by_file,
            synth_config=synth_config,
        )
        tier_stats["2"] = tier2
        log_fn(f"Tier 2 summaries: wrote {tier2.summarized}, skipped {tier2.skipped}.")
        if tier2.failed:
            raise RuntimeError(f"Tier 2 summarization failed for {tier2.failed} notes; tier 3 skipped.")

        tier3 = self._process_tier(
            tier="3",
            notes=sorted((n for n in notes.values() if n.tier == "3"), key=lambda n: n.link_target),
            child_map=children_by_module,
            synth_config=synth_config,
        )
        tier_stats["3"] = tier3
        log_fn(f"Tier 3 summaries: wrote {tier3.summarized}, skipped {tier3.skipped}.")
        if tier3.failed:
            raise RuntimeError(f"Tier 3 summarization failed for {tier3.failed} notes.")

        return {
            "summarized": {tier: stats.summarized for tier, stats in tier_stats.items()},
            "skipped": {tier: stats.skipped for tier, stats in tier_stats.items()},
        }

    def _load_notes(self, *, mirror_root: Path, workspace_name: str) -> dict[str, NoteRecord]:
        notes: dict[str, NoteRecord] = {}
        prefix = f"code/{workspace_name}"
        for md_path in sorted(mirror_root.rglob("*.md")):
            relative = md_path.relative_to(mirror_root).as_posix()
            text = md_path.read_text("utf-8")
            frontmatter, frontmatter_block, body = _parse_frontmatter(text)
            tier = str(frontmatter.get("tier", "")).strip()
            if tier not in {"1", "2", "3"}:
                continue
            body_without_summary, summary_section, summary_meta, summary_text = _split_summary(body)
            link_target = f"{prefix}/{relative[:-3]}"
            notes[link_target] = NoteRecord(
                file_path=md_path,
                relative_path=relative,
                link_target=link_target,
                tier=tier,
                frontmatter=frontmatter,
                frontmatter_block=frontmatter_block,
                body_without_summary=body_without_summary,
                summary_section=summary_section,
                summary_meta=summary_meta,
                summary_text=summary_text,
            )
        return notes

    def _build_children_by_parent(
        self,
        notes: dict[str, NoteRecord],
        *,
        child_tier: str,
        parent_key: str,
    ) -> dict[str, list[NoteRecord]]:
        child_map: dict[str, list[NoteRecord]] = {}
        for note in notes.values():
            if note.tier != child_tier:
                continue
            parent_link = str(note.frontmatter.get(parent_key, "")).strip()
            if not parent_link:
                continue
            child_map.setdefault(parent_link, []).append(note)
        for values in child_map.values():
            values.sort(key=lambda note: note.link_target)
        return child_map

    def _process_tier(
        self,
        *,
        tier: str,
        notes: list[NoteRecord],
        child_map: dict[str, list[NoteRecord]],
        synth_config: dict[str, Any],
    ) -> TierStats:
        stats = TierStats()
        for note in notes:
            children = child_map.get(note.link_target, [])
            child_hash = self._compute_child_hash(children)
            if not self._needs_resummary(note=note, tier=tier, child_hash=child_hash, synth_config=synth_config):
                stats.skipped += 1
                continue

            try:
                summary_text = self._summarize_note(
                    note=note,
                    tier=tier,
                    children=children,
                    synth_config=synth_config,
                )
            except Exception as exc:
                stats.failed += 1
                log.warning("Summary generation failed for %s: %s", note.file_path, exc)
                continue

            meta = {
                "tier": tier,
                "provider": str(synth_config.get("provider", "")),
                "model": str(synth_config.get("model", "")),
                "prompt_version": _PROMPT_VERSION,
                "source_hash": note.source_hash,
                "child_hash": child_hash,
                "summary_hash": _sha256(summary_text.strip()),
                "generated_at": datetime.now(timezone.utc).isoformat(),
            }
            note.summary_text = summary_text.strip()
            note.summary_meta = meta
            note.summary_section = _render_summary_section(note.summary_text, meta)
            self._write_note(note)
            stats.summarized += 1
        return stats

    def _compute_child_hash(self, children: list[NoteRecord]) -> str:
        payload = []
        for child in children:
            payload.append(child.link_target)
            payload.append(child.summary_text.strip())
        return _sha256("\n".join(payload))

    def _needs_resummary(
        self,
        *,
        note: NoteRecord,
        tier: str,
        child_hash: str,
        synth_config: dict[str, Any],
    ) -> bool:
        meta = note.summary_meta
        if not note.summary_text.strip():
            return True
        if meta.get("tier") != tier:
            return True
        if meta.get("provider") != str(synth_config.get("provider", "")):
            return True
        if meta.get("model") != str(synth_config.get("model", "")):
            return True
        if meta.get("prompt_version") != _PROMPT_VERSION:
            return True
        if meta.get("source_hash") != note.source_hash:
            return True
        if meta.get("child_hash") != child_hash:
            return True
        if meta.get("summary_hash") != _sha256(note.summary_text.strip()):
            return True
        return False

    def _summarize_note(
        self,
        *,
        note: NoteRecord,
        tier: str,
        children: list[NoteRecord],
        synth_config: dict[str, Any],
    ) -> str:
        child_context = self._render_child_context(tier=tier, children=children)
        user_parts = [
            f"Tier: {tier}",
            "",
            "Primary note:",
            "",
            note.body_without_summary.strip(),
        ]
        if child_context:
            user_parts.extend(["", "Child summaries:", "", child_context])
        user_parts.extend(
            [
                "",
                "Write a compact retrieval-oriented summary in Markdown prose.",
                "Prefer one short paragraph, optionally followed by a few flat bullets if they add signal.",
                "Do not include a heading.",
            ]
        )

        request_config = dict(synth_config)
        request_config["max_tokens"] = min(
            int(synth_config.get("max_tokens", _TIER_LIMITS[tier]["max_tokens"])),
            _TIER_LIMITS[tier]["max_tokens"],
        )
        return self._llm_chat(
            messages=[
                {"role": "system", "content": _SYSTEM_PROMPTS[tier]},
                {"role": "user", "content": "\n".join(user_parts)},
            ],
            synth_config=request_config,
        ).strip()

    def _render_child_context(self, *, tier: str, children: list[NoteRecord]) -> str:
        budget = _TIER_LIMITS[tier]["child_chars"]
        if budget <= 0 or not children:
            return ""

        chunks: list[str] = []
        used = 0
        for child in children:
            text = child.summary_text.strip()
            if not text:
                raise RuntimeError(f"Missing child summary for {child.file_path}")
            chunk = f"### {child.link_target}\n{text}\n"
            if used >= budget:
                break
            remaining = budget - used
            if len(chunk) > remaining:
                chunk = chunk[:remaining].rstrip() + "\n"
            chunks.append(chunk)
            used += len(chunk)
        return "\n".join(chunks).strip()

    def _write_note(self, note: NoteRecord) -> None:
        content = (
            note.frontmatter_block
            + note.body_without_summary.rstrip()
            + "\n\n"
            + note.summary_section.rstrip()
            + "\n"
        )
        note.file_path.write_text(content, "utf-8")

    def _llm_chat(self, *, messages: list[dict[str, str]], synth_config: dict[str, Any]) -> str:
        provider = str(synth_config.get("provider", "ollama")).lower()
        model = str(synth_config.get("model", ""))
        host = str(synth_config.get("host", "")).rstrip("/").removesuffix("/v1")
        api_key = str(synth_config.get("api_key", ""))
        max_tokens = int(synth_config.get("max_tokens", 512))
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
            headers = {"Content-Type": "application/json"}
            if api_key:
                headers["Authorization"] = f"Bearer {api_key}"
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
                "anthropic-version": "2023-06-01",
            }
            if api_key:
                headers["x-api-key"] = api_key
        else:
            raise ValueError(f"Unknown LLM provider: {provider!r}")

        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(url, data=data, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=30.0) as resp:
                result = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", errors="replace")
            raise RuntimeError(f"LLM request failed with HTTP {exc.code}: {body}") from exc
        except urllib.error.URLError as exc:
            raise RuntimeError(f"LLM request failed: {exc}") from exc

        if provider == "ollama":
            return result["message"]["content"]
        if provider == "openai":
            return result["choices"][0]["message"]["content"]
        return result["content"][0]["text"]
