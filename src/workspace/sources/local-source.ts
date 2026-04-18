import { mkdirSync } from "fs";
import type { FSWatcher } from "chokidar";

import { runWorkspaceMirror } from "../../mirror/run-mirror.js";
import { startWorkspaceMirrorWatcher } from "../mirror-watcher.js";
import type { WorkspaceEntry } from "../registry.js";
import type { RegistryContext, WorkspaceSource } from "./source.js";

export class LocalSource implements WorkspaceSource {
  async register(
    input: Omit<WorkspaceEntry, "id" | "registeredAt">,
    ctx: RegistryContext,
  ): Promise<{ entry: WorkspaceEntry; notesGenerated: number; notePaths: string[] }> {
    process.stderr.write(
      `[registry] DEPRECATED: local workspace "${input.name}" registered. ` +
      `Prefer GitLab-backed workspaces (source_type: "gitlab").\n`,
    );

    const entry = ctx.addEntry(input);
    const mirrorDir = ctx.mirrorDir(entry);
    mkdirSync(mirrorDir, { recursive: true });

    const result = await runWorkspaceMirror({
      scanDir: entry.sourceDir,
      mirrorDir,
      languages: entry.languages,
      force: false,
      workspace: entry.name,
      wikilinkPrefix: ctx.wikilinkPrefix(entry),
      omitPatterns: entry.omitPatterns,
    });
    const notesGenerated = Object.values(result.written).reduce((sum, n) => sum + n, 0);
    const notePaths = notesGenerated > 0 ? ctx.collectNotePaths(mirrorDir) : [];

    return { entry, notesGenerated, notePaths };
  }

  async sync(_entry: WorkspaceEntry, _ctx: RegistryContext): Promise<{ notePaths: string[] }> {
    throw new Error("Local workspaces do not support sync — use the filesystem watcher.");
  }

  startWatcher(entry: WorkspaceEntry, ctx: RegistryContext): FSWatcher {
    return startWorkspaceMirrorWatcher({
      sourceDir: entry.sourceDir,
      mirrorDir: ctx.mirrorDir(entry),
      workspace: entry.name,
      wikilinkPrefix: ctx.wikilinkPrefix(entry),
      languages: entry.languages,
      omitPatterns: entry.omitPatterns,
    });
  }

  async cleanup(_entry: WorkspaceEntry, _ctx: RegistryContext): Promise<void> {
    // Local source dirs are user-owned — nothing for CG to delete.
  }

  shouldMirrorOnStartup(_entry: WorkspaceEntry): boolean {
    return true;
  }
}
