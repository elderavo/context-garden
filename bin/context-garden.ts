#!/usr/bin/env node
/**
 * ContextGarden CLI entry point.
 *
 * Starts the MCP server over stdio transport.
 * Ships empty — workspaces are registered at runtime via MCP tools.
 *
 * Usage:
 *   npx tsx bin/context-garden.ts [--data-dir <path>]
 *
 * Environment:
 *   CG_DATA_DIR — override data directory (default: cwd)
 */

import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import { createStack } from "../src/stack.js";

async function main(): Promise<void> {
  // Parse --data-dir from argv
  let dataDir: string | undefined;
  for (let i = 2; i < process.argv.length; i++) {
    if (process.argv[i] === "--data-dir" && process.argv[i + 1]) {
      dataDir = process.argv[++i];
    }
  }
  dataDir = dataDir ?? process.env["CG_DATA_DIR"];

  // Create the full stack
  const stack = await createStack(dataDir);

  // Wire up stdio transport
  const transport = new StdioServerTransport();
  await stack.mcpServer.connect(transport);

  // Graceful shutdown
  const shutdown = async () => {
    await stack.shutdown();
    process.exit(0);
  };

  process.on("SIGINT", () => void shutdown());
  process.on("SIGTERM", () => void shutdown());

  // Log to stderr (stdout is the MCP RPC channel)
  process.stderr.write("[context-garden] MCP server started on stdio\n");
}

main().catch((err) => {
  process.stderr.write(`[context-garden] Fatal: ${err}\n`);
  process.exit(1);
});
