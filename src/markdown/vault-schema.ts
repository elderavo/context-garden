/**
 * Vault note taxonomy and frontmatter schema for workspace notes.
 *
 * Three note types, each with slightly different required frontmatter fields.
 * Used by: inbox linter, notetaker CLI, capture_note MCP tool.
 */

import { buildFrontmatter } from "./frontmatter.js";

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export type VaultNoteType = "permanent" | "synthesized" | "source";

export const VAULT_NOTE_TYPES: VaultNoteType[] = ["permanent", "synthesized", "source"];

export const NOTE_TYPE_DESCRIPTIONS: Record<VaultNoteType, string> = {
  permanent: "Permanent  — atomic concept or reference note",
  synthesized: "Synthesized  — combines or distills multiple notes",
  source: "Source  — literature note from a paper, book, or article",
};

// ---------------------------------------------------------------------------
// ID helpers
// ---------------------------------------------------------------------------

/** Generate a timestamp-based Zettelkasten ID: YYYYMMDDHHmm */
export function generateNoteId(date: Date = new Date()): string {
  const pad = (n: number, len = 2) => String(n).padStart(len, "0");
  return (
    String(date.getFullYear()) +
    pad(date.getMonth() + 1) +
    pad(date.getDate()) +
    pad(date.getHours()) +
    pad(date.getMinutes())
  );
}

// ---------------------------------------------------------------------------
// Frontmatter builder
// ---------------------------------------------------------------------------

/**
 * Build a YAML frontmatter block for a new vault note.
 * Includes all required fields for the given type.
 */
export function buildVaultFrontmatter(
  type: VaultNoteType,
  title: string,
  tags: string[] = [],
  id?: string,
  workspace?: string,
): string {
  const fields: Record<string, unknown> = {
    id: id ?? generateNoteId(),
    type,
    tags,
    ...(workspace ? { workspace } : {}),
  };

  if (type === "synthesized") {
    fields.source_notes = [];
  }
  if (type === "source") {
    fields.source = "";
    fields.author = "";
  }

  return buildFrontmatter(fields);
}

// ---------------------------------------------------------------------------
// Filename helpers
// ---------------------------------------------------------------------------

/** Slugify a title for use in filenames. */
export function slugifyTitle(title: string): string {
  return (
    title
      .toLowerCase()
      .replace(/[^a-z0-9\s-]/g, "")
      .trim()
      .replace(/\s+/g, "-")
      .slice(0, 60) || "untitled"
  );
}

/** Build a note filename: {slug}.md */
export function buildNoteFilename(title: string): string {
  return `${slugifyTitle(title)}.md`;
}

/** Build the full note file content (frontmatter + title heading + body). */
export function buildNoteContent(
  type: VaultNoteType,
  title: string,
  body: string,
  tags: string[] = [],
  links: string[] = [],
  workspace?: string,
): string {
  const fm = buildVaultFrontmatter(type, title, tags, undefined, workspace);
  const trimmedBody = body.trim();
  let content = fm + "\n# " + title + (trimmedBody ? "\n\n" + trimmedBody : "\n");

  if (links.length > 0) {
    content += "\n\n## Links\n\n" + links.map((l) => `- ${l}`).join("\n");
  }

  return content + "\n";
}
