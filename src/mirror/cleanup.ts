/**
 * mirror-cleanup.ts
 *
 * Language-agnostic stale-note cleanup for code mirror directories.
 *
 * Extracted from mirror-codebase.ts so that cleanup can be shared by the
 * orchestrator (run-mirror.ts) without any language-specific logic.
 *
 * Callers are responsible for passing a *combined* validPaths set that covers
 * all languages mirrored into mirrorDir — otherwise notes belonging to a
 * language that was not included in the run would be incorrectly pruned.
 */

import * as fs from "fs";
import * as path from "path";

/**
 * Delete any .md files under mirrorDir that are not in validPaths.
 * Also removes any empty directories left behind after deletion.
 * Returns the number of files deleted.
 */
export function cleanMirrorDir(mirrorDir: string, validPaths: Set<string>): number {
  let cleaned = 0;

  function walkClean(dir: string): void {
    let entries: fs.Dirent[];
    try {
      entries = fs.readdirSync(dir, { withFileTypes: true });
    } catch {
      return;
    }

    for (const entry of entries) {
      const absPath = path.join(dir, entry.name);
      if (entry.isDirectory()) {
        walkClean(absPath);
        // Remove directory if now empty
        try {
          const remaining = fs.readdirSync(absPath);
          if (remaining.length === 0) fs.rmdirSync(absPath);
        } catch { /* ignore */ }
      } else if (entry.isFile() && entry.name.endsWith(".md")) {
        const resolved = path.resolve(absPath);
        if (!validPaths.has(resolved)) {
          try {
            fs.unlinkSync(absPath);
            cleaned++;
          } catch { /* can't delete — skip */ }
        }
      }
    }
  }

  walkClean(mirrorDir);
  return cleaned;
}
