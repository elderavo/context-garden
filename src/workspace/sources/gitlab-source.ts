import { randomBytes } from "crypto";
import { existsSync, mkdirSync, rmSync } from "fs";
import { join } from "path";

import { runWorkspaceMirror } from "../../mirror/run-mirror.js";
import { cloneRepo, fetchAndReset, resolveToken } from "../gitlab-manager.js";
import type { WorkspaceEntry } from "../registry.js";
import type { RegistryContext, WorkspaceSource } from "./source.js";

export class GitLabSource implements WorkspaceSource {
  async register(
    input: Omit<WorkspaceEntry, "id" | "registeredAt">,
    ctx: RegistryContext,
  ): Promise<{ entry: WorkspaceEntry; notesGenerated: number; notePaths: string[] }> {
    if (!input.gitlabConfig) {
      throw new Error(`gitlabConfig is required when sourceType is "gitlab"`);
    }

    const cloneDir = join(ctx.dataDir, ".context-garden", "clones", input.name);
    const webhookSecret = input.gitlabConfig.webhookSecret || randomBytes(32).toString("hex");
    const gitlabConfig = { ...input.gitlabConfig, cloneDir, webhookSecret };

    const token = resolveToken(gitlabConfig);
    process.stderr.write(`[registry] Cloning ${gitlabConfig.projectUrl} → ${cloneDir}\n`);
    await cloneRepo(gitlabConfig, token);

    const entry = ctx.addEntry({ ...input, sourceDir: cloneDir, gitlabConfig });
    const mirrorDir = ctx.mirrorDir(entry);
    mkdirSync(mirrorDir, { recursive: true });

    const result = await runWorkspaceMirror({
      scanDir: cloneDir,
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

  async sync(entry: WorkspaceEntry, ctx: RegistryContext): Promise<{ notePaths: string[] }> {
    if (!entry.gitlabConfig) {
      throw new Error(`Workspace "${entry.name}" is missing gitlabConfig`);
    }

    const token = resolveToken(entry.gitlabConfig);
    process.stderr.write(
      `[registry] Fetching ${entry.gitlabConfig.projectUrl} (${entry.gitlabConfig.branch})\n`,
    );
    await fetchAndReset(entry.gitlabConfig, token);

    const mirrorDir = ctx.mirrorDir(entry);
    await runWorkspaceMirror({
      scanDir: entry.sourceDir,
      mirrorDir,
      languages: entry.languages,
      force: false,
      workspace: entry.name,
      wikilinkPrefix: ctx.wikilinkPrefix(entry),
      omitPatterns: entry.omitPatterns,
    });

    return { notePaths: ctx.collectNotePaths(mirrorDir) };
  }

  startWatcher(_entry: WorkspaceEntry, _ctx: RegistryContext): null {
    // GitLab workspaces sync via the post-receive hook (cg-sync), not a filesystem watcher.
    return null;
  }

  async cleanup(entry: WorkspaceEntry, _ctx: RegistryContext): Promise<void> {
    if (entry.gitlabConfig?.cloneDir && existsSync(entry.gitlabConfig.cloneDir)) {
      rmSync(entry.gitlabConfig.cloneDir, { recursive: true, force: true });
      process.stderr.write(`[registry] Deleted clone: ${entry.gitlabConfig.cloneDir}\n`);
    }
  }

  shouldMirrorOnStartup(_entry: WorkspaceEntry): boolean {
    return false;
  }
}
