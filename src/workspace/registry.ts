/**
 * WorkspaceRegistry — persistent registry of source directories to mirror.
 *
 * Each workspace is a source directory the system watches and mirrors into
 * md_db/. Clients register/unregister workspaces via MCP tools at runtime.
 *
 * Registry persists to `.context-garden/workspaces.json`.
 */

import { randomUUID } from "crypto";
import { existsSync, mkdirSync, readdirSync, readFileSync, rmSync, statSync, writeFileSync } from "fs";
import { dirname, join, relative, resolve } from "path";
import type { FSWatcher } from "chokidar";

import {
  ensureDefaultLanguages,
  getLanguage,
  type WorkspaceLanguage as RegisteredLanguage,
} from "../mirror/language-registry.js";
import { type GitLabConfig } from "./gitlab-manager.js";
import { runWorkspaceMirror } from "../mirror/run-mirror.js";
import { getSource, type RegistryContext } from "./sources/index.js";

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export type WorkspaceLanguage = RegisteredLanguage;
export type WorkspaceSourceType = "local" | "gitlab";
export type { GitLabConfig };

export interface WorkspaceEntry {
  /** Stable UUID. */
  id: string;
  /** Human slug — used as folder name in md_db. Alphanumeric + hyphens only. */
  name: string;
  /**
   * Absolute path to the source directory.
   * For "gitlab" workspaces this is the managed clone dir ({dataDir}/.context-garden/clones/{name}).
   * For "local" workspaces this is the user-supplied directory.
   */
  sourceDir: string;
  /** Which mirror scripts to run. */
  languages: WorkspaceLanguage[];
  /** Whether this workspace is active. */
  active: boolean;
  /** ISO timestamp of registration. */
  registeredAt: string;
  /** Per-language omit patterns. Falls back to script defaults when absent. */
  omitPatterns?: Partial<Record<WorkspaceLanguage, string[]>>;
  /**
   * Source type. Defaults to "local" for back-compat with existing entries
   * that predate this field.
   *
   * @deprecated "local" — prefer "gitlab" for new workspaces.
   */
  sourceType?: WorkspaceSourceType;
  /** Present when sourceType === "gitlab". */
  gitlabConfig?: GitLabConfig;
}

// ---------------------------------------------------------------------------
// Validation
// ---------------------------------------------------------------------------

const NAME_RE = /^[a-z0-9][a-z0-9-]{0,62}[a-z0-9]$/;

function validateName(name: string): void {
  if (name.length < 2 || name.length > 64) {
    throw new Error(`Workspace name must be 2-64 characters, got "${name}"`);
  }
  if (!NAME_RE.test(name)) {
    throw new Error(
      `Workspace name must be lowercase alphanumeric + hyphens (no leading/trailing hyphen), got "${name}"`,
    );
  }
}

function validateSourceDir(dir: string): void {
  const abs = resolve(dir);
  if (!existsSync(abs)) {
    throw new Error(`Source directory does not exist: ${abs}`);
  }
  if (!statSync(abs).isDirectory()) {
    throw new Error(`Source path is not a directory: ${abs}`);
  }
}

function validateLanguages(languages: WorkspaceLanguage[]): void {
  ensureDefaultLanguages();
  for (const lang of languages) {
    if (!getLanguage(lang)) {
      throw new Error(`Language "${lang}" is not registered.`);
    }
  }
}

// ---------------------------------------------------------------------------
// Registry
// ---------------------------------------------------------------------------

export class WorkspaceRegistry {
  private entries: WorkspaceEntry[] = [];
  private loaded = false;

  constructor(
    /** Path to `.context-garden/workspaces.json`. */
    private readonly registryPath: string,
    /** Path to `md_db/`. */
    private readonly mdDbPath: string,
    /** Root data directory — used to compute clone paths for GitLab workspaces. */
    private readonly dataDir: string,
  ) {}

  // ── Persistence ──────────────────────────────────────────────────────────

  /** Load entries from disk. Creates empty file if missing. */
  load(): void {
    if (existsSync(this.registryPath)) {
      try {
        const raw = readFileSync(this.registryPath, "utf-8");
        this.entries = JSON.parse(raw) as WorkspaceEntry[];
      } catch {
        console.warn("[workspace-registry] failed to parse workspaces.json, starting empty");
        this.entries = [];
      }
    } else {
      this.entries = [];
    }
    this.loaded = true;
  }

  /** Write current entries to disk. */
  save(): void {
    const dir = dirname(this.registryPath);
    if (!existsSync(dir)) mkdirSync(dir, { recursive: true });
    writeFileSync(this.registryPath, JSON.stringify(this.entries, null, 2), "utf-8");
  }

  // ── Queries ──────────────────────────────────────────────────────────────

  list(): readonly WorkspaceEntry[] {
    this.ensureLoaded();
    return this.entries;
  }

  get(id: string): WorkspaceEntry | undefined {
    this.ensureLoaded();
    return this.entries.find((e) => e.id === id);
  }

  getByName(name: string): WorkspaceEntry | undefined {
    this.ensureLoaded();
    return this.entries.find((e) => e.name === name);
  }

  // ── Mutations ────────────────────────────────────────────────────────────

  /**
   * Add a workspace entry. Validates name uniqueness and source directory.
   * Persists immediately.
   */
  add(
    input: Omit<WorkspaceEntry, "id" | "registeredAt">,
  ): WorkspaceEntry {
    this.ensureLoaded();
    validateName(input.name);
    validateSourceDir(input.sourceDir);
    validateLanguages(input.languages);
    if (input.languages.length === 0) {
      throw new Error("At least one language must be specified.");
    }

    if (this.getByName(input.name)) {
      throw new Error(`Workspace "${input.name}" already exists`);
    }

    const entry: WorkspaceEntry = {
      ...input,
      sourceDir: resolve(input.sourceDir),
      id: randomUUID(),
      registeredAt: new Date().toISOString(),
    };

    this.entries.push(entry);
    this.save();
    return entry;
  }

  /** Remove a workspace by ID. Persists immediately. */
  remove(id: string): WorkspaceEntry | undefined {
    this.ensureLoaded();
    const idx = this.entries.findIndex((e) => e.id === id);
    if (idx === -1) return undefined;
    const [removed] = this.entries.splice(idx, 1);
    this.save();
    return removed;
  }

  /** Remove a workspace by name. Persists immediately. */
  removeByName(name: string): WorkspaceEntry | undefined {
    const entry = this.getByName(name);
    if (!entry) return undefined;
    return this.remove(entry.id);
  }

  /** Toggle active state. Persists immediately. */
  setActive(id: string, active: boolean): void {
    this.ensureLoaded();
    const entry = this.get(id);
    if (!entry) throw new Error(`Workspace not found: ${id}`);
    entry.active = active;
    this.save();
  }

  // ── Helpers ──────────────────────────────────────────────────────────────

  /**
   * Return the md_db subdirectory for this workspace's mirrored notes.
   * e.g. "code/my-project"
   */
  static mirrorSubdir(entry: Pick<WorkspaceEntry, "name">): string {
    return `code/${entry.name}`;
  }

  /**
   * Return the absolute path to the workspace's mirror output directory.
   */
  mirrorDir(entry: Pick<WorkspaceEntry, "name">): string {
    return join(this.mdDbPath, WorkspaceRegistry.mirrorSubdir(entry));
  }

  /**
   * Return the wikilink prefix for notes in this workspace.
   */
  static wikilinkPrefix(entry: Pick<WorkspaceEntry, "name">): string {
    return WorkspaceRegistry.mirrorSubdir(entry);
  }

  // ── Watcher lifecycle ─────────────────────────────────────────────────

  private watchers = new Map<string, FSWatcher>();

  /**
   * Start a mirror watcher for a single workspace entry.
   * No-op if a watcher is already running for this workspace.
   */
  startWatcher(entry: WorkspaceEntry): void {
    if (!entry.active || this.watchers.has(entry.id)) return;
    const watcher = getSource(entry.sourceType).startWatcher(entry, this._ctx());
    if (watcher) this.watchers.set(entry.id, watcher);
  }

  /** Stop and remove a watcher for a workspace. */
  async stopWatcher(id: string): Promise<void> {
    const watcher = this.watchers.get(id);
    if (watcher) {
      await watcher.close();
      this.watchers.delete(id);
    }
  }

  /**
   * Run a full mirror pass for all active workspaces. Call on startup to
   * catch any source changes that happened while the MCP server was down
   * (chokidar misses these because it starts with ignoreInitial: true).
   * Runs all workspaces concurrently; errors are logged but non-fatal.
   */
  async mirrorAllWorkspaces(): Promise<void> {
    this.ensureLoaded();
    const active = this.entries.filter((e) => e.active && getSource(e.sourceType).shouldMirrorOnStartup(e));
    await Promise.all(
      active.map(async (entry) => {
        try {
          await runWorkspaceMirror({
            scanDir: entry.sourceDir,
            mirrorDir: this.mirrorDir(entry),
            languages: entry.languages,
            force: false,
            workspace: entry.name,
            wikilinkPrefix: WorkspaceRegistry.wikilinkPrefix(entry),
            omitPatterns: entry.omitPatterns,
          });
        } catch (err) {
          process.stderr.write(`[registry] Mirror pass failed for "${entry.name}": ${err}\n`);
        }
      }),
    );
  }

  /** Start watchers for all active workspaces. Call during stack init. */
  startAllWatchers(): void {
    this.ensureLoaded();
    for (const entry of this.entries) {
      if (entry.active) {
        this.startWatcher(entry);
      }
    }
  }

  /** Stop all watchers. Call during stack shutdown. */
  async stopAllWatchers(): Promise<void> {
    const ids = [...this.watchers.keys()];
    await Promise.all(ids.map((id) => this.stopWatcher(id)));
  }

  // ── Full lifecycle (register / unregister with mirror pipeline) ──────

  /**
   * Register a new workspace, run initial mirror, and start watcher (local) or
   * clone + mirror (gitlab). Returns the created entry, note count, and paths.
   */
  async register(
    input: Omit<WorkspaceEntry, "id" | "registeredAt">,
  ): Promise<{ entry: WorkspaceEntry; notesGenerated: number; notePaths: string[] }> {
    return getSource(input.sourceType).register(input, this._ctx());
  }

  private _ctx(): RegistryContext {
    return {
      mdDbPath: this.mdDbPath,
      dataDir: this.dataDir,
      mirrorDir: (entry) => this.mirrorDir(entry),
      wikilinkPrefix: (entry) => WorkspaceRegistry.wikilinkPrefix(entry),
      addEntry: (input) => this.add(input),
      collectNotePaths: (mirrorDir) => {
        const paths: string[] = [];
        collectMdPaths(mirrorDir, this.mdDbPath, paths);
        return paths;
      },
    };
  }

  /**
   * Pull the latest commits from the workspace source, re-mirror, and return updated note paths.
   * Called by the cg-sync CLI (invoked via SSH from the GitLab post-receive hook).
   * The caller is responsible for triggering engine.reindexWorkspace() afterwards.
   */
  async sync(name: string): Promise<{ notePaths: string[] }> {
    this.ensureLoaded();
    const entry = this.getByName(name);
    if (!entry) throw new Error(`Workspace "${name}" not found`);
    return getSource(entry.sourceType).sync(entry, this._ctx());
  }

  /**
   * Collect all note paths (relative to md_db) for an active workspace.
   */
  listNotePaths(entry: WorkspaceEntry): string[] {
    const mirrorDir = this.mirrorDir(entry);
    const paths: string[] = [];
    collectMdPaths(mirrorDir, this.mdDbPath, paths);
    return paths;
  }

  /**
   * Unregister a workspace: stop watcher, delete mirrored notes and clone (for
   * gitlab workspaces), remove from registry.
   */
  async unregister(
    name: string,
  ): Promise<{ removed: WorkspaceEntry | undefined; deletedPaths: string[] }> {
    const entry = this.getByName(name);
    if (!entry) return { removed: undefined, deletedPaths: [] };

    await this.stopWatcher(entry.id);

    const deletedPaths: string[] = [];
    const mirrorDir = this.mirrorDir(entry);

    if (existsSync(mirrorDir)) {
      collectMdPaths(mirrorDir, this.mdDbPath, deletedPaths);
      rmSync(mirrorDir, { recursive: true, force: true });
    }

    await getSource(entry.sourceType).cleanup(entry, this._ctx());

    const removed = this.remove(entry.id);
    return { removed, deletedPaths };
  }

  private ensureLoaded(): void {
    if (!this.loaded) {
      this.load();
    }
  }
}

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

/** Recursively collect all .md file paths relative to mdDbPath. */
function collectMdPaths(dir: string, mdDbPath: string, out: string[]): void {
  let entries: string[];
  try {
    entries = readdirSync(dir);
  } catch {
    return;
  }
  for (const name of entries) {
    const full = join(dir, name);
    try {
      const stat = statSync(full);
      if (stat.isDirectory()) {
        collectMdPaths(full, mdDbPath, out);
      } else if (name.endsWith(".md")) {
        out.push(relative(mdDbPath, full).replace(/\\/g, "/"));
      }
    } catch {
      continue;
    }
  }
}
