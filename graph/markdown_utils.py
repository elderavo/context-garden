"""Markdown utilities — Python port of shared/markdown/.

compute_md_db_hash() uses content-based hashing (MD5 of each file's bytes)
to detect changes. This is immune to non-semantic modifications like
frontmatter field reordering or linter rewrites that don't change meaning.
"""

from __future__ import annotations

import hashlib
import os
import re
from pathlib import Path
from typing import Optional

# ---------------------------------------------------------------------------
# Frontmatter
# ---------------------------------------------------------------------------


def parse_frontmatter(content: str) -> tuple[dict, str]:
    """Parse YAML-ish frontmatter delimited by ``---``.

    Returns (frontmatter_dict, body_string).
    """
    lines = content.split("\n")

    # Must start with ---
    if not lines or lines[0].strip() != "---":
        return {}, content

    # Find closing ---
    close_idx = -1
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            close_idx = i
            break

    if close_idx == -1:
        return {}, content

    fm_lines = lines[1:close_idx]
    body = "\n".join(lines[close_idx + 1 :]).lstrip()
    return _parse_frontmatter_lines(fm_lines), body


def _parse_frontmatter_lines(lines: list[str]) -> dict:
    result: dict = {}
    i = 0
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()

        # Skip empty / comment lines
        if not stripped or stripped.startswith("#"):
            i += 1
            continue

        colon_pos = stripped.find(":")
        if colon_pos <= 0:
            i += 1
            continue

        key = stripped[:colon_pos].strip()
        raw_value = stripped[colon_pos + 1 :].strip()

        if raw_value:
            # Inline value
            result[key] = raw_value
            i += 1
            continue

        # Block value — collect indented lines
        i += 1
        list_items: list[str] = []
        nested: dict = {}
        while i < len(lines):
            block_line = lines[i]
            if not block_line.startswith("  ") and not block_line.startswith("\t"):
                break
            bl_stripped = block_line.strip()
            if bl_stripped.startswith("- "):
                list_items.append(bl_stripped[2:])
            elif ":" in bl_stripped:
                sub_colon = bl_stripped.find(":")
                if sub_colon > 0:
                    sub_key = bl_stripped[:sub_colon].strip()
                    sub_val = bl_stripped[sub_colon + 1 :].strip()
                    nested[sub_key] = sub_val if sub_val else None
            i += 1

        if list_items:
            result[key] = list_items
        elif nested:
            result[key] = nested
        else:
            result[key] = None

    return result


def build_frontmatter(fields: dict) -> str:
    """Build canonical YAML frontmatter string from a dict."""
    lines = ["---"]
    for key, value in fields.items():
        if value is None:
            lines.append(f"{key}:")
        elif isinstance(value, list):
            lines.append(f"{key}:")
            for item in value:
                lines.append(f"    - {item}")
        elif isinstance(value, dict):
            lines.append(f"{key}:")
            for sub_key, sub_val in value.items():
                if sub_val is None:
                    lines.append(f"    {sub_key}:")
                else:
                    lines.append(f"    {sub_key}: {sub_val}")
        else:
            lines.append(f"{key}: {value}")
    lines.append("---")
    return "\n".join(lines) + "\n"


# ---------------------------------------------------------------------------
# Wikilinks
# ---------------------------------------------------------------------------

_SIMPLE_WIKILINK_RE = re.compile(r"\[\[([^|\]]+)(?:\|[^\]]+)?\]\]")
_RICH_WIKILINK_RE = re.compile(r"\[\[([^\]]+)\]\]")


def extract_wikilinks(body: str) -> list[str]:
    """Extract deduplicated wikilink targets (simple extraction)."""
    matches = _SIMPLE_WIKILINK_RE.findall(body)
    seen: set[str] = set()
    result: list[str] = []
    for m in matches:
        target = m.strip()
        if target and target not in seen:
            seen.add(target)
            result.append(target)
    return result


# ---------------------------------------------------------------------------
# Tokens
# ---------------------------------------------------------------------------

_NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")


def normalize_token(value: str) -> str:
    """Lowercase, replace non-alphanumeric runs with ``_``, strip edges."""
    return _NON_ALNUM_RE.sub("_", value.lower()).strip("_")


def normalize_tag_list(tags: list[str]) -> list[str]:
    """Normalize and deduplicate a list of tag strings."""
    seen: set[str] = set()
    result: list[str] = []
    for t in tags:
        n = normalize_token(t)
        if n and n not in seen:
            seen.add(n)
            result.append(n)
    return result


def extract_tags(frontmatter: dict | None) -> list[str]:
    """Extract normalised tags from a frontmatter dict."""
    if not frontmatter:
        return []
    raw = frontmatter.get("tags")
    if isinstance(raw, list):
        return normalize_tag_list([str(t) for t in raw])
    if isinstance(raw, str):
        parts = [t.strip() for t in raw.split(",") if t.strip()]
        return normalize_tag_list(parts)
    return []


# ---------------------------------------------------------------------------
# Note type inference
# ---------------------------------------------------------------------------

CANONICAL_NOTE_TYPES = {
    "tool": "tool",
    "concept": "concept",
    "index": "index",
    "codebase": "codebase",
    "codeunit": "codeUnit",
    # Three-tier code note types
    "codesymbol": "codeSymbol",  # tier-1: individual exported symbol
    "codemodule": "codeModule",  # tier-3: directory-level module
}


def infer_note_type(
    frontmatter: dict, relative_path: str
) -> str:
    """Infer note type from frontmatter > path prefix > filename > default."""
    for key in ("type", "note_type"):
        val = frontmatter.get(key)
        if val and isinstance(val, str):
            normalized = val.lower().strip()
            if normalized in CANONICAL_NOTE_TYPES:
                return CANONICAL_NOTE_TYPES[normalized]

    # Normalise slashes for path prefix checks
    norm_path = relative_path.replace("\\", "/")
    if norm_path.startswith("tools/"):
        return "tool"
    if norm_path.startswith("concepts/"):
        return "concept"

    stem = Path(relative_path).stem.lower()
    if stem == "index":
        return "index"

    return "concept"


def extract_tier(frontmatter: dict) -> str:
    """Extract the tier field from frontmatter. Returns '1', '2', '3', or ''."""
    val = frontmatter.get("tier")
    if val is not None:
        s = str(val).strip()
        if s in ("1", "2", "3"):
            return s
    return ""


def extract_parent_file(frontmatter: dict) -> str:
    """Extract parentFile wikilink path (tier-1 notes only)."""
    val = frontmatter.get("parentFile")
    return str(val).strip() if val else ""


def extract_parent_module(frontmatter: dict) -> str:
    """Extract parentModule wikilink path (tier-1 and tier-2 notes)."""
    val = frontmatter.get("parentModule")
    return str(val).strip() if val else ""


def extract_symbol_kind(frontmatter: dict) -> str:
    """Extract symbolKind field (tier-1 notes only)."""
    val = frontmatter.get("symbolKind")
    return str(val).strip() if val else ""


def extract_workspace(frontmatter: dict) -> str:
    """Extract workspace name from frontmatter. Returns '' if absent."""
    val = frontmatter.get("workspace")
    return str(val).strip() if val else ""


# ---------------------------------------------------------------------------
# Title extraction
# ---------------------------------------------------------------------------

_HEADING_RE = re.compile(r"^#\s+(.+)", re.MULTILINE)


def extract_title(frontmatter: dict, body: str, relative_path: str) -> str:
    """Extract title: frontmatter > first # heading > filename stem."""
    fm_title = frontmatter.get("title")
    if fm_title and isinstance(fm_title, str) and fm_title.strip():
        return fm_title.strip()

    m = _HEADING_RE.search(body)
    if m:
        return m.group(1).strip()

    stem = Path(relative_path).stem
    return re.sub(r"[-_]", " ", stem).title()


# ---------------------------------------------------------------------------
# md_db hash (MUST match TS byte-for-byte)
# ---------------------------------------------------------------------------

IGNORED_DIRS = frozenset({".obsidian", "node_modules", ".venv", "__pycache__"})


def collect_markdown_files(md_db_path: str) -> list[str]:
    """Recursively collect all .md files, skipping .obsidian."""
    paths: list[str] = []
    for root, dirs, files in os.walk(md_db_path):
        dirs[:] = [d for d in dirs if d not in IGNORED_DIRS]
        for f in files:
            if f.endswith(".md"):
                paths.append(os.path.join(root, f))
    return paths


def compute_md_db_hash(md_db_path: str) -> str:
    """MD5 fingerprint of all .md files by path + content hash.

    Uses file contents instead of mtimes so that non-semantic changes
    (frontmatter field reordering, linter rewrites) don't trigger
    unnecessary re-embedding.
    """
    file_paths = collect_markdown_files(md_db_path)
    entries: list[str] = []
    for p in file_paths:
        try:
            content = open(p, "rb").read()
            file_hash = hashlib.md5(content).hexdigest()
        except OSError:
            continue
        entries.append(f"{p}:{file_hash}")

    entries.sort()
    joined = "|".join(entries)
    return hashlib.md5(joined.encode("utf-8")).hexdigest()
