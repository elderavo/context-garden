import chokidar, { type FSWatcher } from "chokidar";
import { extname, join, relative } from "path";

import { lintFile } from "../markdown/markdown-linter.js";
import { listDir } from "../util/fs.js";

export interface MdDbLintWatcherOptions {
  /** Debounce per-path to avoid self-trigger loops (ms). Default: 2500 */
  debounceMs?: number;
  /** Additional ignore patterns for chokidar. */
  ignored?: (string | RegExp)[];
}

/**
 * Start a chokidar watcher that lints a markdown file whenever it is added/changed.
 */
export function startMdDbLintWatcher(
  mdDbPath: string,
  options?: MdDbLintWatcherOptions,
): FSWatcher {
  const debounceMs = options?.debounceMs ?? 2500;
  const ignored = options?.ignored ?? [/\.obsidian/, /\.context-garden/];

  const watcher = chokidar.watch(mdDbPath, {
    ignored,
    ignoreInitial: true,
    awaitWriteFinish: { stabilityThreshold: 500, pollInterval: 100 },
  });

  const lastRunByPath = new Map<string, number>();

  async function handle(path: string): Promise<void> {
    if (!path.endsWith(".md")) return;
    const rel = relative(mdDbPath, path).replace(/\\/g, "/");

    const now = Date.now();
    const last = lastRunByPath.get(rel) ?? 0;
    if (now - last < debounceMs) return;
    lastRunByPath.set(rel, now);

    try {
      const allNotePaths = collectNotePaths(mdDbPath);
      lintFile(path, rel, { fix: true, allNotePaths });
    } catch (err) {
      // Swallow lint errors to avoid noisy output during automated writes.
    }
  }

  watcher.on("add", (path) => void handle(path));
  watcher.on("change", (path) => void handle(path));

  return watcher;
}

function collectNotePaths(root: string): Set<string> {
  const ignoredDirs = new Set([".obsidian", ".context-garden"]);
  const result = new Set<string>();

  function walk(dir: string): void {
    let entries: string[];
    try {
      entries = listDir(dir);
    } catch {
      return;
    }

    for (const name of entries) {
      if (ignoredDirs.has(name)) continue;
      if (name.startsWith(".") && ignoredDirs.has(name)) continue;

      const fullPath = join(dir, name);
      let isDir = false;
      try {
        listDir(fullPath);
        isDir = true;
      } catch {
        isDir = false;
      }

      if (isDir) {
        walk(fullPath);
      } else if (extname(name) === ".md") {
        const rel = relative(root, fullPath).replace(/\\/g, "/");
        result.add(rel);
      }
    }
  }

  walk(root);
  return result;
}
