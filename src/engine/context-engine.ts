/**
 * ContextEngine — subprocess bridge to the Python graph service.
 *
 * Public API is identical to the original implementation.
 * Internally, all vector/graph operations are delegated to a long-lived
 * Python subprocess (graph) via JSON-RPC over stdin/stdout.
 *
 * What stays in TS:
 *   - Context formatting (formatContext)
 *   - Context reviewer (LLM synthesis call)
 *   - Debug event emission (ce_* events)
 *   - All public types
 *
 * What moves to Python:
 *   - md_db scanning/parsing
 *   - PropertyGraphIndex (LlamaIndex + BFS)
 *   - Hybrid retrieval (VectorContextRetriever + tag boosting)
 *   - Hash-based change detection
 */

import { dirname, join } from "path";
import { fileURLToPath } from "url";
import { randomUUID } from "crypto";
import { EventEmitter } from "events";
import { spawn, execSync, type ChildProcess } from "child_process";
import { createInterface, type Interface as ReadlineInterface } from "readline";
import { existsSync, readFileSync, writeFileSync } from "fs";
import { ensureDir } from "../util/fs.js";
import { getConfig } from "../config.js";
import { stripFrontmatter, estimateTokens } from "./frontmatter-utils.js";
import { ContextReviewer } from "./review/context-reviewer.js";
import type {
  ContextEngineConfig,
  ContextEngineEvent,
  ContextPackage,
  PathResult,
  PathStep,
  RetrievedNote,
} from "./types.js";

const DEFAULT_TOP_K = 5;

const RPC_TIMEOUT_MS = 1_800_000; // 30 minutes for long operations (indexing)

// Returned verbatim when RAG produces zero results.
const NOTHING_FOUND_CONTEXT =
  `## Nothing Found\n\n` +
  `No notes in this workspace match this query.\n\n` +
  `**Next step**: Investigate the codebase directly (read files, search for symbols).`;

// ---------------------------------------------------------------------------
// RPC types
// ---------------------------------------------------------------------------

interface RpcPending {
  resolve: (result: unknown) => void;
  reject: (error: Error) => void;
  timer: ReturnType<typeof setTimeout>;
}

// ---------------------------------------------------------------------------
// ContextEngine — subprocess bridge
// ---------------------------------------------------------------------------

export class ContextEngine extends EventEmitter {
  private subprocess: ChildProcess | null = null;
  private rl: ReadlineInterface | null = null;
  private subprocessExitPromise: Promise<void> | null = null;
  private resolveSubprocessExit: (() => void) | null = null;
  private readonly pendingRpc = new Map<string, RpcPending>();
  private initialized = false;
  private pythonPath: string | null = null;

  /** In-memory note cache populated on init/reindex. */
  private noteCache = new Map<string, string>();

  /** True when the Python engine is in degraded mode (no vector embeddings). */
  private degraded = false;
  private degradedReason = "";

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

    // Initialize context reviewer — always-on unless explicitly disabled
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
   * Must be called before build(). Idempotent — safe to call multiple times.
   *
   * Spawns the Python subprocess, sends `initialize` RPC, and populates the note cache.
   */
  async initialize(): Promise<void> {
    if (this.initialized) return;

    const t0 = Date.now();
    this.debug({ type: "ce_init_start", timestamp: t0, path: "subprocess" });

    ensureDir(this.config.dbPath);

    await this.ensureSubprocess();

    const result = await this.rpc("initialize", {
      md_db_path: this.config.mdDbPath,
      db_dir: this.config.dbPath,
      top_k: this.config.topK,
    }) as {
      path: string;
      mode: string;
      degraded_reason: string;
      duration_ms: number;
      note_count: number;
      note_cache: Record<string, string>;
    };

    // Populate note cache
    this.noteCache.clear();
    for (const [key, val] of Object.entries(result.note_cache)) {
      this.noteCache.set(key, val);
    }

    // Track degradation state
    this.degraded = result.mode === "degraded";
    this.degradedReason = result.degraded_reason ?? "";

    this.initialized = true;

    this.debug({
      type: "ce_init_end",
      timestamp: Date.now(),
      path: result.path,
      durationMs: Date.now() - t0,
      noteCount: result.note_count,
    });
  }

  /**
   * Build a ContextPackage for the given prompt.
   * Sends `retrieve` RPC → formats context in TS → runs reviewer in TS.
   */
  async build(prompt: string, workspace?: string): Promise<ContextPackage> {
    this.ensureInitialized();

    const t0 = Date.now();

    this.debug({ type: "ce_retrieval_start", timestamp: t0, query: prompt.slice(0, 200), topK: this.config.topK });

    const tVector = Date.now();
    const rpcResult = await this.rpc("retrieve", {
      query: prompt,
      top_k: this.config.topK,
      ...(workspace ? { workspace } : {}),
    }) as { seed_notes: RpcRetrievedNote[]; expanded_notes: RpcRetrievedNote[] };

    const seedNotes = rpcResult.seed_notes.map(rpcNoteToRetrievedNote);
    const expandedNotes = rpcResult.expanded_notes.map(rpcNoteToRetrievedNote);

    this.debug({ type: "ce_vector_done", timestamp: Date.now(), seedCount: seedNotes.length, durationMs: Date.now() - tVector });

    // Short-circuit: nothing came back from RAG
    if (seedNotes.length === 0 && expandedNotes.length === 0) {
      return {
        query: prompt,
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

    // Format raw context
    const suggestedTools = allNotes
      .filter((n) => n.type === "tool" && n.toolId !== undefined)
      .map((n) => n.toolId!);

    const rawChars = allNotes.reduce((sum, n) => sum + n.content.length, 0);

    const filteredSeeds = allNotes.filter((n) => n.depth === 0 || n.retrievalSource === "vector");
    const filteredExpanded = allNotes.filter((n) => (n.depth ?? 0) > 0 && n.retrievalSource !== "vector");
    const rawFormattedContext = formatContext(filteredSeeds, filteredExpanded);

    // Optional context review / synthesis
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

    // Append degradation warning if running without embeddings
    if (this.degraded) {
      formattedContext += "\n\n> Warning: Embedding provider unavailable — using keyword matching. Results may be less precise.";
    }

    const retrievalMs = Date.now() - t0;

    return {
      query: prompt,
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
    this.ensureInitialized();
    const t0 = Date.now();

    const rpcResult = await this.rpc("find_path", {
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

    // Run through synthesizer if available
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
   * Return the stripped body of a specific note by relative path.
   * Reads from in-memory cache — no RPC needed.
   */
  getNoteContent(relativePath: string): string | null {
    return this.noteCache.get(relativePath) ?? null;
  }

  /**
   * Get graph statistics via Python RPC.
   */
  async getGraphStats(): Promise<{ noteCount: number; edgeCount: number; indexLoaded: boolean }> {
    if (!this.initialized) {
      return { noteCount: 0, edgeCount: 0, indexLoaded: false };
    }
    const result = await this.rpc("get_graph_stats", {}) as {
      note_count: number;
      edge_count: number;
      index_loaded: boolean;
    };
    return {
      noteCount: result.note_count,
      edgeCount: result.edge_count,
      indexLoaded: result.index_loaded,
    };
  }

  /**
   * Complete reindex via Python RPC. Updates local note cache.
   */
  async reindex(): Promise<void> {
    this.ensureInitialized();

    const t0 = Date.now();

    const result = await this.rpc("reindex", {}) as {
      skipped: boolean;
      duration_ms: number;
      note_count: number;
      note_cache: Record<string, string>;
    };

    if (result.skipped) {
      this.debug({ type: "ce_reindex_start", timestamp: t0, path: "skipped" });
      this.debug({ type: "ce_reindex_done", timestamp: Date.now(), durationMs: Date.now() - t0, noteCount: 0, skipped: true });
      return;
    }

    // Update note cache
    this.noteCache.clear();
    for (const [key, val] of Object.entries(result.note_cache)) {
      this.noteCache.set(key, val);
    }

    this.debug({ type: "ce_reindex_start", timestamp: t0, path: "full" });
    this.debug({ type: "ce_reindex_done", timestamp: Date.now(), durationMs: Date.now() - t0, noteCount: result.note_count, skipped: false });
  }

  /**
   * Queue an incremental index update. Returns immediately — the Python
   * background indexer thread does the actual embedding + graph work.
   */
  async incrementalUpdate(changedPaths: string[], deletedPaths: string[] = []): Promise<void> {
    this.ensureInitialized();

    const result = await this.rpc("incremental_update", {
      changed_paths: changedPaths,
      deleted_paths: deletedPaths,
    }) as { queued: boolean; queue_depth: number };

    this.debug({
      type: "ce_subprocess_log",
      timestamp: Date.now(),
      message: `Incremental update queued (queue_depth=${result.queue_depth})`,
    });
  }

  /**
   * Close the subprocess and clean up resources.
   */
  async close(): Promise<void> {
    const waitForExit = this.waitForSubprocessExit();

    if (this.subprocess) {
      try {
        const id = randomUUID();
        const line = JSON.stringify({ id, method: "shutdown", params: {} }) + "\n";
        this.subprocess.stdin?.write(line);
      } catch {
        // Subprocess may already be dead
      }

      try {
        this.subprocess.kill("SIGTERM");
      } catch {
        // Already dead
      }
    }

    await waitForExit;

    // Reject all pending RPCs (if any remain)
    for (const [id, pending] of this.pendingRpc) {
      clearTimeout(pending.timer);
      pending.reject(new Error("ContextEngine closed"));
    }
    this.pendingRpc.clear();

    this.subprocess = null;
    this.rl = null;
    this.initialized = false;
    this.noteCache.clear();
  }

  // =========================================================================
  // Degradation state
  // =========================================================================

  get isDegraded(): boolean {
    return this.degraded;
  }

  get degradedReasonMessage(): string {
    return this.degradedReason;
  }

  // =========================================================================
  // Subprocess management
  // =========================================================================

  private async ensureSubprocess(): Promise<void> {
    if (this.subprocess && !this.subprocess.killed) return;

    const pythonExe = this.resolvePythonPath();
    const cwd = dirname(this.config.mdDbPath);
    this.debug({ type: "ce_subprocess_log", timestamp: Date.now(), message: `Spawning python in cwd=${cwd}` });

    // Pass embed config as env vars so the Python subprocess picks them up
    const cgConfig = getConfig();
    const envOverrides: Record<string, string> = {
      OBSIDI_EMBED_PROVIDER: cgConfig.embedding.provider,
      OBSIDI_EMBED_MODEL: cgConfig.embedding.model,
      OBSIDI_EMBED_HOST: cgConfig.embedding.host,
      OBSIDI_EMBED_CONTEXT_LENGTH: String(cgConfig.embedding.contextLength),
    };
    if (cgConfig.embedding.apiKey) {
      envOverrides["OPENAI_API_KEY"] = cgConfig.embedding.apiKey;
    }

    const proc = spawn(pythonExe, ["-m", "graph"], {
      stdio: ["pipe", "pipe", "pipe"],
      cwd,
      env: { ...process.env, ...envOverrides },
    });

    this.subprocessExitPromise = new Promise<void>((resolve) => {
      this.resolveSubprocessExit = resolve;
    });

    this.subprocess = proc;

    const stderrLines: string[] = [];

    this.rl = createInterface({ input: proc.stdout! });
    this.rl.on("line", (line: string) => {
      this.handleResponse(line);
    });

    const stderrRl = createInterface({ input: proc.stderr! });
    stderrRl.on("line", (line: string) => {
      stderrLines.push(line);
      if (stderrLines.length > 50) stderrLines.shift();
      this.debug({ type: "ce_subprocess_log", timestamp: Date.now(), message: line });
    });

    proc.on("exit", (code, signal) => {
      this.debug({ type: "ce_subprocess_log", timestamp: Date.now(), message: `Python subprocess exited (code=${code}, signal=${signal})` });
      this.subprocess = null;
      this.rl = null;
      this.initialized = false;

      const stderrSummary = stderrLines.length > 0
        ? `\nPython stderr:\n${stderrLines.join("\n")}`
        : "";

      for (const [id, pending] of this.pendingRpc) {
        clearTimeout(pending.timer);
        pending.reject(new Error(`Python subprocess exited (code=${code})${stderrSummary}`));
      }
      this.pendingRpc.clear();

      this.resolveSubprocessExit?.();
      this.resolveSubprocessExit = null;
      this.subprocessExitPromise = null;
    });

    const STARTUP_TIMEOUT_MS = 2000;
    const started = await new Promise<boolean>((resolve) => {
      let elapsed = 0;
      const poll = setInterval(() => {
        elapsed += 50;
        if (!this.subprocess) { clearInterval(poll); resolve(false); return; }
        if (this.subprocess.stdin?.writable) { clearInterval(poll); resolve(true); return; }
        if (elapsed >= STARTUP_TIMEOUT_MS) { clearInterval(poll); resolve(false); }
      }, 50);
    });

    if (!started) {
      const stderrSummary = stderrLines.length > 0
        ? `\nPython stderr:\n${stderrLines.join("\n")}`
        : "\n(no stderr captured — process may have exited before writing anything)";
      throw new Error(
        `Python graph subprocess failed to start (exe: ${pythonExe}).` +
        `\nVerify the conda env exists: conda env create -f graph/environment.yml` +
        stderrSummary,
      );
    }
  }

  private async waitForSubprocessExit(timeoutMs = 2000): Promise<void> {
    const exitPromise = this.subprocessExitPromise;
    if (!exitPromise) return;

    await Promise.race([
      exitPromise.catch(() => {}),
      new Promise<void>((resolve) => setTimeout(resolve, timeoutMs)),
    ]);
  }

  /**
   * Resolve the Python executable path for the contextgarden conda environment.
   * Uses direct path (no conda run — conda run doesn't forward stdin on Windows).
   *
   * Caches the resolved path to `.context-garden/.python_path`.
   */
  private resolvePythonPath(): string {
    if (this.pythonPath) return this.pythonPath;

    // Check cache file first
    const cacheFile = join(this.config.dbPath, "..", ".python_path");
    try {
      const cached = readFileSync(cacheFile, "utf-8").trim();
      if (cached && existsSync(cached)) {
        this.pythonPath = cached;
        return cached;
      }
    } catch {
      // No cache or stale — fall through to search
    }

    const resolved = this.searchPythonPath();

    // Persist for next startup
    try {
      writeFileSync(cacheFile, resolved, "utf-8");
    } catch {
      // Non-fatal
    }

    this.pythonPath = resolved;
    return resolved;
  }

  private searchPythonPath(): string {
    const home = process.env["USERPROFILE"] ?? process.env["HOME"] ?? "";
    const condaDirs = [
      process.env["CONDA_PREFIX"] ? dirname(process.env["CONDA_PREFIX"]) : "",
      join(home, "miniconda3", "envs"),
      join(home, "anaconda3", "envs"),
      join(home, ".conda", "envs"),
    ].filter(Boolean);

    for (const base of condaDirs) {
      // Windows
      const winPath = join(base, "contextgarden", "python.exe");
      if (existsSync(winPath)) return winPath;
      // Unix
      const unixPath = join(base, "contextgarden", "bin", "python");
      if (existsSync(unixPath)) return unixPath;
    }

    // Fall back: ask conda for the path
    try {
      const result = execSync(
        'conda run -n contextgarden python -c "import sys; print(sys.executable)"',
        { encoding: "utf-8", timeout: 15_000 },
      ).trim();
      if (result && existsSync(result)) return result;
    } catch {
      // conda not available or env not found
    }

    throw new Error(
      "Could not find Python for conda env 'contextgarden'. " +
      "Ensure the environment exists: conda env create -f graph/environment.yml"
    );
  }

  // =========================================================================
  // JSON-RPC
  // =========================================================================

  private async rpc(method: string, params: Record<string, unknown>): Promise<unknown> {
    await this.ensureSubprocess();

    if (!this.subprocess?.stdin?.writable) {
      throw new Error(
        "Python subprocess stdin not writable — subprocess may have crashed. " +
        "Check ce_subprocess_log debug events or run: conda run -n contextgarden python -m graph"
      );
    }

    const id = randomUUID();
    const line = JSON.stringify({ id, method, params }) + "\n";

    return new Promise<unknown>((resolve, reject) => {
      const timer = setTimeout(() => {
        this.pendingRpc.delete(id);
        reject(new Error(`RPC timeout: ${method} (${RPC_TIMEOUT_MS}ms)`));
      }, RPC_TIMEOUT_MS);

      this.pendingRpc.set(id, { resolve, reject, timer });

      this.subprocess!.stdin!.write(line, (err) => {
        if (err) {
          clearTimeout(timer);
          this.pendingRpc.delete(id);
          reject(new Error(`Failed to write to subprocess: ${err.message}`));
        }
      });
    });
  }

  private handleResponse(line: string): void {
    let data: { id?: string; result?: unknown; error?: { code: number; message: string } };
    try {
      data = JSON.parse(line);
    } catch {
      this.debug({ type: "ce_subprocess_log", timestamp: Date.now(), message: `Non-JSON from subprocess: ${line.slice(0, 200)}` });
      return;
    }

    if (!data.id) {
      const n = data as Record<string, unknown>;
      if (n["type"] === "index_progress") {
        this.emit("indexProgress", n["done"] as number, n["total"] as number);
      }
      return;
    }

    const pending = this.pendingRpc.get(data.id);
    if (!pending) return;

    clearTimeout(pending.timer);
    this.pendingRpc.delete(data.id);

    if (data.error) {
      pending.reject(new Error(`RPC error (${data.error.code}): ${data.error.message}`));
    } else {
      pending.resolve(data.result);
    }
  }

  // =========================================================================
  // Helpers
  // =========================================================================

  private ensureInitialized(): void {
    if (!this.initialized) {
      throw new Error("ContextEngine not initialized. Call initialize() first.");
    }
  }

  private debug(event: ContextEngineEvent): void {
    this.onDebug?.(event);
  }
}

// ---------------------------------------------------------------------------
// RPC note type (matches Python server output)
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
  };
}

// ---------------------------------------------------------------------------
// Context formatting — tier-aware
// ---------------------------------------------------------------------------

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
// formatPathContext — graph path retrieval
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
