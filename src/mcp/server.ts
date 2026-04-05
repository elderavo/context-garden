/**
 * ContextGarden MCP server — wraps ContextEngine behind MCP.
 *
 * 8 tools:
 *   retrieve_context     — hybrid RAG query
 *   find_path            — graph path between two concepts
 *   rate_context         — feedback on retrieval quality
 *   register_workspace   — register a source directory
 *   unregister_workspace — remove a workspace
 *   list_workspaces      — list registered workspaces
 *   configure            — runtime config override (embed/LLM provider, model, API keys, etc.)
 *   setup                — guided 0-to-running onboarding (provider + workspace setup)
 *
 * Transport-agnostic: wire via StdioServerTransport.
 */

import { McpServer } from "@modelcontextprotocol/sdk/server/mcp.js";
import { z } from "zod";
import type { ContextEngine } from "../engine/context-engine.js";
import type { ContextPackage } from "../engine/types.js";
import type { WorkspaceRegistry } from "../workspace/registry.js";
import {
  updateConfig,
  getConfigSnapshot,
  type EmbedProvider,
  type LlmProvider,
} from "../config.js";
import { isLlmReachable } from "../llm/client.js";
import {
  ensureDefaultLanguages,
  getLanguage,
  listLanguages,
  type WorkspaceLanguage,
} from "../mirror/language-registry.js";

export type OnContextBuilt = (pkg: ContextPackage) => void;
export type OnContextRated = (rating: { retrievalId: string; query: string; score: number; missing: string; helpful: string }) => void;

export interface McpServerOptions {
  engine: ContextEngine;
  onContextBuilt?: OnContextBuilt;
  onContextRated?: OnContextRated;
  workspaceRegistry?: WorkspaceRegistry;
  onBackgroundError?: (context: string, err: unknown) => void;
}

export function createContextGardenMcpServer(opts: McpServerOptions): McpServer {
  const { engine, onContextBuilt, onContextRated, workspaceRegistry, onBackgroundError } = opts;
  const server = new McpServer({ name: "context-garden", version: "1.0.0" });

  const DEFAULT_MAX_CHARS = 15000;

  // ── retrieve_context ──────────────────────────────────────────────────────

  server.registerTool(
    "retrieve_context",
    {
      description:
        "Search the knowledge base for relevant notes, tools, concepts, and best " +
        "practices. Returns markdown-formatted context from the knowledge graph. " +
        "Call this before relying on your own knowledge for any project-specific question.",
      inputSchema: {
        query: z.string().describe("What to search for in the knowledge base."),
        workspace: z.string().optional().describe("Limit retrieval to a specific registered workspace by name. Omit to search all workspaces."),
        max_chars: z.number().optional().describe("Maximum characters to return (default: 15000)."),
      },
    },
    async ({ query, workspace, max_chars }) => {
      const retrievalId = `r-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 6)}`;
      const pkg = await engine.build(query, workspace);
      onContextBuilt?.(pkg);
      const budget = Math.max(500, Math.floor(max_chars ?? DEFAULT_MAX_CHARS));
      let text = pkg.formattedContext.length <= budget
        ? pkg.formattedContext
        : pkg.formattedContext.slice(0, budget) + "\n\n_(context truncated to fit budget)_\n<!-- End ContextGarden Context -->";
      text += `\n\n<!-- retrieval_id: ${retrievalId} -->`;
      return { content: [{ type: "text" as const, text }] };
    },
  );

  // ── find_path ───────────────────────────────────────────────────────────

  server.registerTool(
    "find_path",
    {
      description:
        "Find the shortest path between two concepts in the knowledge graph. " +
        "Endpoints can be note paths or natural language. " +
        "Returns the chain of notes and edge types connecting them.",
      inputSchema: {
        start: z.string().describe("Starting concept, note path, or search query."),
        end: z.string().describe("Ending concept, note path, or search query."),
        edge_types: z.array(z.string()).optional().describe(
          "Edge types to traverse. Default: all. Options: CALLS, DEFINED_IN, BELONGS_TO, CONTAINS, CONTAINS_SYMBOL, IMPORTS, LINKS_TO"
        ),
        max_depth: z.number().optional().describe("Maximum path length in hops (default: 8)."),
      },
    },
    async ({ start, end, edge_types, max_depth }) => {
      const result = await engine.findPath(start, end, {
        edgeTypes: edge_types,
        maxDepth: max_depth,
      });
      if (result.noPath) {
        const parts = [`No path found between "${start}" and "${end}" in the knowledge graph.`];
        if (result.startId) parts.push(`Start resolved to: ${result.startId} (${result.startResolvedBy})`);
        else parts.push(`Could not resolve start: "${start}"`);
        if (result.endId) parts.push(`End resolved to: ${result.endId} (${result.endResolvedBy})`);
        else parts.push(`Could not resolve end: "${end}"`);
        return { content: [{ type: "text" as const, text: parts.join("\n") }] };
      }
      let text = result.formattedContext;
      if (text.length > DEFAULT_MAX_CHARS) {
        text = text.slice(0, DEFAULT_MAX_CHARS) + "\n\n_(path context truncated)_\n<!-- End ContextGarden Path Context -->";
      }
      const header = `_Start: "${start}" → ${result.startId} (${result.startResolvedBy}) | End: "${end}" → ${result.endId} (${result.endResolvedBy})_\n\n`;
      text = header + text;
      return { content: [{ type: "text" as const, text }] };
    },
  );

  // ── rate_context ─────────────────────────────────────────────────────────

  server.registerTool(
    "rate_context",
    {
      description:
        "Rate how well the last retrieve_context result answered your query. " +
        "Your rating helps the knowledge base improve over time.",
      inputSchema: {
        retrieval_id: z.string().describe("The retrieval_id from the <!-- retrieval_id: ... --> comment in the retrieve_context response."),
        query: z.string().describe("The original query you searched for."),
        score: z.number().int().min(1).max(5).describe(
          "1 = completely irrelevant, 2 = mostly unhelpful, 3 = partially useful, 4 = good coverage, 5 = exactly what was needed."
        ),
        missing: z.string().describe("What information was missing? Empty string if nothing."),
        helpful: z.string().describe("Which notes or sections were most useful? Empty string if none."),
      },
    },
    async ({ retrieval_id, query, score, missing, helpful }) => {
      onContextRated?.({ retrievalId: retrieval_id, query, score, missing, helpful });
      return {
        content: [{
          type: "text" as const,
          text: `Rating recorded: ${score}/5. Thank you — this helps the knowledge base improve.`,
        }],
      };
    },
  );

  // ── Workspace tools ────────────────────────────────────────────────────

  const availableLanguageDescriptors = (() => {
    ensureDefaultLanguages();
    return listLanguages();
  })();
  const defaultLanguageList = availableLanguageDescriptors.length > 0
    ? [availableLanguageDescriptors[0].id]
    : [];
  const languageIdSchema = z.string().min(1).refine((id) => !!getLanguage(id), {
    message: "Unknown language id.",
  });

  server.registerTool(
    "register_workspace",
    {
      description:
        "Register a source directory as a workspace for code mirroring. " +
        "Runs an initial mirror pass and starts a file watcher for continuous updates. " +
        "Mirrored notes become available via retrieve_context.",
      inputSchema: {
        name: z.string().describe("Unique slug (lowercase alphanumeric + hyphens, 2-64 chars)."),
        source_dir: z.string().describe("Absolute path to the source directory to mirror."),
        languages: z.array(languageIdSchema).default(defaultLanguageList).describe("Which languages to mirror. Default: first registered language."),
      },
    },
    async ({ name, source_dir, languages }) => {
      if (!workspaceRegistry) {
        return { content: [{ type: "text" as const, text: "Workspace registry not available." }] };
      }

      const uniqueLanguages = [...new Set(languages)] as WorkspaceLanguage[];
      if (uniqueLanguages.length === 0) {
        return { content: [{ type: "text" as const, text: "At least one language must be specified." }] };
      }

      try {
        const { entry, notesGenerated, notePaths } = await workspaceRegistry.register({
          name,
          sourceDir: source_dir,
          languages: uniqueLanguages,
          active: true,
        });

        // Full reindex after registration — mirrors ObsidiClaw's behaviour of
        // scanning all notes after the mirror completes, rather than processing
        // them one-by-one through incremental_update (which previously lost
        // tier/parentFile/parentModule fields needed for graph edges).
        engine.reindex().catch((err) => {
          onBackgroundError?.("reindex after register_workspace", err);
        });

        return {
          content: [{
            type: "text" as const,
            text: `Workspace "${entry.name}" registered.\n` +
              `Source: ${entry.sourceDir}\n` +
              `Languages: ${entry.languages.join(", ")}\n` +
              `Notes generated: ${notesGenerated}\n` +
              `Indexing: running in background\n` +
              `Watcher: active`,
          }],
        };
      } catch (err) {
        return { content: [{ type: "text" as const, text: `Failed to register workspace: ${err}` }] };
      }
    },
  );

  server.registerTool(
    "list_workspaces",
    {
      description: "List all registered workspaces and their status.",
      inputSchema: {},
    },
    async () => {
      if (!workspaceRegistry) {
        return { content: [{ type: "text" as const, text: "Workspace registry not available." }] };
      }
      const entries = workspaceRegistry.list();
      if (entries.length === 0) {
        return { content: [{ type: "text" as const, text: "No workspaces registered." }] };
      }
      const lines = [
        "| Name | Languages | Source | Active | Registered |",
        "|------|-----------|--------|--------|------------|",
        ...entries.map((e) =>
          `| ${e.name} | ${e.languages.join(", ")} | ${e.sourceDir} | ${e.active ? "yes" : "no"} | ${e.registeredAt.slice(0, 10)} |`,
        ),
      ];
      return { content: [{ type: "text" as const, text: lines.join("\n") }] };
    },
  );

  server.registerTool(
    "unregister_workspace",
    {
      description:
        "Unregister a workspace: stops watcher, deletes mirrored notes, and removes from registry.",
      inputSchema: {
        name: z.string().describe("Workspace name to remove."),
      },
    },
    async ({ name }) => {
      if (!workspaceRegistry) {
        return { content: [{ type: "text" as const, text: "Workspace registry not available." }] };
      }
      try {
        const { removed, deletedPaths } = await workspaceRegistry.unregister(name);
        if (!removed) {
          return { content: [{ type: "text" as const, text: `Workspace "${name}" not found.` }] };
        }

        if (deletedPaths.length > 0) {
          try {
            await engine.incrementalUpdate([], deletedPaths);
          } catch {
            // Non-fatal
          }
        }

        return {
          content: [{
            type: "text" as const,
            text: `Workspace "${name}" unregistered.\n` +
              `Notes deleted: ${deletedPaths.length}\n` +
              `Watcher: stopped`,
          }],
        };
      } catch (err) {
        return { content: [{ type: "text" as const, text: `Failed to unregister workspace: ${err}` }] };
      }
    },
  );

  // ── configure ─────────────────────────────────────────────────────────

  server.registerTool(
    "configure",
    {
      description:
        "View or update ContextGarden configuration at runtime. " +
        "Pass no arguments to view current config. " +
        "Pass key-value overrides to change settings. " +
        "Set persist=true to save changes to config.json.",
      inputSchema: {
        embed_provider: z.enum(["ollama", "openai", "local"]).optional().describe("Embedding provider."),
        embed_model: z.string().optional().describe("Embedding model name."),
        embed_host: z.string().optional().describe("Embedding provider host URL."),
        embed_api_key: z.string().optional().describe("API key for the embedding provider."),
        llm_provider: z.enum(["ollama", "openai", "anthropic"]).optional().describe("LLM provider for synthesis."),
        llm_model: z.string().optional().describe("LLM model name."),
        llm_host: z.string().optional().describe("LLM provider host URL."),
        llm_api_key: z.string().optional().describe("API key for the LLM provider."),
        persist: z.boolean().optional().describe("Save changes to config.json (default: false)."),
      },
    },
    async ({ embed_provider, embed_model, embed_host, embed_api_key, llm_provider, llm_model, llm_host, llm_api_key, persist }) => {
      const hasOverrides = embed_provider || embed_model || embed_host || embed_api_key
        || llm_provider || llm_model || llm_host || llm_api_key;

      if (hasOverrides) {
        updateConfig(
          {
            embedProvider: embed_provider as EmbedProvider | undefined,
            embedModel: embed_model,
            embedHost: embed_host,
            embedApiKey: embed_api_key,
            llmProvider: llm_provider as LlmProvider | undefined,
            llmModel: llm_model,
            llmHost: llm_host,
            llmApiKey: llm_api_key,
          },
          persist ?? false,
        );
      }

      const snapshot = getConfigSnapshot();
      const lines = [
        "# ContextGarden Configuration",
        "",
        "## Embedding",
        `- Provider: ${snapshot.embedProvider}`,
        `- Model: ${snapshot.embedModel}`,
        `- Host: ${snapshot.embedHost}`,
        `- Context Length: ${snapshot.embedContextLength}`,
        `- API Key: ${snapshot.embedApiKey ? "set" : "not set"}`,
        "",
        "## Synthesizer (LLM)",
        `- Provider: ${snapshot.llmProvider}`,
        `- Model: ${snapshot.llmModel}`,
        `- Host: ${snapshot.llmHost}`,
        `- Context Window: ${snapshot.llmContextWindow}`,
        `- Max Tokens: ${snapshot.llmMaxTokens}`,
        `- API Key: ${snapshot.llmApiKey ? "set" : "not set"}`,
      ];

      if (hasOverrides) {
        lines.push("", persist ? "_Changes saved to config.json._" : "_Changes applied for this session only._");
      }

      return { content: [{ type: "text" as const, text: lines.join("\n") }] };
    },
  );

  // ── setup ─────────────────────────────────────────────────────────────────

  server.registerTool(
    "setup",
    {
      description:
        "Guided 0-to-running setup for ContextGarden. " +
        "Call with no arguments to see current status and what's needed. " +
        "Pass provider config, API keys, and/or workspace info to configure in one call. " +
        "Provider defaults are applied automatically when you choose a provider without specifying model/host.",
      inputSchema: {
        // Embedding provider
        embed_provider: z.enum(["ollama", "openai", "local"]).optional().describe(
          "Embedding provider. Defaults: openai→text-embedding-3-small, ollama→(specify model), local→nomic-embed-text:latest"
        ),
        embed_model:   z.string().optional().describe("Override embedding model (optional if provider has a default)."),
        embed_host:    z.string().optional().describe("Override embedding host URL (optional if provider has a default)."),
        embed_api_key: z.string().optional().describe("API key for the embedding provider (required for openai)."),

        // LLM / synthesizer provider
        llm_provider: z.enum(["ollama", "openai", "anthropic"]).optional().describe(
          "LLM provider for context synthesis. Defaults: openai→gpt-4o, anthropic→claude-3-5-haiku-latest, ollama→(specify model)"
        ),
        llm_model:   z.string().optional().describe("Override LLM model (optional if provider has a default)."),
        llm_host:    z.string().optional().describe("Override LLM host URL (optional if provider has a default)."),
        llm_api_key: z.string().optional().describe("API key for the LLM provider (required for openai/anthropic)."),

        // Workspace registration
        workspace_name:       z.string().optional().describe("Slug for the workspace (lowercase alphanumeric + hyphens, 2-64 chars)."),
        workspace_source_dir: z.string().optional().describe("Absolute path to the source directory to mirror."),
        workspace_languages:  z.array(z.string()).optional().describe(`Languages to mirror. Available: ${availableLanguageDescriptors.map((d) => d.id).join(", ")}`),

        // Control
        persist:         z.boolean().optional().describe("Persist provider config to config.json (default: true)."),
        test_connection: z.boolean().optional().describe("Test LLM provider connectivity after applying config (default: false)."),
      },
    },
    async ({
      embed_provider, embed_model, embed_host, embed_api_key,
      llm_provider, llm_model, llm_host, llm_api_key,
      workspace_name, workspace_source_dir, workspace_languages,
      persist, test_connection,
    }) => {
      const lines: string[] = [];

      // ── 1. Apply provider-based defaults ──────────────────────────────────
      const resolvedEmbedModel = embed_model
        ?? (embed_provider === "openai" ? "text-embedding-3-small" : undefined)
        ?? (embed_provider === "local"  ? "nomic-embed-text:latest" : undefined);
      const resolvedEmbedHost = embed_host
        ?? (embed_provider === "openai" ? "https://api.openai.com" : undefined)
        ?? ((embed_provider === "local" || embed_provider === "ollama") ? "http://localhost:11434" : undefined);

      const resolvedLlmModel = llm_model
        ?? (llm_provider === "openai"    ? "gpt-4o" : undefined)
        ?? (llm_provider === "anthropic" ? "claude-3-5-haiku-latest" : undefined);
      const resolvedLlmHost = llm_host
        ?? (llm_provider === "openai"    ? "https://api.openai.com" : undefined)
        ?? (llm_provider === "anthropic" ? "https://api.anthropic.com" : undefined)
        ?? (llm_provider === "ollama"    ? "http://localhost:11434" : undefined);

      // ── 2. Apply config overrides ──────────────────────────────────────────
      const hasProviderOverrides = embed_provider || resolvedEmbedModel || resolvedEmbedHost || embed_api_key
        || llm_provider || resolvedLlmModel || resolvedLlmHost || llm_api_key;

      if (hasProviderOverrides) {
        updateConfig(
          {
            embedProvider: embed_provider as EmbedProvider | undefined,
            embedModel: resolvedEmbedModel,
            embedHost: resolvedEmbedHost,
            embedApiKey: embed_api_key,
            llmProvider: llm_provider as LlmProvider | undefined,
            llmModel: resolvedLlmModel,
            llmHost: resolvedLlmHost,
            llmApiKey: llm_api_key,
          },
          persist ?? true,
        );
        lines.push("_Provider config updated._", "");
      }

      // ── 3. Register workspace if requested ───────────────────────────────
      let workspaceResult = "";
      if (workspace_name && workspace_source_dir) {
        if (!workspaceRegistry) {
          workspaceResult = "⚠ Workspace registry not available.";
        } else {
          const langs = (workspace_languages ?? defaultLanguageList) as WorkspaceLanguage[];
          try {
            const { entry, notesGenerated } = await workspaceRegistry.register({
              name: workspace_name,
              sourceDir: workspace_source_dir,
              languages: langs,
              active: true,
            });
            engine.reindex().catch((err) => {
              onBackgroundError?.("reindex after setup workspace", err);
            });
            workspaceResult = `✓ Workspace "${entry.name}" registered — ${notesGenerated} notes generated, indexing in background.`;
          } catch (err) {
            workspaceResult = `⚠ Workspace registration failed: ${err}`;
          }
        }
      } else if (workspace_name || workspace_source_dir) {
        workspaceResult = "⚠ Both workspace_name and workspace_source_dir are required to register a workspace.";
      }

      // ── 4. Test connectivity ───────────────────────────────────────────────
      let connectivityResult = "";
      if (test_connection) {
        const reachable = await isLlmReachable();
        connectivityResult = reachable
          ? "✓ LLM provider is reachable."
          : "⚠ LLM provider is NOT reachable — check host, model, and API key.";
      }

      // ── 5. Build status report ─────────────────────────────────────────────
      const snap = getConfigSnapshot();

      const embedKeyNeeded = (snap.embedProvider === "openai") && !snap.embedApiKey;
      const llmKeyNeeded   = (snap.llmProvider === "openai" || snap.llmProvider === "anthropic") && !snap.llmApiKey;

      lines.push("## ContextGarden Setup", "");

      lines.push("### Embedding Provider");
      lines.push(`- Provider: ${snap.embedProvider}`);
      lines.push(`- Model:    ${snap.embedModel}`);
      lines.push(`- Host:     ${snap.embedHost}`);
      lines.push(snap.embedApiKey ? "- API Key:  ✓ set" : embedKeyNeeded ? "- API Key:  ⚠ not set (required for this provider)" : "- API Key:  — not required");
      lines.push("");

      lines.push("### LLM Provider (Synthesizer)");
      lines.push(`- Provider: ${snap.llmProvider}`);
      lines.push(`- Model:    ${snap.llmModel}`);
      lines.push(`- Host:     ${snap.llmHost}`);
      lines.push(snap.llmApiKey ? "- API Key:  ✓ set" : llmKeyNeeded ? "- API Key:  ⚠ not set (required for this provider)" : "- API Key:  — not required");
      lines.push("");

      lines.push("### Workspaces");
      const workspaces = workspaceRegistry?.list() ?? [];
      if (workspaceResult) lines.push(workspaceResult, "");
      if (workspaces.length === 0) {
        lines.push("_(none registered)_");
      } else {
        for (const ws of workspaces) {
          lines.push(`- ✓ **${ws.name}** — ${ws.sourceDir}  [${ws.languages.join(", ")}]`);
        }
      }
      lines.push("");

      if (connectivityResult) {
        lines.push("### Connectivity", connectivityResult, "");
      }

      // ── 6. Next steps ──────────────────────────────────────────────────────
      const issues: string[] = [];
      if (embedKeyNeeded) issues.push(`- Provide \`embed_api_key\` (or set \`${snap.embedProvider === "openai" ? "OPENAI_API_KEY" : "CG_EMBED_API_KEY"}\` env var)`);
      if (llmKeyNeeded)   issues.push(`- Provide \`llm_api_key\` (or set \`${snap.llmProvider === "openai" ? "OPENAI_API_KEY" : "ANTHROPIC_API_KEY"}\` env var)`);
      if (workspaces.length === 0) issues.push("- Register a workspace: provide `workspace_name`, `workspace_source_dir`, and optionally `workspace_languages`");

      lines.push("### Next Steps");
      if (issues.length === 0 && workspaces.length > 0) {
        lines.push("✓ Setup looks complete. Call `setup(test_connection=true)` to verify provider connectivity.");
        lines.push("  Then try `retrieve_context` with a query about your codebase.");
      } else {
        lines.push(...issues);
        if (issues.length === 0) lines.push("✓ All set — try `retrieve_context` with a query.");
      }

      lines.push("");
      lines.push("_Tip: You can provide multiple params in one call, e.g. `setup(embed_provider=\"openai\", embed_api_key=\"sk-...\", workspace_name=\"my-app\", workspace_source_dir=\"/path/to/app\", workspace_languages=[\"ts\"])`_");

      return { content: [{ type: "text" as const, text: lines.join("\n") }] };
    },
  );

  return server;
}
