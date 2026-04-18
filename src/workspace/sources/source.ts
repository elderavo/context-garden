import type { FSWatcher } from "chokidar";
import type { WorkspaceEntry } from "../registry.js";

export interface RegistryContext {
  mdDbPath: string;
  dataDir: string;
  mirrorDir(entry: Pick<WorkspaceEntry, "name">): string;
  wikilinkPrefix(entry: Pick<WorkspaceEntry, "name">): string;
  addEntry(input: Omit<WorkspaceEntry, "id" | "registeredAt">): WorkspaceEntry;
  collectNotePaths(mirrorDir: string): string[];
}

export interface WorkspaceSource {
  register(
    input: Omit<WorkspaceEntry, "id" | "registeredAt">,
    ctx: RegistryContext,
  ): Promise<{ entry: WorkspaceEntry; notesGenerated: number; notePaths: string[] }>;

  sync(entry: WorkspaceEntry, ctx: RegistryContext): Promise<{ notePaths: string[] }>;

  /** Return a watcher to track, or null if the source manages its own sync externally. */
  startWatcher(entry: WorkspaceEntry, ctx: RegistryContext): FSWatcher | null;

  /** Remove source-specific artifacts during unregister (e.g. managed clone dirs). */
  cleanup(entry: WorkspaceEntry, ctx: RegistryContext): Promise<void>;

  /** Return false to exclude from the startup mirror pass. */
  shouldMirrorOnStartup(entry: WorkspaceEntry): boolean;
}
