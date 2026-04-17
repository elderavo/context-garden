/**
 * Thin JSON-RPC client for the Python daemon TCP socket.
 *
 * Shared between webapp/server.ts and webapp/jobs.ts. Does NOT use
 * DaemonClient (which auto-spawns) — callers choose when to start the daemon.
 */

import { createConnection } from "net";
import { randomUUID } from "crypto";

const DAEMON_PORT = 7432;
const RPC_TIMEOUT_MS = 5_000;

export async function daemonRpc(
  method: string,
  params: Record<string, unknown> = {},
): Promise<unknown> {
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

    sock.on("data", (chunk: Buffer) => {
      buf += chunk.toString("utf-8");
      const lines = buf.split("\n");
      buf = lines.pop() ?? "";
      for (const line of lines) {
        if (!line.trim()) continue;
        try {
          const data = JSON.parse(line) as {
            id?: string;
            result?: unknown;
            error?: { message: string };
          };
          if (data.id === id) {
            clearTimeout(timer);
            sock.end();
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
