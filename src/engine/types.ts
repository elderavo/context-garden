/**
 * Context engine public types.
 */

// ---------------------------------------------------------------------------
// Note types
// ---------------------------------------------------------------------------

export type NoteType = "tool" | "concept" | "index" | "codebase" | "codeUnit" | "codeSymbol" | "codeModule";

// ---------------------------------------------------------------------------
// RetrievedNote — a note that has entered the result set
// ---------------------------------------------------------------------------

export interface RetrievedNote {
  /** Graph identity key (== relative path). */
  noteId: string;

  /** Relative path within md_db (e.g. "tools/network.md"). */
  path: string;

  /** Raw markdown body text. */
  content: string;

  /** Retrieval score (0–1). Seed notes: vector similarity. Graph notes: derived score. */
  score: number;

  type: NoteType;

  /** Stem of path for tool notes (e.g. "network"). */
  toolId?: string;

  /** Normalized tags from frontmatter (if available). */
  tags?: string[];

  /** How this note entered the result set. */
  retrievalSource: "vector" | "graph" | "hybrid" | "keyword" | "path";

  /**
   * NoteIds of notes that linked to this one.
   * Only set for graph-retrieved notes.
   */
  linkedFrom?: string[];

  /**
   * Graph traversal depth.
   * 0 = direct vector match (seed).
   * 1 = one hop from a seed, etc.
   */
  depth?: number;

  /** Note tier: '1' (symbol), '2' (file), '3' (module). Undefined for non-code notes. */
  tier?: string;

  /** Symbol kind for tier-1 notes: 'function', 'class', 'interface', 'type', 'const'. */
  symbolKind?: string;

  /** Human-readable title from frontmatter or H1, set by the Python engine at retrieval time. */
  title?: string;

  /** Edge label that brought this note into the result set (graph-expanded notes only). */
  viaEdge?: string;

  /** Human title of the note that linked to this one via viaEdge. */
  viaSourceTitle?: string;
}

// ---------------------------------------------------------------------------
// ContextPackage — what context_engine returns to the caller
// ---------------------------------------------------------------------------

export interface ContextPackage {
  /** Original prompt used for retrieval. */
  query: string;

  /** All retrieved notes, sorted by score descending. */
  retrievedNotes: RetrievedNote[];

  /** Tool IDs from retrieved tool nodes. */
  suggestedTools: string[];

  /** Formatted markdown ready for injection into the system prompt. */
  formattedContext: string;

  /** Wall-clock time for the full retrieval in ms. */
  retrievalMs: number;

  /** Unix timestamp (ms) when the package was built. */
  builtAt: number;

  /** NoteIds of vector seed notes (depth 0). */
  seedNoteIds?: string[];

  /** NoteIds of graph-expanded notes (depth >= 1). */
  expandedNoteIds?: string[];

  /** Total raw character count across all retrieved note bodies (before stripping). */
  rawChars: number;

  /** Character count of formattedContext (after frontmatter stripping). */
  strippedChars: number;

  /** Rough token estimate of formattedContext (chars ÷ 4). */
  estimatedTokens: number;

  /** Review/synthesis result, if context review was performed. */
  reviewResult?: {
    reviewMs: number;
    skipped: boolean;
    skipReason?: string;
  };
}

// ---------------------------------------------------------------------------
// PathResult — graph path retrieval result
// ---------------------------------------------------------------------------

export interface PathStep {
  nodeId: string;
  edgeLabel: string;
  edgeDirection: "outgoing" | "incoming" | "";
  fromNodeId: string;
}

export interface PathResult {
  startId: string;
  endId: string;
  startResolvedBy: "exact" | "vector" | "keyword" | "none";
  endResolvedBy: "exact" | "vector" | "keyword" | "none";
  pathLength: number;
  pathSteps: PathStep[];
  pathNotes: RetrievedNote[];
  formattedContext: string;
  retrievalMs: number;
  noPath: boolean;
  reviewResult?: {
    reviewMs: number;
    skipped: boolean;
    skipReason?: string;
  };
}

// ---------------------------------------------------------------------------
// ContextEngineConfig
// ---------------------------------------------------------------------------

export interface ContextEngineConfig {
  /**
   * Absolute path to the md_db directory.
   */
  mdDbPath: string;

  /**
   * Absolute path to the data directory.
   * Defaults to: path.join(path.dirname(mdDbPath), '.context-garden')
   * The .context-garden/ directory is created automatically on initialize().
   */
  dbPath?: string;

  /**
   * Number of vector seed notes to retrieve per query.
   * Default: 5
   */
  topK?: number;

  /**
   * Path to the personalities directory.
   * Default: src/data/ (relative to project root)
   */
  personalitiesDir?: string;

  /**
   * Context review configuration. Always-on by default.
   * Set `enabled: false` to explicitly disable.
   * Falls back to raw context on LLM/network errors.
   */
  review?: {
    enabled?: boolean;
    personality?: string;
    maxLatencyMs?: number;
  };

  /**
   * Debug event callback. Called at each internal state transition
   * (init, retrieval steps, review, reindex).
   */
  onDebug?: (event: ContextEngineEvent) => void;
}

// ---------------------------------------------------------------------------
// Debug events — emitted via onDebug callback
// ---------------------------------------------------------------------------

export interface ContextEngineEvent {
  type: string;
  timestamp: number;
  sessionId?: string;
  runId?: string;
  [key: string]: unknown;
}
