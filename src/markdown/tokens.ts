/**
 * Token and tag normalization utilities for markdown notes.
 *
 * Used by hybrid retrieval (tag boosting), pruning (tag filtering),
 * and anywhere else that needs consistent token handling.
 */

// ---------------------------------------------------------------------------
// Token normalization
// ---------------------------------------------------------------------------

/** Lowercase, replace non-alphanumeric runs with _, trim leading/trailing _. */
export function normalizeToken(value: string): string {
  return value
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, "_")
    .replace(/^_+|_+$/g, "");
}

/** Split a string into normalized tokens on non-alphanumeric boundaries. */
export function normalizeTokens(value: string): string[] {
  return value
    .toLowerCase()
    .split(/[^a-z0-9]+/g)
    .map((token) => token.trim())
    .filter(Boolean);
}

// ---------------------------------------------------------------------------
// Tag extraction
// ---------------------------------------------------------------------------

/** Normalize and deduplicate a raw tag array. */
export function normalizeTagList(tags: string[]): string[] {
  const normalized = tags
    .map((tag) => normalizeToken(tag))
    .filter(Boolean);
  return [...new Set(normalized)];
}

/**
 * Extract and normalize tags from a frontmatter JSON string (as stored in SQLite).
 * Handles both array and comma-separated string formats.
 */
export function extractTags(frontmatterJson?: string | null): string[] {
  if (!frontmatterJson) return [];

  try {
    const parsed = JSON.parse(frontmatterJson) as Record<string, unknown>;
    const rawTags = parsed["tags"];

    if (Array.isArray(rawTags)) {
      return normalizeTagList(rawTags.map((t) => String(t)));
    }

    if (typeof rawTags === "string") {
      const parts = rawTags.split(",").map((tag) => tag.trim()).filter(Boolean);
      return normalizeTagList(parts);
    }
  } catch {
    return [];
  }

  return [];
}
