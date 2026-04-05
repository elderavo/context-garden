/**
 * Frontmatter stripping utility for md_db notes.
 *
 * Obsidian notes commonly have YAML frontmatter (---) at the top that
 * contains metadata (tags, aliases, dates) not useful as agent context.
 * Strip it before injecting into the system prompt.
 */

/**
 * Remove YAML (--- ... ---) or TOML (+++ ... +++) frontmatter from markdown.
 * Also collapses runs of 3+ blank lines down to 2.
 */
export function stripFrontmatter(markdown: string): string {
  let s = markdown;

  // YAML frontmatter: must start at line 1
  s = s.replace(/^---\r?\n[\s\S]*?\r?\n---\r?\n?/, "");
  // TOML frontmatter
  s = s.replace(/^\+\+\+\r?\n[\s\S]*?\r?\n\+\+\+\r?\n?/, "");
  // Collapse excessive blank lines
  s = s.replace(/\n{3,}/g, "\n\n");

  return s.trim();
}

/** Rough token estimate: 1 token ≈ 4 chars (OpenAI tokenizer average). */
export function estimateTokens(text: string): number {
  return Math.ceil(text.length / 4);
}
