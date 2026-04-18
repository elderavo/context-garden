/**
 * Stack factory — creates engine + registry + watchers.
 *
 * Single entry point for wiring all ContextGarden components together.
 */

import { join } from "path";
import { mkdirSync } from "fs";
import { initConfig, resolvePaths, type ContextGardenConfig } from "./config.js";
import { ContextEngine } from "./engine/context-engine.js";
import { WorkspaceRegistry } from "./workspace/registry.js";
import { startMdDbLintWatcher } from "./workspace/lint-watcher.js";
import { createContextGardenMcpServer } from "./mcp/server.js";
import type { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import type { FSWatcher } from "chokidar";

export interface StackComponents {
  config: ContextGardenConfig;
  engine: ContextEngine;
  registry: WorkspaceRegistry;
  mcpServer: McpServer;
  lintWatcher: FSWatcher;
  shutdown: () => Promise<void>;
}

export async function createStack(dataDir?: string): Promise<StackComponents> {
  // Initialize config
  const config = initConfig(dataDir);
  const paths = resolvePaths(config.dataDir);

  // Ensure directories exist
  mkdirSync(paths.mdDbPath, { recursive: true });
  mkdirSync(join(config.dataDir, ".context-garden"), { recursive: true });

  // Create engine
  const engine = new ContextEngine({
    mdDbPath: paths.mdDbPath,
    dbPath: paths.graphDbPath ? join(config.dataDir, ".context-garden", "knowledge_graph") : undefined,
    personalitiesDir: paths.personalitiesDir,
  });

  // Initialize engine
  await engine.initialize();

  // Create workspace registry
  const registry = new WorkspaceRegistry(
    paths.workspacesPath,
    paths.mdDbPath,
    config.dataDir,
  );
  registry.load();

  // Sync daemon with workspaces.json (idempotent — daemon reads file directly).
  await engine.syncDaemonWorkspaces();

  // Catch any source changes that happened while the MCP server was down,
  // then start watchers for ongoing changes.
  await registry.mirrorAllWorkspaces();
  registry.startAllWatchers();

  // Start lint watcher
  const lintWatcher = startMdDbLintWatcher(paths.mdDbPath);

  // Create MCP server
  const mcpServer = createContextGardenMcpServer({
    engine,
    workspaceRegistry: registry,
  });

  // Shutdown function
  async function shutdown(): Promise<void> {
    await registry.stopAllWatchers();
    await lintWatcher.close();
    await engine.close();
  }

  return {
    config,
    engine,
    registry,
    mcpServer,
    lintWatcher,
    shutdown,
  };
}
