/**
 * DaemonClient — JSON-RPC over TCP to the Python ContextGarden daemon.
 *
 * On connect():
 *   1. On first connect in this process, start `python -m graph.daemon --data-dir <dir>`.
 *      Skipped when CG_DAEMON_HOST is set — connects to remote daemon instead.
 *   2. Daemon startup is restart-safe: existing daemon is shutdown/rotated by Python.
 *   3. Maintain one persistent TCP connection.
 *   4. On disconnect, retry with exponential backoff (max 30s).
 */

import { createConnection, Socket } from "net";
import { join, dirname } from "path";
import { randomUUID } from "crypto";
import { execSync, spawn } from "child_process";
import { existsSync, readFileSync, writeFileSync } from "fs";

const DAEMON_PORT = parseInt(process.env["CG_DAEMON_PORT"] ?? "7432", 10);
const DAEMON_HOST = process.env["CG_DAEMON_HOST"] ?? "127.0.0.1";
const CONNECT_TIMEOUT_MS = 5_000;
const SPAWN_WAIT_MS = 10_000;
const SPAWN_PROBE_INTERVAL_MS = 500;
const RPC_TIMEOUT_MS = 120_000;
const MAX_RECONNECT_DELAY_MS = 30_000;

interface RpcPending {
  resolve: (result: unknown) => void;
  reject: (error: Error) => void;
  timer: ReturnType<typeof setTimeout>;
}

export class DaemonClient {
  private socket: Socket | null = null;
  private buffer = "";
  private pending = new Map<string, RpcPending>();
  private connected = false;
  private reconnectDelay = 1_000;
  private shuttingDown = false;

  constructor(
    private readonly pythonPath: string,
    private readonly daemonCwd: string,
    private readonly dataDir: string,
  ) {}

  // ── Public API ──────────────────────────────────────────────────────────

  /**
   * Connect to the daemon, spawning it first if unreachable.
   * Safe to call multiple times — idempotent when already connected.
   */
  async connect(): Promise<void> {
    if (this.connected) return;

    const remote = !!process.env["CG_DAEMON_HOST"];
    if (!remote) {
      const reachable = await this._probe();
      if (!reachable) {
        await this._spawnDaemon();
      }
    }

    await this._openSocket();
  }

  /**
   * Send a JSON-RPC call and await the response.
   * Reconnects if the socket is dead.
   */
  async rpc(method: string, params: Record<string, unknown>): Promise<unknown> {
    if (!this.connected) {
      await this.connect();
    }

    const id = randomUUID();
    const line = JSON.stringify({ id, method, params }) + "\n";

    return new Promise<unknown>((resolve, reject) => {
      const timer = setTimeout(() => {
        this.pending.delete(id);
        reject(new Error(`RPC timeout: ${method} (${RPC_TIMEOUT_MS}ms)`));
      }, RPC_TIMEOUT_MS);

      this.pending.set(id, { resolve, reject, timer });

      if (!this.socket || !this.socket.writable) {
        clearTimeout(timer);
        this.pending.delete(id);
        reject(new Error(`Daemon socket not writable (method: ${method})`));
        return;
      }

      this.socket.write(line, (err) => {
        if (err) {
          clearTimeout(timer);
          this.pending.delete(id);
          reject(new Error(`Daemon socket write error: ${err.message}`));
        }
      });
    });
  }

  /**
   * Close the socket. The daemon is a long-running OS process and is never
   * shut down by the TS — only an explicit daemon.shutdown RPC does that.
   */
  close(): void {
    this.shuttingDown = true;
    this._closeSocket();
  }

  // ── Connection internals ────────────────────────────────────────────────

  private async _probe(): Promise<boolean> {
    try {
      await this._tryConnect();
      return true;
    } catch {
      return false;
    }
  }

  private _tryConnect(): Promise<void> {
    return new Promise<void>((resolve, reject) => {
      const sock = createConnection({ port: DAEMON_PORT, host: DAEMON_HOST });
      const timer = setTimeout(() => {
        sock.destroy();
        reject(new Error("Connection timeout"));
      }, CONNECT_TIMEOUT_MS);

      sock.on("connect", () => {
        clearTimeout(timer);
        sock.end(); // FIN, not RST — lets Python detect EOF cleanly
        resolve();
      });

      sock.on("error", (err) => {
        clearTimeout(timer);
        reject(err);
      });
    });
  }

  private async _spawnDaemon(): Promise<void> {
    const env: Record<string, string> = {};
    // Inherit env for PATH/conda runtime, but daemon config is sourced from
    // config.json + ~/.context-garden/.env only.
    for (const [k, v] of Object.entries(process.env)) {
      if (v !== undefined) env[k] = v;
    }
    // Prevent roaming user-site packages from leaking into the conda interpreter
    env["PYTHONNOUSERSITE"] = "1";

    const proc = spawn(this.pythonPath, ["-m", "graph.daemon", "--data-dir", this.dataDir], {
      cwd: this.daemonCwd,
      env,
      stdio: ["ignore", "ignore", "pipe"],
      detached: false,
    });

    proc.stderr?.on("data", (chunk: Buffer) => {
      process.stderr.write(chunk);
    });

    proc.on("exit", (code, signal) => {
      if (!this.shuttingDown) {
        process.stderr.write(
          `[daemon-client] Daemon process exited unexpectedly (code=${code} signal=${signal})\n`,
        );
      }
    });

    // Wait until the daemon is accepting connections
    const deadline = Date.now() + SPAWN_WAIT_MS;
    while (Date.now() < deadline) {
      await _sleep(SPAWN_PROBE_INTERVAL_MS);
      const ready = await this._probe();
      if (ready) return;
    }

    throw new Error(
      `Daemon failed to start within ${SPAWN_WAIT_MS}ms. ` +
      `Try: conda run -n contextgarden python -m graph.daemon`,
    );
  }

  private async _openSocket(): Promise<void> {
    const sock = createConnection({ port: DAEMON_PORT, host: DAEMON_HOST });

    await new Promise<void>((resolve, reject) => {
      const timer = setTimeout(() => {
        sock.destroy();
        reject(new Error("Daemon socket connect timeout"));
      }, CONNECT_TIMEOUT_MS);

      sock.on("connect", () => {
        clearTimeout(timer);
        resolve();
      });

      sock.on("error", (err) => {
        clearTimeout(timer);
        reject(err);
      });
    });

    sock.on("data", (chunk: Buffer) => this._onData(chunk.toString("utf-8")));

    sock.on("close", () => {
      this.connected = false;
      this.socket = null;
      this._rejectAllPending(new Error("Daemon connection closed"));
      if (!this.shuttingDown) {
        this._scheduleReconnect();
      }
    });

    sock.on("error", (err) => {
      process.stderr.write(`[daemon-client] Socket error: ${err.message}\n`);
    });

    this.socket = sock;
    this.connected = true;
    this.reconnectDelay = 1_000;
  }

  private _onData(chunk: string): void {
    this.buffer += chunk;
    const lines = this.buffer.split("\n");
    this.buffer = lines.pop() ?? "";

    for (const line of lines) {
      const trimmed = line.trim();
      if (!trimmed) continue;
      this._handleResponse(trimmed);
    }
  }

  private _handleResponse(line: string): void {
    let data: { id?: string; result?: unknown; error?: { code: number; message: string } };
    try {
      data = JSON.parse(line);
    } catch {
      process.stderr.write(`[daemon-client] Non-JSON from daemon: ${line.slice(0, 200)}\n`);
      return;
    }

    if (!data.id) return;

    const pending = this.pending.get(data.id);
    if (!pending) return;

    clearTimeout(pending.timer);
    this.pending.delete(data.id);

    if (data.error) {
      pending.reject(new Error(`Daemon RPC error (${data.error.code}): ${data.error.message}`));
    } else {
      pending.resolve(data.result);
    }
  }

  private _rejectAllPending(err: Error): void {
    for (const [id, pending] of this.pending) {
      clearTimeout(pending.timer);
      pending.reject(err);
    }
    this.pending.clear();
  }

  private _closeSocket(): void {
    this.connected = false;
    if (this.socket) {
      try { this.socket.destroy(); } catch { /* ignore */ }
      this.socket = null;
    }
  }

  private _scheduleReconnect(): void {
    const delay = this.reconnectDelay;
    this.reconnectDelay = Math.min(this.reconnectDelay * 2, MAX_RECONNECT_DELAY_MS);

    setTimeout(async () => {
      if (this.shuttingDown) return;
      try {
        await this.connect();
      } catch (err) {
        process.stderr.write(`[daemon-client] Reconnect failed: ${err}\n`);
        this._scheduleReconnect();
      }
    }, delay);
  }
}

// ---------------------------------------------------------------------------
// Python path resolution (mirrors context-engine.ts logic)
// ---------------------------------------------------------------------------

export function resolvePythonPath(cacheFile: string): string {
  // Check cache file
  try {
    const cached = readFileSync(cacheFile, "utf-8").trim();
    if (cached && existsSync(cached)) return cached;
  } catch {
    // No cache — fall through
  }

  const resolved = searchPythonPath();

  try {
    writeFileSync(cacheFile, resolved, "utf-8");
  } catch {
    // Non-fatal
  }

  return resolved;
}

function searchPythonPath(): string {
  const home = process.env["USERPROFILE"] ?? process.env["HOME"] ?? "";
  const condaDirs = [
    process.env["CONDA_PREFIX"] ? dirname(process.env["CONDA_PREFIX"]!) : "",
    join(home, "miniconda3", "envs"),
    join(home, "anaconda3", "envs"),
    join(home, ".conda", "envs"),
  ].filter(Boolean);

  for (const base of condaDirs) {
    const winPath = join(base, "contextgarden", "python.exe");
    if (existsSync(winPath)) return winPath;
    const unixPath = join(base, "contextgarden", "bin", "python");
    if (existsSync(unixPath)) return unixPath;
  }

  try {
    const result = execSync(
      'conda run -n contextgarden python -c "import sys; print(sys.executable)"',
      { encoding: "utf-8", timeout: 15_000 },
    ).trim();
    if (result && existsSync(result)) return result;
  } catch {
    // conda not available
  }

  throw new Error(
    "Could not find Python for conda env 'contextgarden'. " +
    "Ensure the environment exists: conda env create -f graph/environment.yml",
  );
}

function _sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}
