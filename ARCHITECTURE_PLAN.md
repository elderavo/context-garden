# ContextGarden 3‑Tier Architecture Plan (Tray + Gateway + Indexer)

Date: 2026-04-06

This document proposes a scalable, extensible refactor that keeps ContextGarden responsive and memory-stable on developer laptops by splitting the system into three cooperating processes:

1. **Tray App (Controller/UX)**: starts/stops services, manages workspaces/config, shows status/health.
2. **Gateway Server (Lightweight MCP interface)**: speaks MCP over stdio to the agent client; forwards to indexer; enforces limits.
3. **Indexer Daemon (Heavy worker)**: watches files, performs incremental indexing, serves retrieval/path queries from persisted indexes.

The core goals are:
- **Fast startup** (O(1) open DBs + load small metadata, no scans at boot)
- **Bounded memory** (bounded queues + bounded caches + streaming/chunked processing)
- **Incremental indexing** (hash-based deltas; no full rebuilds unless necessary)
- **Extensibility** (pluggable parsers, chunkers, retrievers, stores)
- **Operational control** (pause/resume, concurrency knobs, observability)

---

## 0) Current pain & likely causes (hypotheses)

Symptoms: “laptop groaning” on startup and/or during indexing; suspected memory issue.

Common root causes in RAG/indexer systems:
- **Startup does too much work**: scanning/parsing/embedding at server boot.
- **Unbounded queues**: watcher events accumulate during big `git checkout` or build steps.
- **Unbounded caches**: note bodies, parsed ASTs, embeddings, graph objects grow without eviction.
- **Large in-memory indexes**: loading vector/graph state fully into RAM on init.
- **Synchronous hot paths**: retrieval blocks on indexing; no separation of concerns.

This plan is designed to address all of the above with explicit constraints and architectural boundaries.

---

## 1) Target architecture overview

### 1.1 Processes and responsibilities

**A) Tray App (Controller/UX)**
- Starts/stops/restarts Gateway + Indexer.
- Workspace management UI: add/remove roots, include/exclude globs, language selection.
- Configuration UI: embed/LLM providers & models, concurrency limits, thresholds.
- Health UI: indexing status, queue depth, last indexed time, memory/CPU, error surface.
- “Safety controls”: pause indexing, rebuild index, clear caches, open logs.

**B) Gateway Server (MCP interface)**
- Speaks MCP over stdio (what Claude Desktop/Codex clients expect).
- Minimal state; no filesystem watchers.
- Forwards API calls to Indexer Daemon via local IPC.
- Enforces safety limits:
  - max request/response size
  - timeouts
  - rate limits per tool
  - workspace allow-listing
- Streams progress events (optional) as MCP notifications if the client supports them.

**C) Indexer Daemon (Heavy worker + query service)**
- Owns all “heavy” compute and memory:
  - file watching & debounced batching
  - parsing + chunking
  - embeddings + vector index updates
  - graph/link extraction + updates
  - retrieval/path queries against on-disk indexes
- Implements bounded resource policies:
  - bounded job queue + coalescing
  - bounded caches (LRU with byte budgets)
  - limited concurrency (embedding/parsing)
  - memory pressure valve (pause watchers, shrink caches)

### 1.2 Data plane vs control plane

- **Control plane**: workspaces, config, job orchestration, status/health, pause/resume.
- **Data plane**: incremental indexing inputs and retrieval outputs.

This separation ensures the Gateway remains cheap and responsive and the Indexer can be restarted independently.

---

## 2) IPC choice and protocol design

### 2.1 Recommended IPC options (pick one)

**Option 1 (recommended for speed-to-ship): JSON-RPC over localhost TCP**
- Pros: quick to implement, matches current newline-delimited JSON patterns, easy debugging.
- Cons: less strict typing unless paired with JSON schema / zod.

**Option 2: gRPC (Protobuf)**
- Pros: typed contracts, streaming built in, easier long-term compatibility.
- Cons: more setup and build plumbing.

**Option 3: Named pipes (Windows) / Unix domain sockets**
- Pros: efficient local IPC, better OS integration.
- Cons: cross-platform complexity.

For an initial refactor, **JSON-RPC over TCP** is the pragmatic choice; migrate to gRPC later if desired.

### 2.2 Contract versioning

Every request/response includes:
- `request_id` (UUID)
- `schema_version` (e.g., `"v1"`)
- `workspace_id` (UUID, not path)

Rules:
- **Backward compatibility** at least within a major version.
- Reject unknown major versions with actionable errors.

### 2.3 Core API surface (v1)

**Workspaces**
- `workspaces.register({ name, root_path, languages, include_globs?, exclude_globs? }) -> { workspace_id }`
- `workspaces.list() -> { workspaces: [...] }`
- `workspaces.unregister({ workspace_id, delete_data? }) -> { ok }`
- `workspaces.status({ workspace_id }) -> { state, last_indexed_at, queue_depth, docs, chunks, ... }`

**Index jobs**
- `index.enqueue({ workspace_id, changed_paths, deleted_paths, reason }) -> { job_id }`
- `index.rebuild({ workspace_id, reason }) -> { job_id }`
- `index.pause({ workspace_id? }) -> { ok }`
- `index.resume({ workspace_id? }) -> { ok }`
- `index.job_status({ job_id }) -> { state, progress, errors? }`
- `index.cancel({ job_id }) -> { ok }`

**Query**
- `query.retrieve({ workspace_id?, query, top_k?, max_chars? }) -> { context_markdown, notes: [...], meta: {...} }`
- `query.find_path({ workspace_id?, start, end, max_depth?, edge_types? }) -> { path: [...] }`
- `query.stats({ workspace_id? }) -> { docs, chunks, nodes, edges, index_version, ... }`

**Config**
- `config.get() -> { embedding: {...}, synthesizer: {...}, limits: {...} }`
- `config.set({ ... }, persist? ) -> { ok }`

**Events (stream)**
- `events.subscribe({ workspace_id? }) -> stream of { type, timestamp, payload }`
  - `index.progress`, `index.warning`, `index.error`, `memory.pressure`, `queue.depth`, `watcher.debounced`

---

## 3) Storage design (persisted, restart-fast)

The Indexer should persist its state so restarts do not trigger reindex.

### 3.1 Stores (embedded, local-first)

**A) Document store (SQLite)**
- Tables:
  - `files(workspace_id, path, mtime, size, content_hash, parse_version, ...)`
  - `chunks(chunk_id, workspace_id, file_path, chunk_hash, start, end, text, metadata_json, ...)`

**B) Vector index**
Pick one:
- SQLite + `sqlite-vss` (simple; good enough for many repos)
- LanceDB (good local experience; columnar; efficient)
- FAISS + sidecar mapping table (fast, but more glue)

Store embeddings keyed by `(provider, model, chunk_hash)` to enable reuse:
- `embeddings(embed_id, provider, model, chunk_hash, vector_blob, dims, created_at)`
- `chunk_embeddings(chunk_id, embed_id)`

**C) Graph/link index (SQLite)**
- `nodes(node_id, workspace_id, label, type, attrs_json)`
- `edges(edge_id, workspace_id, src_node_id, dst_node_id, edge_type, weight, attrs_json)`

### 3.2 Schema migrations

Maintain `schema_version` and forward-only migrations.
- Minor changes: migrate in place.
- Incompatible changes: rebuild indexes in background; keep serving stale index until ready.

---

## 4) Indexing pipeline (incremental + bounded)

### 4.1 Incremental update strategy

Per file:
- Compute `content_hash` (fast hash; optionally chunked to reduce memory).
- If unchanged, skip.
- If changed:
  - parse -> chunk -> compute `chunk_hash` for each chunk
  - reuse embeddings for existing `chunk_hash` if present
  - update stores (doc, vector, graph)

Deletion:
- Remove file row, chunks, chunk_embeddings, and any graph references; maintain integrity.

### 4.2 Watcher batching + coalescing

Key requirement: avoid event storms exploding memory.

- Debounce window: `250–1000ms`
- Coalesce paths per workspace; dedupe
- Bounded queue:
  - if queue exceeds `N`, merge jobs into a single “reconcile” job:
    - rescan file manifest and compute deltas once

### 4.3 Concurrency and backpressure

Embedding is the most expensive step. Defaults:
- `embed_concurrency = 1` (safe), configurable up to 4.
- `parse_concurrency = min(2, cpu_cores)`
- Maximum bytes per batch: e.g. `CG_INDEX_MAX_BATCH_MB=8`

Backpressure rules:
- If embed queue grows: stop accepting new embed tasks; keep accepting “file changed” events only as coalesced manifest updates.

### 4.4 Memory budgets (hard limits)

Implement explicit budgets to prevent slow leaks from becoming outages:
- LRU caches with max bytes:
  - `note_body_cache_bytes`
  - `parsed_cache_bytes` (if any)
  - `recent_results_cache_bytes` (optional)
- Queue size limits:
  - `max_pending_jobs`
  - `max_pending_embed_tasks`

Memory pressure valve:
- If RSS (or heap) crosses a threshold:
  1) pause watchers
  2) clear caches
  3) reduce concurrency
  4) resume when below hysteresis threshold

---

## 5) Retrieval design (hybrid, modular)

### 5.1 Retrieval pipeline (pluggable)

Stages:
1. Candidate retrieval:
   - vector similarity (top K)
   - optional keyword/BM25
2. Boosting:
   - recency
   - workspace tags
   - file path heuristics (e.g., `src/`, `docs/`)
3. Graph expansion:
   - expand neighbors up to depth D with weights
4. Formatting:
   - produce context markdown + citations to chunk/file paths

Define interfaces (TS/Python depending on where retrieval runs):
- `Retriever`, `Reranker`, `GraphExpander`, `Formatter`

Make it easy to add:
- a reranker model later
- a “tool note” source later
- multi-workspace federation later

---

## 6) Tray App plan (controller UX)

### 6.1 Feature set (v1)
- Show: running/stopped, index lag, last indexed, queue depth, memory/CPU.
- Workspaces: add/remove; per-workspace include/exclude globs.
- Buttons: pause/resume indexing, rebuild index, open logs folder.
- Provider config: embed/LLM model/provider, keys stored in OS keychain if possible.

### 6.2 Process supervision

Tray app supervises two child processes:
- `context-garden-gateway` (Node)
- `context-garden-indexer` (Python or Node+Python)

Policies:
- restart on crash with exponential backoff
- retain last N logs
- show actionable error messages

---

## 7) Gateway Server plan (MCP interface)

### 7.1 Responsibilities
- MCP stdio server only.
- Connects to indexer via IPC and forwards tool calls.
- Adds safety:
  - `max_chars` enforcement
  - tool timeouts
  - workspace allow-list
  - request validation

### 7.2 Tools mapping

Existing MCP tools remain, but implementation changes:
- `register_workspace` -> indexer `workspaces.register`
- `list_workspaces` -> indexer `workspaces.list`
- `unregister_workspace` -> indexer `workspaces.unregister`
- `retrieve_context` -> indexer `query.retrieve`
- `find_path` -> indexer `query.find_path`
- `configure` -> indexer `config.set/get`

The Gateway should never load the knowledge base into memory.

---

## 8) Migration strategy (phased refactor)

### Phase 1: Stabilize current implementation (1–3 days)
- Add memory/queue instrumentation and log periodic snapshots.
- Add watcher debounce + job coalescing if missing.
- Add hard caps on caches/queues.
- Ensure startup does not auto-scan/index unless explicitly requested.

Deliverable: current server behaves better, produces metrics for profiling.

### Phase 2: Extract Indexer Daemon + IPC (3–7 days)
- Implement indexer daemon process with:
  - persistent stores
  - RPC over localhost TCP
  - background indexing jobs
  - retrieval endpoints
- Update existing ContextGarden server to become Gateway and forward calls.

Deliverable: Gateway stays fast; indexer can be restarted independently.

### Phase 3: Tray App (5–10 days)
- Build tray app and process supervision.
- UI for workspaces/config/status; log viewer.

Deliverable: end-user experience; operational control and visibility.

### Phase 4: Hardening & extensibility (ongoing)
- Add reranker support, richer graph features, multi-workspace federation.
- Add soak tests and regression tests for incremental correctness.

---

## 9) Observability & test plan

### 9.1 Metrics
- `index.jobs.queue_depth`
- `index.embed.queue_depth`
- `index.throughput.docs_per_min`
- `query.latency.p50/p95`
- `process.rss_mb`, `process.heap_mb`
- `gc.pause_ms` (Node)

### 9.2 Soak tests
- “Big repo” baseline: register workspace, wait until indexed, run query loop.
- “Event storm”: `git checkout` across branches; ensure queue stays bounded.
- “Leak check”: 1–2 hour idle with edits; RSS should plateau.

---

## 10) Open decisions (confirm before implementation)

1. IPC choice: JSON-RPC over TCP vs gRPC.
2. Vector store: sqlite-vss vs LanceDB vs FAISS.
3. Where retrieval runs: Indexer only (recommended) vs split.
4. Tray tech: Tauri vs Electron.

---

## 11) Immediate next steps checklist

- [ ] Add periodic memory snapshots to current ContextGarden.
- [ ] Add bounded queues + debounce/coalescing.
- [ ] Make indexing opt-in (no work at boot).
- [ ] Pick IPC + store tech and lock v1 schemas.
- [ ] Implement indexer daemon skeleton + health endpoint.
- [ ] Convert current MCP server into a pure gateway.

