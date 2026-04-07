/**
 * ContextEngine — bridges MCP tool calls to the persistent Python daemon.
 *
 * In Phase 2, the Python graph service is no longer a stdio subprocess.
 * It runs as a persistent TCP daemon (127.0.0.1:7432). This class:
 *   - Resolves the Python executable (conda env contextgarden)
 *   - Connects via DaemonClient (spawning the daemon if not running)
 *   - Registers the md_db path as a workspace on first connect
 *   - Delegates all retrieval/index operations over TCP
 *
 * What stays in TS (unchanged):
 *   - Context formatting (formatContext, formatPathContext)
 *   - Context reviewer (LLM synthesis call)
 *   - Debug event emission (ce_* events)
 *   - All public types
 */

import { dirname, join } from "path";
import { fileURLToPath } from "url";
import { EventEmitter } from "events";
import { mkdirSync } from "fs";
import { getConfig } from "../config.js";
import { stripFrontmatter, estimateTokens } from "./frontmatter-utils.js";
import { ContextReviewer } from "./review/context-reviewer.js";
import { DaemonClient, resolvePythonPath } from "./daemon-client.js";
import type {
  ContextEngineConfig,
  ContextEngineEvent,
  ContextPackage,
  PathResult,
  PathStep,
  RetrievedNote,
} from "./types.js";

const DEFAULT_TOP_K = 5;

// Returned verbatim when RAG produces zero results.
const NOTHING_FOUND_CONTEXT =
  `## Nothing Found\n\n` +
  `No notes in this workspace match this query.\n\n` +
  `**Next step**: Investigate the codebase directly (read files, search for symbols).`;

// ---------------------------------------------------------------------------
// ContextEngine
// ---------------------------------------------------------------------------

export class ContextEngine extends EventEmitter {
  private client: DaemonClient | null = null;
  private workspaceId: string | null = null;
  private initialized = false;

  private readonly config: Required<Omit<ContextEngineConfig, "review" | "onDebug">> & {
    review?: ContextEngineConfig["review"];
  };
  private readonly onDebug: ((event: ContextEngineEvent) => void) | undefined;
  private readonly reviewer: ContextReviewer | null;

  constructor(config: ContextEngineConfig) {
    super();
    const mdDbPath = config.mdDbPath;
    const defaultDbPath = join(dirname(mdDbPath), ".context-garden", "knowledge_graph");

    this.config = {
      mdDbPath,
      dbPath: config.dbPath ?? defaultDbPath,
      topK: config.topK ?? DEFAULT_TOP_K,
      personalitiesDir: config.personalitiesDir ?? join(dirname(fileURLToPath(import.meta.url)), "..", "data"),
      review: config.review,
    };

    this.onDebug = config.onDebug;

    this.reviewer = config.review?.enabled === false
      ? null
      : new ContextReviewer({
          ...(config.review ?? {}),
          personalitiesDir: this.config.personalitiesDir,
        });
  }

  // =========================================================================
  // Public API
  // =========================================================================

  /**
   * Connect to the daemon and register the md_db workspace. Idempotent.
   */
  async initialize(): Promise<void> {
    if (this.initialized) return;

    const t0 = Date.now();
    this.debug({ type: "ce_init_start", timestamp: t0, path: "daemon" });

    mkdirSync(this.config.dbPath, { recursive: true });

    await this._ensureClient();

    // Register the md_db root as a workspace (idempotent — daemon dedupes by root_path)
    const wsName = "default"; // single-workspace mode
    const result = await this.client!.rpc("workspaces.register", {
      name: wsName,
      root_path: this.config.mdDbPath,
    }) as { workspace_id: string; existed: boolean; doc_count?: number };

    this.workspaceId = result.workspace_id;

    this.initialized = true;

    this.debug({
      type: "ce_init_end",
      timestamp: Date.now(),
      path: result.existed ? "daemon-existing" : "daemon-new",
      durationMs: Date.now() - t0,
      noteCount: result.doc_count ?? 0,
    });
  }

  /**
   * Build a ContextPackage for the given prompt.
   *
   * @param prompt      Natural-language intent — sent to the synthesizer LLM.
   * @param workspace   Optional workspace filter.
   * @param searchTerms Keyword/embedding string for the vector DB. Defaults to prompt.
   */
  async build(prompt: string, workspace?: string, searchTerms?: string): Promise<ContextPackage> {
    this._ensureInitialized();

    const vectorQuery = searchTerms ?? prompt;

    const t0 = Date.now();
    this.debug({ type: "ce_retrieval_start", timestamp: t0, query: vectorQuery.slice(0, 200), intent: prompt.slice(0, 200), topK: this.config.topK });

    const tVector = Date.now();
    const rpcResult = await this.client!.rpc("query.retrieve", {
      workspace_id: this.workspaceId,
      query: vectorQuery,
      top_k: this.config.topK,
      ...(workspace ? { workspace } : {}),
    }) as { seed_notes: RpcRetrievedNote[]; expanded_notes: RpcRetrievedNote[] };

    const seedNotes = rpcResult.seed_notes.map(rpcNoteToRetrievedNote);
    const expandedNotes = rpcResult.expanded_notes.map(rpcNoteToRetrievedNote);

    this.debug({ type: "ce_vector_done", timestamp: Date.now(), seedCount: seedNotes.length, durationMs: Date.now() - tVector });

    if (seedNotes.length === 0 && expandedNotes.length === 0) {
      return {
        query: prompt,
        ...(searchTerms ? { searchTerms } : {}),
        retrievedNotes: [],
        suggestedTools: [],
        formattedContext: NOTHING_FOUND_CONTEXT,
        retrievalMs: Date.now() - t0,
        builtAt: Date.now(),
        seedNoteIds: [],
        expandedNoteIds: [],
        rawChars: 0,
        strippedChars: NOTHING_FOUND_CONTEXT.length,
        estimatedTokens: estimateTokens(NOTHING_FOUND_CONTEXT),
        reviewResult: { reviewMs: 0, skipped: true, skipReason: "no_notes" },
      };
    }

    const tGraph = Date.now();
    const allNotes = [...seedNotes, ...expandedNotes].sort((a, b) => b.score - a.score);
    this.debug({ type: "ce_graph_done", timestamp: Date.now(), expandedCount: expandedNotes.length, durationMs: Date.now() - tGraph });

    const suggestedTools = allNotes
      .filter((n) => n.type === "tool" && n.toolId !== undefined)
      .map((n) => n.toolId!);

    const rawChars = allNotes.reduce((sum, n) => sum + n.content.length, 0);

    const filteredSeeds = allNotes.filter((n) => n.depth === 0 || n.retrievalSource === "vector");
    const filteredExpanded = allNotes.filter((n) => (n.depth ?? 0) > 0 && n.retrievalSource !== "vector");
    const rawFormattedContext = formatContext(filteredSeeds, filteredExpanded);

    let formattedContext = rawFormattedContext;
    let reviewResult: ContextPackage["reviewResult"] | undefined;

    if (this.reviewer) {
      const avgScore = allNotes.length > 0 ? allNotes.reduce((sum, n) => sum + n.score, 0) / allNotes.length : 0;
      this.debug({ type: "ce_review_start", timestamp: Date.now(), noteCount: allNotes.length, avgScore });

      const review = await this.reviewer.review(prompt, allNotes, rawFormattedContext);
      reviewResult = {
        reviewMs: review.reviewMs,
        skipped: review.skipped,
        skipReason: review.skipReason,
      };

      this.debug({
        type: "ce_review_done",
        timestamp: Date.now(),
        skipped: review.skipped,
        skipReason: review.skipReason,
        reviewMs: review.reviewMs,
        inputChars: rawFormattedContext.length,
        outputChars: review.synthesizedContext?.length,
      });

      if (!review.skipped && review.synthesizedContext) {
        formattedContext = review.synthesizedContext;
      }
    }


    const retrievalMs = Date.now() - t0;

    return {
      query: prompt,
      ...(searchTerms ? { searchTerms } : {}),
      retrievedNotes: allNotes,
      suggestedTools,
      formattedContext,
      retrievalMs,
      builtAt: Date.now(),
      seedNoteIds: filteredSeeds.map((n) => n.noteId),
      expandedNoteIds: filteredExpanded.map((n) => n.noteId),
      rawChars,
      strippedChars: formattedContext.length,
      estimatedTokens: estimateTokens(formattedContext),
      reviewResult,
    };
  }

  /**
   * Find the shortest graph path between two endpoints.
   */
  async findPath(
    start: string,
    end: string,
    options?: { edgeTypes?: string[]; maxDepth?: number },
  ): Promise<PathResult> {
    this._ensureInitialized();
    const t0 = Date.now();

    const rpcResult = await this.client!.rpc("query.find_path", {
      workspace_id: this.workspaceId,
      start,
      end,
      ...(options?.edgeTypes ? { edge_types: options.edgeTypes } : {}),
      ...(options?.maxDepth ? { max_depth: options.maxDepth } : {}),
    }) as {
      start_id: string;
      end_id: string;
      start_resolved_by: string;
      end_resolved_by: string;
      path_length: number;
      path_steps: Array<{ nodeId: string; edgeLabel: string; edgeDirection: string; fromNodeId: string }>;
      path_notes: RpcRetrievedNote[];
      no_path: boolean;
      duration_ms: number;
    };

    const pathNotes = rpcResult.path_notes.map(rpcNoteToRetrievedNote);
    const pathSteps: PathStep[] = rpcResult.path_steps.map((s) => ({
      nodeId: s.nodeId,
      edgeLabel: s.edgeLabel,
      edgeDirection: s.edgeDirection as PathStep["edgeDirection"],
      fromNodeId: s.fromNodeId,
    }));

    let formattedContext = rpcResult.no_path
      ? `<!-- ContextGarden: no graph path found between "${start}" and "${end}" -->`
      : formatPathContext(pathSteps, pathNotes);

    let reviewResult: PathResult["reviewResult"];
    if (!rpcResult.no_path && this.reviewer && pathNotes.length > 0) {
      const pathQuery = `How does "${start}" connect to "${end}"?`;
      const review = await this.reviewer.review(pathQuery, pathNotes, formattedContext);
      reviewResult = { reviewMs: review.reviewMs, skipped: review.skipped, skipReason: review.skipReason };
      if (!review.skipped && review.synthesizedContext) {
        formattedContext = review.synthesizedContext;
      }
    }

    return {
      startId: rpcResult.start_id,
      endId: rpcResult.end_id,
      startResolvedBy: rpcResult.start_resolved_by as PathResult["startResolvedBy"],
      endResolvedBy: rpcResult.end_resolved_by as PathResult["endResolvedBy"],
      pathLength: rpcResult.path_length,
      pathSteps,
      pathNotes,
      formattedContext,
      retrievalMs: Date.now() - t0,
      noPath: rpcResult.no_path,
      reviewResult,
    };
  }

  /**
   * Get the body of a note by relative path.
   * Fetches from the daemon — no local cache in Phase 2.
   */
  async getNoteContentAsync(relativePath: string): Promise<string | null> {
    if (!this.initialized) return null;
    try {
      const result = await this.client!.rpc("query.get_note_content", {
        workspace_id: this.workspaceId,
        relative_path: relativePath,
      }) as { body: string | null };
      return result.body;
    } catch {
      return null;
    }
  }

  /** Synchronous shim — returns null; use getNoteContentAsync for actual content. */
  getNoteContent(_relativePath: string): string | null {
    return null;
  }

  /**
   * Get graph statistics.
   */
  async getGraphStats(): Promise<{ noteCount: number; edgeCount: number; indexLoaded: boolean }> {
    if (!this.initialized) return { noteCount: 0, edgeCount: 0, indexLoaded: false };
    const result = await this.client!.rpc("query.stats", {
      workspace_id: this.workspaceId,
    }) as { doc_count: number; edge_count: number; index_loaded: boolean };
    return {
      noteCount: result.doc_count,
      edgeCount: result.edge_count,
      indexLoaded: result.index_loaded,
    };
  }

  /**
   * Trigger a full reindex of the workspace.
   */
  async reindex(): Promise<void> {
    this._ensureInitialized();
    const t0 = Date.now();
    this.debug({ type: "ce_reindex_start", timestamp: t0, path: "daemon" });

    await this.client!.rpc("index.rebuild", { workspace_id: this.workspaceId });

    this.debug({ type: "ce_reindex_done", timestamp: Date.now(), durationMs: Date.now() - t0, noteCount: 0, skipped: false });
  }

  /**
   * Queue an incremental index update. The daemon's background indexer does the work.
   */
  async incrementalUpdate(changedPaths: string[], deletedPaths: string[] = []): Promise<void> {
    this._ensureInitialized();
    const result = await this.client!.rpc("index.enqueue", {
      workspace_id: this.workspaceId,
      changed_paths: changedPaths,
      deleted_paths: deletedPaths,
    }) as { job_id: string; queue_depth: number };

    this.debug({
      type: "ce_subprocess_log",
      timestamp: Date.now(),
      message: `Incremental update enqueued (queue_depth=${result.queue_depth})`,
    });
  }

  /**
   * Disconnect from the daemon. If we spawned it, signals shutdown.
   */
  async close(): Promise<void> {
    if (this.client) {
      await this.client.close();
      this.client = null;
    }
    this.initialized = false;
    this.workspaceId = null;
  }

  // =========================================================================
  // Daemon connection
  // =========================================================================

  private async _ensureClient(): Promise<void> {
    if (this.client) return;

    const cgConfig = getConfig();
    const cacheFile = join(this.config.dbPath, "..", ".python_path");
    const pythonPath = resolvePythonPath(cacheFile);
    const daemonCwd = dirname(this.config.mdDbPath);

    this.client = new DaemonClient(pythonPath, daemonCwd, cgConfig.dataDir);
    await this.client.connect();
  }

  // =========================================================================
  // Helpers
  // =========================================================================

  private _ensureInitialized(): void {
    if (!this.initialized) {
      throw new Error("ContextEngine not initialized. Call initialize() first.");
    }
  }

  private debug(event: ContextEngineEvent): void {
    this.onDebug?.(event);
  }
}

// ---------------------------------------------------------------------------
// RPC note type (matches Python daemon output)
// ---------------------------------------------------------------------------

interface RpcRetrievedNote {
  noteId: string;
  path: string;
  content: string;
  score: number;
  type: string;
  toolId?: string | null;
  tags?: string[] | null;
  retrievalSource: string;
  linkedFrom?: string[] | null;
  depth?: number | null;
  title?: string | null;
  viaEdge?: string | null;
  viaSourceTitle?: string | null;
}

function rpcNoteToRetrievedNote(n: RpcRetrievedNote): RetrievedNote {
  return {
    noteId: n.noteId,
    path: n.path,
    content: n.content,
    score: n.score,
    type: n.type as RetrievedNote["type"],
    toolId: n.toolId ?? undefined,
    tags: n.tags ?? undefined,
    retrievalSource: n.retrievalSource as RetrievedNote["retrievalSource"],
    linkedFrom: n.linkedFrom ?? undefined,
    depth: n.depth ?? undefined,
    title: n.title ?? undefined,
    viaEdge: n.viaEdge ?? undefined,
    viaSourceTitle: n.viaSourceTitle ?? undefined,
  };
}

// ---------------------------------------------------------------------------
// Context formatting — tier-aware
// ---------------------------------------------------------------------------

const _EDGE_READABLE: Record<string, string> = {
  CALLS: "calls",
  CONTAINS_SYMBOL: "contains",
  DEFINED_IN: "defined in",
  CONTAINS: "contains file",
  IMPORTS: "imports",
  LINKS_TO: "links to",
  BELONGS_TO: "belongs to",
};

function _edgeArrow(edgeLabel: string): string {
  return `—[${_EDGE_READABLE[edgeLabel] ?? edgeLabel.toLowerCase()}]→`;
}

function formatContext(seedNotes: RetrievedNote[], expandedNotes: RetrievedNote[]): string {
  const allNotes = [...seedNotes, ...expandedNotes];

  if (allNotes.length === 0) {
    return "<!-- ContextGarden: no relevant knowledge base context found for this query -->";
  }

  const lines: string[] = [
    "<!-- ContextGarden Knowledge Base Context -->",
    "",
    "# Knowledge Base Context",
    "",
  ];

  const tier3 = allNotes.filter((n) => n.tier === "3");
  const tier2 = allNotes.filter((n) => n.tier === "2");
  const tier1 = allNotes.filter((n) => n.tier === "1");
  const concepts = allNotes.filter((n) => !n.tier && n.type === "concept");
  const tools = allNotes.filter((n) => n.type === "tool");
  const other = allNotes.filter(
    (n) => !n.tier && n.type !== "concept" && n.type !== "tool",
  );

  const renderNote = (note: RetrievedNote, labelSuffix = "", extraMeta = "") => {
    const seedMark = note.depth === 0 || note.retrievalSource === "vector" ? "*" : "";
    const meta = [seedMark, `score: ${note.score.toFixed(3)}`, extraMeta].filter(Boolean).join(" | ");
    lines.push(`### ${note.path}${labelSuffix} (${meta})`);
    // #4 — path annotation: show which edge + source brought this note in
    if (note.viaEdge && note.viaSourceTitle) {
      const arrow = _edgeArrow(note.viaEdge);
      lines.push(`_via: ${note.viaSourceTitle} ${arrow} ${note.viaEdge} ${arrow} here_`);
    }
    lines.push(stripFrontmatter(note.content));
    lines.push("");
  };

  if (tier3.length > 0) {
    lines.push("## Module Context");
    lines.push("_Architecture-level: what each subsystem/directory does._");
    lines.push("");
    for (const note of tier3) renderNote(note);
  }

  if (tier2.length > 0) {
    lines.push("## File Context");
    lines.push("_File-level: exports, imports, and role within the module._");
    lines.push("");
    for (const note of tier2) renderNote(note);
  }

  if (tier1.length > 0) {
    const tier1Ids = new Set(tier1.map((n) => n.noteId));
    const callRelated = tier1.filter(
      (n) => n.linkedFrom && n.linkedFrom.some((id) => tier1Ids.has(id)),
    );
    const callRelatedIds = new Set(callRelated.map((n) => n.noteId));
    const directSymbols = tier1.filter((n) => !callRelatedIds.has(n.noteId));

    lines.push("## Symbol Details");
    lines.push("_Symbol-level: specific function/class/type signatures and behavior._");
    lines.push("");
    for (const note of directSymbols) {
      const kindLabel = note.symbolKind ? ` [${note.symbolKind}]` : "";
      renderNote(note, kindLabel);
    }

    if (callRelated.length > 0) {
      lines.push("## Call Relationships");
      lines.push("_Symbols reached via CALLS/IMPORTS edges from the above._");
      lines.push("");
      for (const note of callRelated) {
        const kindLabel = note.symbolKind ? ` [${note.symbolKind}]` : "";
        const callers = note.linkedFrom!.filter((id) => tier1Ids.has(id));
        const callerMeta = callers.length > 0 ? `called by: ${callers.join(", ")}` : "";
        renderNote(note, kindLabel, callerMeta);
      }
    }
  }

  if (concepts.length > 0) {
    lines.push("## Concepts & Patterns");
    lines.push("_Design heuristics, failure modes, best practices._");
    lines.push("");
    for (const note of concepts) renderNote(note);
  }

  if (other.length > 0) {
    lines.push("## Additional Context");
    lines.push("");
    for (const note of other) renderNote(note);
  }

  if (tools.length > 0) {
    lines.push("## Suggested Tools");
    lines.push("_Live tools for real-time data._");
    lines.push("");
    for (const note of tools) {
      lines.push(`### Tool: ${note.toolId} (${note.path})`);
      lines.push(stripFrontmatter(note.content));
      lines.push("");
    }
  }

  lines.push("_* = directly retrieved by semantic similarity_");
  lines.push("<!-- End ContextGarden Context -->");

  return lines.join("\n");
}

// ---------------------------------------------------------------------------
// formatPathContext — graph path retrieval (unchanged)
// ---------------------------------------------------------------------------

function formatPathContext(pathSteps: PathStep[], pathNotes: RetrievedNote[]): string {
  if (pathNotes.length === 0) {
    return "<!-- ContextGarden: empty graph path -->";
  }

  const noteByPath = new Map(pathNotes.map((n) => [n.noteId, n]));
  const startNote = pathNotes[0];
  const endNote = pathNotes[pathNotes.length - 1];

  const lines: string[] = [
    "<!-- ContextGarden Knowledge Graph Path -->",
    "",
    `# Graph Path: ${startNote.path} → ${endNote.path}`,
    `_Shortest path (${pathSteps.length - 1} hop${pathSteps.length - 1 === 1 ? "" : "s"})._`,
    "",
  ];

  for (let i = 0; i < pathSteps.length; i++) {
    const step = pathSteps[i];
    const note = noteByPath.get(step.nodeId);

    if (i > 0 && step.edgeLabel) {
      const arrow = step.edgeDirection === "outgoing" ? "→" : "←";
      lines.push(`  ${arrow} **${step.edgeLabel}**`);
      lines.push("");
    }

    const posLabel = i === 0 ? " (start)" : i === pathSteps.length - 1 ? " (end)" : "";
    lines.push(`## ${step.nodeId}${posLabel}`);

    if (note) {
      lines.push(stripFrontmatter(note.content));
    } else {
      lines.push("_(note not found in cache)_");
    }
    lines.push("");
  }

  const edgeSummary = pathSteps
    .slice(1)
    .map((s) => {
      const arrow = s.edgeDirection === "outgoing" ? "→" : "←";
      return `--${s.edgeLabel}${arrow}`;
    });
  if (edgeSummary.length > 0) {
    lines.push(`_Path: ${pathSteps[0].nodeId} ${edgeSummary.map((e, i) => `${e} ${pathSteps[i + 1].nodeId}`).join(" ")}_`);
  }
  lines.push("<!-- End ContextGarden Path Context -->");

  return lines.join("\n");
}
