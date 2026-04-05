/**
 * md_db normalizer — scans markdown files for formatting issues and optionally
 * auto-fixes safe inconsistencies.
 *
 * Safe auto-fixes:
 *   - Reformat frontmatter to canonical YAML (dash-list tags, consistent spacing)
 *   - Add missing `type` field inferred from path prefix
 *
 * Report-only (not auto-fixed):
 *   - Missing required frontmatter fields (tags)
 *   - Broken [[wikilinks]] (target file doesn't exist)
 *   - Malformed frontmatter (can't be parsed)
 */

import { join, relative, extname, dirname } from "path";
import { readText, writeText, fileExists, listDir } from "../util/fs.js";
import { parseFrontmatter, buildFrontmatter } from "./frontmatter.js";
import { extractWikilinks } from "./wikilinks.js";

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export interface NormalizationIssue {
  /** Relative path from mdDbPath. */
  path: string;
  type: "missing_field" | "format_inconsistency" | "broken_link" | "malformed_frontmatter";
  description: string;
  autoFixable: boolean;
}

export interface NormalizationResult {
  /** Number of .md files scanned. */
  scanned: number;
  /** All issues found. */
  issues: NormalizationIssue[];
  /** Number of files auto-fixed (when fix: true). */
  fixed: number;
}

export interface NormalizeOptions {
  /** When true, auto-fix safe issues (frontmatter reformatting, missing type). Default: false. */
  fix?: boolean;
  /** Directory names to skip during traversal. Default: [".obsidian"]. */
  ignoredDirs?: string[];
}

// ---------------------------------------------------------------------------
// Public API
// ---------------------------------------------------------------------------

/**
 * Scan md_db for formatting issues and optionally auto-fix safe ones.
 */
export function normalizeMdDb(
  mdDbPath: string,
  options?: NormalizeOptions,
): NormalizationResult {
  const fix = options?.fix ?? false;
  const ignoredDirs = new Set(options?.ignoredDirs ?? [".obsidian"]);

  const mdFiles = collectMdFiles(mdDbPath, mdDbPath, ignoredDirs);
  const allPaths = new Set(mdFiles.map((f) => f.relativePath));

  const issues: NormalizationIssue[] = [];
  let fixed = 0;

  for (const file of mdFiles) {
    const fileIssues = analyzeFile(file.absolutePath, file.relativePath, allPaths);
    issues.push(...fileIssues);

    if (fix && fileIssues.some((i) => i.autoFixable)) {
      const didFix = autoFix(file.absolutePath, file.relativePath);
      if (didFix) fixed++;
    }
  }

  return { scanned: mdFiles.length, issues, fixed };
}

// ---------------------------------------------------------------------------
// File discovery
// ---------------------------------------------------------------------------

interface MdFile {
  absolutePath: string;
  relativePath: string;
}

function collectMdFiles(
  dir: string,
  rootDir: string,
  ignoredDirs: Set<string>,
): MdFile[] {
  const results: MdFile[] = [];

  let entries: string[];
  try {
    entries = listDir(dir);
  } catch {
    return results;
  }

  for (const name of entries) {
    if (name.startsWith(".") && ignoredDirs.has(name)) continue;
    if (ignoredDirs.has(name)) continue;

    const fullPath = join(dir, name);

    // Check if directory by trying to list it
    try {
      const subEntries = listDir(fullPath);
      // It's a directory — recurse
      results.push(...collectMdFiles(fullPath, rootDir, ignoredDirs));
      void subEntries; // used as directory check
    } catch {
      // It's a file (or unreadable directory)
      if (extname(name) === ".md") {
        results.push({
          absolutePath: fullPath,
          relativePath: relative(rootDir, fullPath).replace(/\\/g, "/"),
        });
      }
    }
  }

  return results;
}

// ---------------------------------------------------------------------------
// Analysis
// ---------------------------------------------------------------------------

function analyzeFile(
  absPath: string,
  relPath: string,
  allPaths: Set<string>,
): NormalizationIssue[] {
  const issues: NormalizationIssue[] = [];

  let content: string;
  try {
    content = readText(absPath);
  } catch {
    issues.push({
      path: relPath,
      type: "malformed_frontmatter",
      description: "Could not read file",
      autoFixable: false,
    });
    return issues;
  }

  const { frontmatter, body } = parseFrontmatter(content);

  // Check frontmatter exists
  if (Object.keys(frontmatter).length === 0 && content.trimStart().startsWith("#")) {
    issues.push({
      path: relPath,
      type: "malformed_frontmatter",
      description: "No frontmatter block found",
      autoFixable: false,
    });
  }

  // Check required field: type
  if (!frontmatter["type"] && !frontmatter["note_type"]) {
    const inferredType = inferTypeFromPath(relPath);
    issues.push({
      path: relPath,
      type: "missing_field",
      description: `Missing 'type' field (would infer: ${inferredType})`,
      autoFixable: true,
    });
  }

  // Check frontmatter format consistency (inline arrays vs dash lists)
  const raw = content.split("\n");
  const closingIdx = findFrontmatterEnd(raw);
  if (closingIdx > 0) {
    const fmBlock = raw.slice(1, closingIdx).join("\n");
    // Detect inline array format like `tags: [a, b]`
    if (/\w+:\s*\[.+\]/.test(fmBlock)) {
      issues.push({
        path: relPath,
        type: "format_inconsistency",
        description: "Frontmatter uses inline array format instead of YAML dash lists",
        autoFixable: true,
      });
    }
  }

  // Check for broken wikilinks
  const links = extractWikilinks(body);
  for (const link of links) {
    const resolved = resolveWikilink(link, allPaths);
    if (!resolved) {
      issues.push({
        path: relPath,
        type: "broken_link",
        description: `Broken wikilink: [[${link}]]`,
        autoFixable: false,
      });
    }
  }

  return issues;
}

// ---------------------------------------------------------------------------
// Auto-fix
// ---------------------------------------------------------------------------

function autoFix(absPath: string, relPath: string): boolean {
  let content: string;
  try {
    content = readText(absPath);
  } catch {
    return false;
  }

  const { frontmatter, body } = parseFrontmatter(content);
  if (Object.keys(frontmatter).length === 0) return false;

  let changed = false;

  // Add missing type field
  if (!frontmatter["type"] && !frontmatter["note_type"]) {
    frontmatter["type"] = inferTypeFromPath(relPath);
    changed = true;
  }

  // Rebuild frontmatter in canonical format (always, to fix inline arrays)
  const canonical = buildFrontmatter(frontmatter);
  const newContent = canonical + body;

  // Only write if actually different
  if (newContent !== content) {
    writeText(absPath, newContent);
    return true;
  }

  return changed;
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function inferTypeFromPath(relPath: string): string {
  if (relPath.startsWith("tools/")) return "tool";
  if (relPath.startsWith("concepts/")) return "concept";
  const stem = relPath.split("/").pop()?.replace(/\.md$/, "")?.toLowerCase() ?? "";
  if (stem === "index") return "index";
  return "concept";
}

function findFrontmatterEnd(lines: string[]): number {
  if (lines[0]?.trim() !== "---") return -1;
  for (let i = 1; i < lines.length; i++) {
    if (lines[i]?.trim() === "---") return i;
  }
  return -1;
}

function resolveWikilink(linkText: string, allPaths: Set<string>): string | null {
  // Direct match
  if (allPaths.has(linkText)) return linkText;

  // With .md extension
  const withMd = linkText + ".md";
  if (allPaths.has(withMd)) return withMd;

  // Search for matching filename in any subdirectory
  const target = linkText.endsWith(".md") ? linkText : linkText + ".md";
  for (const path of allPaths) {
    if (path.endsWith("/" + target) || path === target) return path;
  }

  return null;
}
