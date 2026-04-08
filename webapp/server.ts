/**
 * ContextGarden Webapp Server
 *
 * Thin Express bridge between the browser and the Python daemon's TCP JSON-RPC.
 * Does NOT use DaemonClient (which auto-spawns) — status checks use direct probes
 * so we can distinguish "stopped" from errors. Only /api/daemon/start triggers spawn.
 *
 * Run: npm run webapp
 * Opens: http://localhost:7433
 */

import express from "express";
import { createConnection } from "net";
import { randomUUID } from "crypto";
import { fileURLToPath } from "url";
import { dirname, join } from "path";
import { DaemonClient, resolvePythonPath } from "../src/engine/daemon-client.js";

const __filename = fileURLToPath(import.meta.url);
const __dirname = dirname(__filename);

const WEBAPP_PORT = 7433;
const DAEMON_PORT = 7432;
const DATA_DIR = process.cwd();
const PYTHON_CACHE = join(DATA_DIR, ".context-garden", "python_path.txt");
const RPC_TIMEOUT_MS = 5_000;

// ── Direct TCP JSON-RPC (no auto-spawn, no reconnect) ───────────────────────

async function daemonRpc(method: string, params: Record<string, unknown> = {}): Promise<unknown> {
  return new Promise((resolve, reject) => {
    const sock = createConnection({ port: DAEMON_PORT, host: "127.0.0.1" });
    const id = randomUUID();
    let buf = "";

    const timer = setTimeout(() => {
      sock.destroy();
      reject(new Error(`RPC timeout: ${method}`));
    }, RPC_TIMEOUT_MS);

    sock.on("connect", () => {
      sock.write(JSON.stringify({ id, method, params }) + "\n");
    });

    sock.on("data", (chunk) => {
      buf += chunk.toString("utf-8");
      const lines = buf.split("\n");
      buf = lines.pop() ?? "";
      for (const line of lines) {
        if (!line.trim()) continue;
        try {
          const data = JSON.parse(line) as { id?: string; result?: unknown; error?: { message: string } };
          if (data.id === id) {
            clearTimeout(timer);
            sock.destroy();
            if (data.error) reject(new Error(data.error.message));
            else resolve(data.result);
          }
        } catch {
          // ignore non-JSON lines
        }
      }
    });

    sock.on("error", (err) => {
      clearTimeout(timer);
      reject(err);
    });

    sock.on("close", () => {
      clearTimeout(timer);
      reject(new Error("Connection closed before response"));
    });
  });
}

// ── Express app ──────────────────────────────────────────────────────────────

const app = express();
app.use(express.json());

// Serve the dashboard HTML
app.use(express.static(__dirname));

// GET /api/status — single daemon.health RPC (includes workspaces + active_connections)
app.get("/api/status", async (_req, res) => {
  try {
    const health = await daemonRpc("daemon.health", {});
    res.json(health);
  } catch {
    res.json({ status: "stopped" });
  }
});

// POST /api/workspaces/:id/unregister
app.post("/api/workspaces/:id/unregister", async (req, res) => {
  const { id } = req.params;
  try {
    const result = await daemonRpc("workspaces.unregister", { workspace_id: id, delete_data: false });
    res.json(result);
  } catch (err) {
    res.status(500).json({ error: String(err) });
  }
});

// POST /api/daemon/stop — send shutdown RPC
app.post("/api/daemon/stop", async (_req, res) => {
  try {
    const result = await daemonRpc("daemon.shutdown", {});
    res.json(result);
  } catch (err) {
    res.status(500).json({ error: String(err) });
  }
});

// POST /api/daemon/start — spawn via DaemonClient (handles Python env resolution)
app.post("/api/daemon/start", async (_req, res) => {
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

app.listen(WEBAPP_PORT, () => {
  console.log(`ContextGarden dashboard → http://localhost:${WEBAPP_PORT}`);
});
