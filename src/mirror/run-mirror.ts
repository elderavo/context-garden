/**
 * run-mirror.ts
 *
 * Language-agnostic orchestrator for the full mirror + cleanup pipeline.
 *
 * Responsibilities:
 *   1. Run each registered language mirror for the configured workspace.
 *   2. Union the validPaths sets returned by every language mirror.
 *   3. Call cleanMirrorDir exactly once with the combined set so stale notes
 *      are removed regardless of which language produced them.
 *
 * Safety contract:
 *   If any configured language mirror throws, cleanup is skipped for that
 *   run. An incomplete validPaths set would cause the other language's notes
 *   to be incorrectly pruned. The caller still gets accurate written counts
 *   for the languages that succeeded.
 */

import { cleanMirrorDir } from "./cleanup.js";
import {
  ensureDefaultLanguages,
  getLanguage,
  type WorkspaceLanguage,
  type LanguageMirrorOptions,
} from "./language-registry.js";

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export interface WorkspaceMirrorRunOptions {
  /** Absolute path to the source directory to scan. */
  scanDir: string;
  /** Absolute path to the mirror output directory (e.g. md_db/code/{name}). */
  mirrorDir: string;
  /** Which languages to mirror. */
  languages: WorkspaceLanguage[];
  /** Force-regenerate all notes even if mirror is up-to-date. */
  force: boolean;
  /** Workspace name — written into note frontmatter. */
  workspace?: string;
  /** Wikilink prefix for cross-note references (e.g. "code/myproject"). */
  wikilinkPrefix?: string;
  /** Per-language omit pattern overrides. Falls back to descriptor defaults. */
  omitPatterns?: Partial<Record<WorkspaceLanguage, string[]>>;
}

export interface WorkspaceMirrorRunResult {
  /** Number of notes written per language. */
  written: Record<WorkspaceLanguage, number>;
  /** Number of skipped (up-to-date) notes per language. */
  skipped: Record<WorkspaceLanguage, number>;
  /** Number of stale notes deleted from mirrorDir. 0 if cleanup was skipped. */
  cleaned: number;
}

// ---------------------------------------------------------------------------
// Orchestrator
// ---------------------------------------------------------------------------

export async function runWorkspaceMirror(
  opts: WorkspaceMirrorRunOptions,
): Promise<WorkspaceMirrorRunResult> {
  ensureDefaultLanguages();

  const combinedValidPaths = new Set<string>();
  const written: Record<WorkspaceLanguage, number> = {};
  const skipped: Record<WorkspaceLanguage, number> = {};
  let allSucceeded = true;

  for (const languageId of opts.languages) {
    const descriptor = getLanguage(languageId);
    if (!descriptor) {
      allSucceeded = false;
      console.warn(
        `[mirror-workspace] Unknown language "${languageId}" for ${opts.workspace ?? opts.scanDir}`,
      );
      continue;
    }

    written[languageId] = 0;
    skipped[languageId] = 0;

    const mirrorOptions: LanguageMirrorOptions = {
      scanDir: opts.scanDir,
      mirrorDir: opts.mirrorDir,
      omitPatterns: opts.omitPatterns?.[languageId] ?? descriptor.defaultOmit,
      force: opts.force,
      workspace: opts.workspace,
      wikilinkPrefix: opts.wikilinkPrefix,
    };

    try {
      const result = await descriptor.runMirror(mirrorOptions);
      written[languageId] = result.written;
      skipped[languageId] = result.skipped;
      for (const p of result.validPaths) combinedValidPaths.add(p);
    } catch (err) {
      allSucceeded = false;
      console.warn(
        `[mirror-workspace] ${descriptor.displayName} mirror failed for ${opts.workspace ?? opts.scanDir}: ${err}`,
      );
    }
  }

  // Only prune stale notes when all enabled mirrors succeeded.
  let cleaned = 0;
  if (allSucceeded && combinedValidPaths.size > 0) {
    cleaned = cleanMirrorDir(opts.mirrorDir, combinedValidPaths);
  }

  return { written, skipped, cleaned };
}
