/**
 * Mirror watcher — watches source files and regenerates mirrors on change.
 *
 * Stripped of all summarizer spawning. ContextGarden does not run summarizers;
 * mirrors are the final output of the code pipeline.
 */

import chokidar, { type FSWatcher } from "chokidar";
import { join } from "path";

import { runWorkspaceMirror } from "../mirror/run-mirror.js";
import type { WorkspaceLanguage } from "../mirror/language-registry.js";
import {
  ensureDefaultLanguages,
  getLanguage,
} from "../mirror/language-registry.js";

// Common directories to ignore regardless of language
const COMMON_IGNORED: RegExp[] = [
  /[/\\]dist[/\\]/,
  /[/\\]node_modules[/\\]/,
  /[/\\]_legacy[/\\]/,
  /[/\\]_tmp[/\\]/,
  /[/\\]\.claude[/\\]/,
  /[/\\]md_db[/\\]/,
  /[/\\]__pycache__[/\\]/,
  /[/\\]\.venv[/\\]/,
  /[/\\]\.context-garden[/\\]/,
  /\.d\.ts$/,
];

// ---------------------------------------------------------------------------
// Options
// ---------------------------------------------------------------------------

export interface MirrorWatcherOptions {
  /**
   * Debounce window before regenerating (ms).
   * Any source file change resets the timer. Default: 3000
   */
  debounceMs?: number;
}

/** Config describing a workspace's mirror pipeline. */
export interface WorkspaceMirrorConfig {
  /** Absolute path to the source directory to watch. */
  sourceDir: string;
  /** Absolute path to mirror output directory (e.g. md_db/code/{name}). */
  mirrorDir: string;
  /** Workspace name — written into note frontmatter. */
  workspace: string;
  /** Wikilink prefix for cross-note references. */
  wikilinkPrefix: string;
  /** Which languages to mirror. */
  languages: WorkspaceLanguage[];
  /** Per-language omit pattern overrides. Falls back to defaults. */
  omitPatterns?: Partial<Record<WorkspaceLanguage, string[]>>;
}

// ---------------------------------------------------------------------------
// Config-driven watcher
// ---------------------------------------------------------------------------

/**
 * Start a chokidar watcher for a single workspace. Watches source files
 * matching the configured languages and regenerates mirrors on change.
 */
export function startWorkspaceMirrorWatcher(
  config: WorkspaceMirrorConfig,
  options?: MirrorWatcherOptions,
): FSWatcher {
  const debounceMs = options?.debounceMs ?? 3000;

  ensureDefaultLanguages();

  // Build watch globs from languages
  const watchPaths = config.languages
    .map((lang) => {
      const descriptor = getLanguage(lang);
      if (!descriptor) {
        console.warn(`[mirror-watcher] unknown language "${lang}" for workspace ${config.workspace}`);
        return null;
      }
      return join(config.sourceDir, descriptor.globPattern);
    })
    .filter((p): p is string => p !== null);

  const watcher = chokidar.watch(watchPaths, {
    ignored: COMMON_IGNORED,
    ignoreInitial: true,
    ignorePermissionErrors: true,
    awaitWriteFinish: { stabilityThreshold: 500, pollInterval: 100 },
  });

  let debounceTimer: ReturnType<typeof setTimeout> | undefined;

  function scheduleRun(): void {
    if (debounceTimer) clearTimeout(debounceTimer);
    debounceTimer = setTimeout(() => {
      debounceTimer = undefined;
      void runMirrors();
    }, debounceMs);
  }

  async function runMirrors(): Promise<void> {
    try {
      await runWorkspaceMirror({
        scanDir: config.sourceDir,
        mirrorDir: config.mirrorDir,
        languages: config.languages,
        force: true,
        workspace: config.workspace,
        wikilinkPrefix: config.wikilinkPrefix,
        omitPatterns: config.omitPatterns,
      });
    } catch (err) {
      console.warn(`[mirror-watcher] Mirror run failed for ${config.workspace}: ${err}`);
    }
  }

  watcher.on("add", scheduleRun);
  watcher.on("change", scheduleRun);
  watcher.on("unlink", scheduleRun);

  return watcher;
}
