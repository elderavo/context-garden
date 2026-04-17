/**
 * ContextGarden Control Plane
 *
 * Evolves the monitoring webapp into a proper control plane:
 *   - Receives GitLab push webhooks and enqueues sync jobs
 *   - Exposes manual sync/index triggers per workspace
 *   - Shows job queue status and logs
 *   - Manages daemon lifecycle (start/stop)
 *   - Serves the monitoring dashboard
 *
 * Architecture:
 *   GitLab → POST /webhooks/gitlab → job queue → worker (fetch+reset, mirror, reindex)
 *   Browser → GET / → dashboard
 *   Agent/script → POST /workspaces/:id/sync|index → job queue
 *
 * Run: npm run webapp
 * Opens: http://localhost:7433
 */

import express from "express";
import { readFileSync, writeFileSync, existsSync, rmSync } from "fs";
import { join } from "path";
import { fileURLToPath } from "url";
import { dirname } from "path";
import { DaemonClient, resolvePythonPath } from "../src/engine/daemon-client.js";
import { daemonRpc } from "./daemon-rpc.js";
import { initJobs, enqueueSync, enqueueIndex, listJobs, getJob } from "./jobs.js";

const __filename = fileURLToPath(import.meta.url);
const __dirname = dirname(__filename);

const WEBAPP_PORT = parseInt(process.env["CG_WEBAPP_PORT"] ?? "7433", 10);
const DATA_DIR = process.env["CG_DATA_DIR"] ?? process.cwd();
const PYTHON_CACHE = join(DATA_DIR, ".context-garden", "python_path.txt");
const WORKSPACES_PATH = join(DATA_DIR, ".context-garden", "workspaces.json");

// ---------------------------------------------------------------------------
// Workspace helpers (reads workspaces.json directly — no registry class)
// ---------------------------------------------------------------------------

interface GitLabConfig {
  projectUrl: string;
  branch: string;
  accessToken?: string;
  cloneDir: string;
  webhookSecret: string;
}

interface WorkspaceEntry {
  id: string;
  name: string;
  sourceDir: string;
  languages: string[];
  active: boolean;
  registeredAt: string;
  sourceType?: "local" | "gitlab";
  gitlabConfig?: GitLabConfig;
}

function loadWorkspaces(): WorkspaceEntry[] {
  if (!existsSync(WORKSPACES_PATH)) return [];
  try {
    return JSON.parse(readFileSync(WORKSPACES_PATH, "utf-8")) as WorkspaceEntry[];
  } catch {
    return [];
  }
}

function saveWorkspaces(entries: WorkspaceEntry[]): void {
  writeFileSync(WORKSPACES_PATH, JSON.stringify(entries, null, 2), "utf-8");
}

// ---------------------------------------------------------------------------
// Init
// ---------------------------------------------------------------------------

initJobs(DATA_DIR);

// ---------------------------------------------------------------------------
// Express app
// ---------------------------------------------------------------------------

const app = express();
app.use(express.json());
app.use(express.static(__dirname));

// ---------------------------------------------------------------------------
// Webhook: POST /webhooks/gitlab
//
// Single endpoint for all GitLab push events. The webhook secret in the
// X-Gitlab-Token header identifies which workspace the event belongs to.
// ---------------------------------------------------------------------------

app.post("/webhooks/gitlab", (req, res) => {
  const token = req.headers["x-gitlab-token"];
  if (typeof token !== "string" || !token) {
    res.status(401).json({ error: "Missing X-Gitlab-Token header" });
    return;
  }

  const workspaces = loadWorkspaces();
  const entry = workspaces.find(
    (w) => w.sourceType === "gitlab" && w.gitlabConfig?.webhookSecret === token,
  );

  if (!entry) {
    // Return 401 rather than 404 so GitLab doesn't infer workspace names from timing.
    res.status(401).json({ error: "Unknown webhook token" });
    return;
  }

  // Check that the push is for the tracked branch.
  const payload = req.body as { ref?: string; object_kind?: string };
  const expectedRef = `refs/heads/${entry.gitlabConfig!.branch}`;
  if (payload.ref && payload.ref !== expectedRef) {
    res.status(200).json({ status: "ignored", reason: `push was to ${payload.ref}, tracking ${expectedRef}` });
    return;
  }

  // Respond immediately — enqueue the work.
  const job = enqueueSync(entry.id, entry.name, "webhook");
  res.status(202).json({ status: "accepted", jobId: job.id, workspaceName: entry.name });
});

// ---------------------------------------------------------------------------
// Workspace routes
// ---------------------------------------------------------------------------

// GET /workspaces — list all workspaces with their status
app.get("/workspaces", (_req, res) => {
  const workspaces = loadWorkspaces();
  const jobs = listJobs();

  const result = workspaces.map((w) => {
    const lastJob = jobs.find((j) => j.workspaceId === w.id);
    return {
      id: w.id,
      name: w.name,
      sourceType: w.sourceType ?? "local",
      active: w.active,
      registeredAt: w.registeredAt,
      source: w.sourceType === "gitlab" ? w.gitlabConfig?.projectUrl : w.sourceDir,
      branch: w.gitlabConfig?.branch,
      lastSync: lastJob
        ? { status: lastJob.status, completedAt: lastJob.completedAt ?? null }
        : null,
    };
  });

  res.json(result);
});

// POST /workspaces/:id/sync — manually trigger a sync job
app.post("/workspaces/:id/sync", (req, res) => {
  const entry = loadWorkspaces().find((w) => w.id === req.params["id"]);
  if (!entry) { res.status(404).json({ error: "Workspace not found" }); return; }
  if (entry.sourceType !== "gitlab") {
    res.status(400).json({ error: "Only GitLab workspaces support sync" });
    return;
  }

  const job = enqueueSync(entry.id, entry.name, "manual");
  res.status(202).json({ jobId: job.id, status: job.status });
});

// POST /workspaces/:id/index — manually trigger a reindex job
app.post("/workspaces/:id/index", (req, res) => {
  const entry = loadWorkspaces().find((w) => w.id === req.params["id"]);
  if (!entry) { res.status(404).json({ error: "Workspace not found" }); return; }

  const job = enqueueIndex(entry.id, entry.name, "manual");
  res.status(202).json({ jobId: job.id, status: job.status });
});

// Shared handler for unregistering a workspace:
// 1. Remove from workspaces.json
// 2. Delete the mirror dir under md_db/code/<name>
// 3. For gitlab workspaces, also delete the clone dir
// 4. Tell the daemon to unregister (in-memory removal)
async function handleUnregister(id: string, res: express.Response): Promise<void> {
  try {
    const workspaces = loadWorkspaces();
    const idx = workspaces.findIndex((w) => w.id === id);
    if (idx === -1) {
      res.status(404).json({ error: "Workspace not found" });
      return;
    }

    const entry = workspaces[idx]!;

    // Remove from workspaces.json first so it won't be re-registered on reconnect.
    const updated = [...workspaces.slice(0, idx), ...workspaces.slice(idx + 1)];
    saveWorkspaces(updated);

    // Delete mirror dir: md_db/code/<name>
    const mirrorDir = join(DATA_DIR, "md_db", "code", entry.name);
    rmSync(mirrorDir, { recursive: true, force: true });

    // For gitlab workspaces, also delete the clone dir.
    if (entry.sourceType === "gitlab" && entry.gitlabConfig?.cloneDir) {
      rmSync(entry.gitlabConfig.cloneDir, { recursive: true, force: true });
    }

    // Remove from the daemon's in-memory registry.
    const result = await daemonRpc("workspaces.unregister", {
      workspace_id: id,
      delete_data: false,
    });

    res.json(result);
  } catch (err) {
    res.status(500).json({ error: String(err) });
  }
}

// POST /api/workspaces/:id/unregister — kept for dashboard compatibility
app.post("/api/workspaces/:id/unregister", (req, res) => {
  void handleUnregister(req.params["id"]!, res);
});

// POST /workspaces/:id/unregister — canonical path (no /api prefix)
app.post("/workspaces/:id/unregister", (req, res) => {
  void handleUnregister(req.params["id"]!, res);
});

// ---------------------------------------------------------------------------
// Job routes
// ---------------------------------------------------------------------------

// GET /jobs — list recent jobs (newest first)
app.get("/jobs", (_req, res) => {
  res.json(listJobs().map((j) => ({
    id: j.id,
    type: j.type,
    workspaceName: j.workspaceName,
    triggeredBy: j.triggeredBy,
    status: j.status,
    createdAt: j.createdAt,
    startedAt: j.startedAt ?? null,
    completedAt: j.completedAt ?? null,
  })));
});

// GET /jobs/:id — full job detail including log
app.get("/jobs/:id", (req, res) => {
  const job = getJob(req.params["id"]);
  if (!job) { res.status(404).json({ error: "Job not found" }); return; }
  res.json(job);
});

// ---------------------------------------------------------------------------
// Daemon routes
// ---------------------------------------------------------------------------

// GET /status — daemon health (includes workspaces + connections)
app.get("/status", async (_req, res) => {
  try {
    const health = await daemonRpc("daemon.health", {});
    res.json(health);
  } catch {
    res.json({ status: "stopped" });
  }
});

// Keep legacy path for existing dashboard clients
app.get("/api/status", async (_req, res) => {
  try {
    const health = await daemonRpc("daemon.health", {});
    res.json(health);
  } catch {
    res.json({ status: "stopped" });
  }
});

app.post("/daemon/stop", async (_req, res) => {
  try {
    const result = await daemonRpc("daemon.shutdown", {});
    res.json(result);
  } catch (err) {
    res.status(500).json({ error: String(err) });
  }
});

app.post("/daemon/start", async (_req, res) => {
  try {
    let pythonPath: string;
    try {
      pythonPath = resolvePythonPath(PYTHON_CACHE);
    } catch {
      pythonPath = "python";
    }
    const client = new DaemonClient(pythonPath, DATA_DIR, DATA_DIR);
    await client.connect();
    client.close();

    const health = await daemonRpc("daemon.health", {});
    res.json(health);
  } catch (err) {
    res.status(500).json({ error: String(err) });
  }
});

// Legacy paths for existing dashboard
app.post("/api/daemon/stop", async (_req, res) => {
  try { res.json(await daemonRpc("daemon.shutdown", {})); }
  catch (err) { res.status(500).json({ error: String(err) }); }
});
app.post("/api/daemon/start", async (_req, res) => {
  try {
    let pythonPath: string;
    try { pythonPath = resolvePythonPath(PYTHON_CACHE); } catch { pythonPath = "python"; }
    const client = new DaemonClient(pythonPath, DATA_DIR, DATA_DIR);
    await client.connect();
    client.close();
    res.json(await daemonRpc("daemon.health", {}));
  } catch (err) { res.status(500).json({ error: String(err) }); }
});

// ---------------------------------------------------------------------------
// Start
// ---------------------------------------------------------------------------

app.listen(WEBAPP_PORT, () => {
  console.log(`ContextGarden control plane → http://localhost:${WEBAPP_PORT}`);
  console.log(`  Webhook endpoint: POST http://localhost:${WEBAPP_PORT}/webhooks/gitlab`);
});
