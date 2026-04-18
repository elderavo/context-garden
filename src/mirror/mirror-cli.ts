/**
 * mirror-cli.ts
 *
 * Thin CLI wrapper around runWorkspaceMirror — invoked by the Python daemon
 * as a subprocess during sync jobs.
 *
 * Usage:
 *   node dist/mirror/mirror-cli.js '<json>'
 *
 * Input: JSON string (WorkspaceMirrorRunOptions) as argv[2]
 * Output: JSON result (WorkspaceMirrorRunResult) on stdout
 *         { "error": "..." } on stdout + exit 1 on failure
 */

import { runWorkspaceMirror } from "./run-mirror.js";
import type { WorkspaceMirrorRunOptions } from "./run-mirror.js";

async function main(): Promise<void> {
  const raw = process.argv[2];
  if (!raw) {
    process.stdout.write(JSON.stringify({ error: "No arguments provided" }) + "\n");
    process.exit(1);
  }

  let opts: WorkspaceMirrorRunOptions;
  try {
    opts = JSON.parse(raw) as WorkspaceMirrorRunOptions;
  } catch (err) {
    process.stdout.write(JSON.stringify({ error: `Failed to parse args: ${err}` }) + "\n");
    process.exit(1);
  }

  try {
    const result = await runWorkspaceMirror(opts);
    process.stdout.write(JSON.stringify(result) + "\n");
  } catch (err) {
    process.stdout.write(JSON.stringify({ error: String(err) }) + "\n");
    process.exit(1);
  }
}

main().catch((err) => {
  process.stdout.write(JSON.stringify({ error: String(err) }) + "\n");
  process.exit(1);
});
