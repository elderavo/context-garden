/**
 * Job queue for the ContextGarden control plane.
 *
 * Decouples webhook receipt from long-running work. Jobs are processed
 * serially per workspace (a second push while one is running is coalesced
 * into a single follow-up job, not queued N times).
 *
 * Job types:
 *   sync_workspace  — git fetch+reset, mirror, reindex
 *   index_workspace — reindex only (no git or mirror)
 */

import { randomUUID } from "crypto";
import { readFileSync } from "fs";
import { join } from "path";

import { fetchAndReset, resolveToken } from "../src/workspace/gitlab-manager.js";
import { runWorkspaceMirror } from "../src/mirror/run-mirror.js";
import { ensureDefaultLanguages } from "../src/mirror/language-registry.js";
import { daemonRpc } from "./daemon-rpc.js";

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export type JobType = "sync_workspace" | "index_workspace";
export type JobStatus = "pending" | "running" | "done" | "failed";

export interface Job {
  id: string;
  type: JobType;
  workspaceId: string;
  workspaceName: string;
  triggeredBy: "webhook" | "manual";
  status: JobStatus;
  createdAt: string;
  startedAt?: string;
  completedAt?: string;
  log: string[];
}

// Minimal types for reading workspaces.json directly (avoids importing the
// full registry class and its chokidar/watcher transitive deps).
interface GitLabConfig {
  projectUrl: string;
  branch: string;
  accessToken?: string;
  cloneDir: string;
  webhookSecret: string;
}

interface WorkspaceEntryRaw {
  id: string;
  name: string;
  sourceDir: string;
  languages: string[];
  active: boolean;
  registeredAt: string;
  sourceType?: "local" | "gitlab";
  gitlabConfig?: GitLabConfig;
  omitPatterns?: Record<string, string[]>;
}

// ---------------------------------------------------------------------------
// Queue state
// ---------------------------------------------------------------------------

const MAX_HISTORY = 200;
const _queue: Job[] = [];
let _workerRunning = false;
let _dataDir = process.cwd();

/** Call once at startup with the data directory. */
export function initJobs(dataDir: string): void {
  _dataDir = dataDir;
  ensureDefaultLanguages();
}

// ---------------------------------------------------------------------------
// Public API
// ---------------------------------------------------------------------------

/**
 * Enqueue a sync job for a workspace.
 *
 * Idempotent: if the workspace already has a pending or running job of the
 * same type, returns the existing job rather than creating a duplicate.
 * This means receiving the same push webhook twice is harmless.
 */
export function enqueueSync(
  workspaceId: string,
  workspaceName: string,
  triggeredBy: "webhook" | "manual" = "manual",
): Job {
  const existing = _queue.find(
    (j) =>
      j.workspaceId === workspaceId &&
      j.type === "sync_workspace" &&
      (j.status === "pending" || j.status === "running"),
  );
  if (existing) return existing;

  const job = _makeJob("sync_workspace", workspaceId, workspaceName, triggeredBy);
  _push(job);
  _scheduleWorker();
  return job;
}

/**
 * Enqueue a reindex-only job (no git or mirror pass).
 * Same idempotency rules as enqueueSync.
 */
export function enqueueIndex(
  workspaceId: string,
  workspaceName: string,
  triggeredBy: "webhook" | "manual" = "manual",
): Job {
  const existing = _queue.find(
    (j) =>
      j.workspaceId === workspaceId &&
      j.type === "index_workspace" &&
      (j.status === "pending" || j.status === "running"),
  );
  if (existing) return existing;

  const job = _makeJob("index_workspace", workspaceId, workspaceName, triggeredBy);
  _push(job);
  _scheduleWorker();
  return job;
}

/** Return all jobs, newest first. */
export function listJobs(): Job[] {
  return [..._queue].reverse();
}

/** Return a single job by ID. */
export function getJob(id: string): Job | undefined {
  return _queue.find((j) => j.id === id);
}

// ---------------------------------------------------------------------------
// Internal queue helpers
// ---------------------------------------------------------------------------

function _makeJob(
  type: JobType,
  workspaceId: string,
  workspaceName: string,
  triggeredBy: "webhook" | "manual",
): Job {
  return {
    id: randomUUID(),
    type,
    workspaceId,
    workspaceName,
    triggeredBy,
    status: "pending",
    createdAt: new Date().toISOString(),
    log: [],
  };
}

function _push(job: Job): void {
  _queue.push(job);
  if (_queue.length > MAX_HISTORY) _queue.splice(0, _queue.length - MAX_HISTORY);
}

function _log(job: Job, line: string): void {
  job.log.push(`[${new Date().toISOString()}] ${line}`);
}

// ---------------------------------------------------------------------------
// Worker
// ---------------------------------------------------------------------------

function _scheduleWorker(): void {
  if (_workerRunning) return;
  _workerRunning = true;
  void _runWorker();
}

async function _runWorker(): Promise<void> {
  while (true) {
    const job = _queue.find((j) => j.status === "pending");
    if (!job) {
      _workerRunning = false;
      return;
    }

    job.status = "running";
    job.startedAt = new Date().toISOString();

    try {
      if (job.type === "sync_workspace") {
        await _executeSyncJob(job);
      } else {
        await _executeIndexJob(job);
      }
      job.status = "done";
    } catch (err) {
      job.status = "failed";
      _log(job, `Error: ${err}`);
    }

    job.completedAt = new Date().toISOString();
  }
}

// ---------------------------------------------------------------------------
// Job execution
// ---------------------------------------------------------------------------

function _loadWorkspace(workspaceId: string): WorkspaceEntryRaw {
  const wsPath = join(_dataDir, ".context-garden", "workspaces.json");
  const entries = JSON.parse(readFileSync(wsPath, "utf-8")) as WorkspaceEntryRaw[];
  const entry = entries.find((e) => e.id === workspaceId);
  if (!entry) throw new Error(`Workspace ${workspaceId} not found in registry`);
  return entry;
}

async function _executeSyncJob(job: Job): Promise<void> {
  const entry = _loadWorkspace(job.workspaceId);

  if (entry.sourceType !== "gitlab" || !entry.gitlabConfig) {
    throw new Error(`Workspace "${entry.name}" is not a GitLab workspace`);
  }

  const { gitlabConfig } = entry;
  const token = resolveToken(gitlabConfig);

  // 1. Converge clone to remote state.
  _log(job, `Fetching ${gitlabConfig.projectUrl} (${gitlabConfig.branch})...`);
  await fetchAndReset(gitlabConfig, token);
  _log(job, `Fetch complete.`);

  // 2. Mirror source into md_db.
  _log(job, `Mirroring...`);
  const mdDbPath = join(_dataDir, "md_db");
  const mirrorDir = join(mdDbPath, "code", entry.name);
  const result = await runWorkspaceMirror({
    scanDir: gitlabConfig.cloneDir,
    mirrorDir,
    languages: entry.languages as never[],
    force: false,
    workspace: entry.name,
    wikilinkPrefix: `code/${entry.name}`,
    omitPatterns: entry.omitPatterns,
  });
  const written = Object.values(result.written).reduce((s, n) => s + n, 0);
  _log(job, `Mirror complete. Notes written: ${written}.`);

  // 3. Tell daemon to reindex.
  await _triggerReindex(job);
}

async function _executeIndexJob(job: Job): Promise<void> {
  await _triggerReindex(job);
}

async function _triggerReindex(job: Job): Promise<void> {
  _log(job, `Triggering daemon reindex...`);
  try {
    await daemonRpc("reindex", {});
    _log(job, `Reindex queued.`);
  } catch (err) {
    // Non-fatal: daemon may be temporarily down; it will catch up on restart
    // via manifest diff detection.
    _log(job, `Warning: reindex RPC failed (daemon may be down): ${err}`);
  }
}
