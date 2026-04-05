/**
 * Wikilink parsing utilities — both simple extraction and rich parsing.
 *
 * Two levels of detail:
 *   - extractWikilinks(): just target strings, deduplicated. For ingestion.
 *   - parseWikiLinks(): full WikiLink objects with position, alias, anchor. For validation.
 */

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export interface WikiLink {
  /** The target file/note being linked to */
  target: string;

  /** Display text (alias) if using [[target|alias]] syntax, null otherwise */
  alias: string | null;

  /** Anchor/section reference if using [[target#section]] syntax, null otherwise */
  anchor: string | null;

  /** Raw link text as it appears in the source */
  raw: string;

  /** Character position where link starts in the source */
  position: number;

  /** Line number where link appears (1-indexed) */
  line: number;
}

export interface ParsedLinks {
  /** All valid wikilinks found in the content */
  links: WikiLink[];

  /** Raw link texts that couldn't be parsed properly */
  malformed: string[];
}

// ---------------------------------------------------------------------------
// Simple extraction (for ingestion)
// ---------------------------------------------------------------------------

/**
 * Extract [[wikilink]] targets from body text.
 * Handles [[note|alias]] — captures only the part before the pipe.
 * Deduplicates and filters empty captures.
 */
export function extractWikilinks(body: string): string[] {
  const seen = new Set<string>();
  const pattern = /\[\[([^|\]]+)(?:\|[^\]]+)?\]\]/g;
  let match: RegExpExecArray | null;

  while ((match = pattern.exec(body)) !== null) {
    const linkText = match[1]?.trim();
    if (linkText) seen.add(linkText);
  }

  return [...seen];
}

// ---------------------------------------------------------------------------
// Rich parsing (for validation / link graph)
// ---------------------------------------------------------------------------

/**
 * Extract all wikilinks from markdown content with detailed metadata.
 *
 * Handles:
 * - Basic links: [[target]]
 * - Aliased links: [[target|alias]]
 * - Anchor links: [[target#section]]
 * - Combined: [[target#section|alias]]
 * - Malformed links (reports them)
 */
export function parseWikiLinks(content: string, sourceFile?: string): ParsedLinks {
  const links: WikiLink[] = [];
  const malformed: string[] = [];

  const wikilinkPattern = /\[\[([^\]]+)\]\]/g;
  let match: RegExpExecArray | null;

  while ((match = wikilinkPattern.exec(content)) !== null) {
    const raw = match[0];
    const inner = match[1];
    const position = match.index;

    if (!inner?.trim()) {
      malformed.push(raw);
      continue;
    }

    try {
      const parsed = parseWikiLinkInner(inner.trim(), raw, position, content);
      if (parsed) {
        links.push(parsed);
      } else {
        malformed.push(raw);
      }
    } catch {
      malformed.push(raw);
      if (sourceFile) {
        console.warn(`[link-parser] Malformed wikilink in ${sourceFile}: ${raw}`);
      }
    }
  }

  return { links, malformed };
}

/**
 * Extract simple target list from parsed WikiLinks.
 * Strips anchors and aliases to return just the target file names.
 */
export function extractSimpleTargets(links: WikiLink[]): string[] {
  const seen = new Set<string>();

  for (const link of links) {
    if (link.target.trim()) {
      seen.add(link.target.trim());
    }
  }

  return [...seen];
}

/**
 * Validate wikilink syntax without parsing full structure.
 * Returns true if the link appears to be well-formed.
 */
export function isValidWikiLinkSyntax(linkText: string): boolean {
  if (!linkText.startsWith("[[") || !linkText.endsWith("]]")) {
    return false;
  }

  const inner = linkText.slice(2, -2).trim();
  if (!inner) {
    return false;
  }

  // Nested brackets not allowed
  if (inner.includes("[[") || inner.includes("]]")) {
    return false;
  }

  return true;
}

// ---------------------------------------------------------------------------
// Internal
// ---------------------------------------------------------------------------

function parseWikiLinkInner(
  inner: string,
  raw: string,
  position: number,
  fullContent: string,
): WikiLink | null {
  if (!inner) return null;

  // Calculate line number from position
  const beforePosition = fullContent.slice(0, position);
  const line = beforePosition.split("\n").length;

  // Split on pipe first to handle alias
  const parts = inner.split("|");
  const targetPart = parts[0]?.trim();
  const alias = parts.length > 1 ? parts.slice(1).join("|").trim() || null : null;

  if (!targetPart) return null;

  // Split target part on # to handle anchor
  const anchorSplit = targetPart.split("#");
  const target = anchorSplit[0]?.trim();
  const anchor = anchorSplit.length > 1 ? anchorSplit.slice(1).join("#").trim() || null : null;

  if (!target) return null;

  return { target, alias, anchor, raw, position, line };
}
