/**
 * Markdown linter / normalizer for md_db.
 *
 * Responsibilities:
 *   - Canonicalize frontmatter formatting (YAML dash lists via buildFrontmatter)
 *   - Normalize tag lists (lowercase, deduplicate)
 *   - Normalize markdown body whitespace (line endings, trailing spaces, final newline)
 *   - Validate wikilinks (report-only)
 *   - Support single-file linting for watcher-driven workflows
 *
 * The linter preserves all existing frontmatter fields as-is. It does NOT
 * inject id/uuid/created/updated/md_db — those are not used by the context
 * engine and add noise. Type inference only runs when `type` is missing.
 */

import { statSync } from "fs";
import { basename, extname, join, relative } from "path";

import { parseFrontmatter, buildFrontmatter } from "./frontmatter.js";
import { extractWikilinks } from "./wikilinks.js";
import { normalizeTagList } from "./tokens.js";
import { readText, writeText, listDir } from "../util/fs.js";

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export type LintIssueType =
  | "missing_field"
  | "format_inconsistency"
  | "broken_link"
  | "malformed_frontmatter";

export interface LintIssue {
  path: string; // relative to md_db
  type: LintIssueType;
  description: string;
  autoFixed: boolean;
}

export interface LintResult {
  scanned: number;
  fixed: number;
  issues: LintIssue[];
}

export interface LintOptions {
  /** When true, write fixes to disk. */
  fix?: boolean;
  /** Directories to skip (defaults: .obsidian, .context-garden). */
  ignoredDirs?: string[];
}

export interface SingleFileLintOptions extends LintOptions {
  /** Optional set of known note paths (relative) for wikilink validation. */
  allNotePaths?: Set<string>;
}

// ---------------------------------------------------------------------------
// Public API
// ---------------------------------------------------------------------------

export function lintMdDb(mdDbPath: string, options?: LintOptions): LintResult {
  const fix = options?.fix ?? false;
  const ignoredDirs = new Set(options?.ignoredDirs ?? [".obsidian", ".context-garden"]);

  const mdFiles = collectMdFiles(mdDbPath, mdDbPath, ignoredDirs);
  const allNotePaths = new Set(mdFiles.map((f) => f.relativePath));

  let scanned = 0;
  let fixed = 0;
  const issues: LintIssue[] = [];

  for (const file of mdFiles) {
    const result = lintFile(file.absolutePath, file.relativePath, { fix, allNotePaths });
    scanned += result.scanned;
    fixed += result.fixed;
    issues.push(...result.issues);
  }

  return { scanned, fixed, issues };
}

export function lintFile(
  absolutePath: string,
  relativePath: string,
  options?: SingleFileLintOptions,
): LintResult {
  const fix = options?.fix ?? false;
  const allNotePaths = options?.allNotePaths ?? new Set<string>([relativePath]);

  let content: string;
  try {
    content = readText(absolutePath);
  } catch {
    return {
      scanned: 1,
      fixed: 0,
      issues: [
        {
          path: relativePath,
          type: "malformed_frontmatter",
          description: "Could not read file",
          autoFixed: false,
        },
      ],
    };
  }

  const originalContent = content;
  const { frontmatter, body } = parseFrontmatter(content);

  let stats: ReturnType<typeof statSync> | undefined;
  try {
    stats = statSync(absolutePath);
  } catch {
    // Best-effort; fall back to now.
  }

  const { normalizedFrontmatter, normalizedBody, issues } = normalizeFile(
    frontmatter,
    body,
    relativePath,
    stats,
    allNotePaths,
  );

  const newFrontmatterBlock = buildFrontmatter(normalizedFrontmatter);
  const bodyWithFinalNewline = ensureFinalNewline(normalizedBody);
  const newContent = newFrontmatterBlock + bodyWithFinalNewline;

  const needsWrite = fix && newContent !== originalContent;
  if (needsWrite) {
    writeText(absolutePath, newContent);
  }

  return {
    scanned: 1,
    fixed: needsWrite ? 1 : 0,
    issues,
  };
}

// ---------------------------------------------------------------------------
// Core normalization
// ---------------------------------------------------------------------------

function normalizeFile(
  frontmatter: Record<string, unknown>,
  body: string,
  relativePath: string,
  _stats: ReturnType<typeof statSync> | undefined,
  allNotePaths: Set<string>,
): {
  normalizedFrontmatter: Record<string, unknown>;
  normalizedBody: string;
  issues: LintIssue[];
} {
  const issues: LintIssue[] = [];

  // Start with all existing fields preserved as-is
  const normalizedFrontmatter: Record<string, unknown> = { ...frontmatter };

  // Normalize type if present, infer from path if missing
  const noteType = normalizeType(frontmatter["type"], relativePath);
  if (!frontmatter["type"] || frontmatter["type"] !== noteType) {
    normalizedFrontmatter["type"] = noteType;
    if (!frontmatter["type"]) {
      issues.push({
        path: relativePath,
        type: "missing_field",
        description: `Missing type — inferred "${noteType}" from path`,
        autoFixed: true,
      });
    }
  }

  // Normalize tags (lowercase, deduplicate) if present
  if (frontmatter["tags"]) {
    normalizedFrontmatter["tags"] = normalizeTags(frontmatter["tags"]);
  }

  // Markdown body normalization
  const normalizedBody = normalizeBody(body);

  // Wikilink validation (report-only)
  const links = extractWikilinks(normalizedBody);
  for (const link of links) {
    const resolved = resolveWikilink(link, allNotePaths);
    if (!resolved) {
      issues.push({
        path: relativePath,
        type: "broken_link",
        description: `Broken wikilink: [[${link}]]`,
        autoFixed: false,
      });
    }
  }

  return { normalizedFrontmatter, normalizedBody, issues };
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

interface MdFile {
  absolutePath: string;
  relativePath: string;
}

function collectMdFiles(dir: string, rootDir: string, ignoredDirs: Set<string>): MdFile[] {
  const results: MdFile[] = [];

  let entries: string[];
  try {
    entries = listDir(dir);
  } catch {
    return results;
  }

  for (const name of entries) {
    if (ignoredDirs.has(name)) continue;
    if (name.startsWith(".")) {
      if (ignoredDirs.has(name)) continue;
    }

    const fullPath = join(dir, name);
    let isDir = false;
    try {
      // If listDir succeeds, it's a directory.
      listDir(fullPath);
      isDir = true;
    } catch {
      isDir = false;
    }

    if (isDir) {
      results.push(...collectMdFiles(fullPath, rootDir, ignoredDirs));
    } else if (extname(name) === ".md") {
      results.push({
        absolutePath: fullPath,
        relativePath: relative(rootDir, fullPath).replace(/\\/g, "/"),
      });
    }
  }

  return results;
}

function normalizeType(raw: unknown, relPath: string): string {
  if (typeof raw === "string" && raw.trim()) {
    return raw.trim();
  }
  return inferTypeFromPath(relPath);
}

function inferTypeFromPath(relPath: string): string {
  if (relPath.startsWith("tools/")) return "tool";
  const stem = basename(relPath, ".md").toLowerCase();
  if (stem === "index") return "index";
  if (relPath.toLowerCase().includes("rule")) return "rule";
  if (relPath.toLowerCase().includes("context")) return "context";
  return "concept";
}

function normalizeTags(raw: unknown): string[] {
  const tags: string[] = [];

  if (Array.isArray(raw)) {
    for (const item of raw) {
      const t = String(item).trim();
      if (t) tags.push(t);
    }
  } else if (typeof raw === "string") {
    const parts = raw.includes(",") ? raw.split(",") : raw.split(/\s+/);
    for (const part of parts) {
      const t = part.trim();
      if (t) tags.push(t);
    }
  }

  return normalizeTagList(tags);
}

function normalizeBody(body: string): string {
  let normalized = body.replace(/\r\n/g, "\n");
  const lines = normalized.split("\n").map((line) => line.replace(/[ \t]+$/, ""));
  normalized = lines.join("\n");
  return normalized;
}

function ensureFinalNewline(body: string): string {
  if (!body.endsWith("\n")) return body + "\n";
  return body;
}

function resolveWikilink(linkText: string, allPaths: Set<string>): string | null {
  // Direct match
  if (allPaths.has(linkText)) return linkText;

  const withMd = linkText.endsWith(".md") ? linkText : `${linkText}.md`;
  if (allPaths.has(withMd)) return withMd;

  // Search for matching filename in any subdirectory
  for (const path of allPaths) {
    if (path.endsWith(`/${withMd}`) || path === withMd) return path;
  }

  return null;
}
