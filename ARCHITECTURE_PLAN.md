# ContextGarden Architecture Plan

Date: 2026-04-06 (revised)

---

## 1. Overview

ContextGarden is a two-process system:

- **Python Daemon** — the heavy worker. Owns file watching, incremental indexing, graph maintenance, and retrieval. Runs persistently (started at OS login; TS gateway spawns it as a fallback). Exposes a JSON-RPC API over localhost TCP.
- **TypeScript Gateway** — the MCP interface. Speaks MCP stdio to the agent client. Owns context synthesis, LLM calls, and markdown tooling. Forwards index/retrieval operations to the daemon over TCP.

The daemon is the source of truth for all knowledge graph state. The gateway is stateless with respect to the index.

---

## 2. Process architecture

```
Agent (Claude Desktop / Codex)
        │ MCP stdio
        ▼
  TypeScript Gateway
  - MCP server (stdio)
  - Context synthesizer
  - LLM client
  - Markdown tooling
        │ JSON-RPC over TCP (localhost)
        ▼
  Python Daemon
  - watchdog file watcher (per workspace)
  - Incremental indexer
  - LlamaIndex vector + graph store
  - Retrieval pipeline
  - Workspace registry (persisted to disk)
```

---

## 3. Python Daemon

### 3.1 Startup

On boot:
1. Load `~/.contextgarden/registry.json` — list of registered workspaces.
2. For each workspace, load its persisted LlamaIndex index from `~/.contextgarden/<workspace_id>/`.
3. Start a watchdog watcher for each workspace root immediately. No re-embedding unless files changed while the daemon was offline (detected by mtime/hash on first scan).
4. Begin listening on `127.0.0.1:<port>` (default `7432`, configurable via `CG_DAEMON_PORT`).

The daemon does **not** wait for a client connection before watching. It stays active as long as the OS session is running.

### 3.2 Persistence layout

```
~/.contextgarden/                   # configurable via CG_DATA_DIR
  registry.json                     # list of registered workspaces
  <workspace_id>/
    meta.json                       # workspace name, root_path, config
    index/                          # LlamaIndex VectorStoreIndex JSON
    graph/                          # SimplePropertyGraphStore JSON
    file_hashes.json                # path -> content_hash, for drift detection on restart
```

On restart, `file_hashes.json` is used to detect files that changed while the daemon was offline. Only changed/deleted files are re-embedded — no full rebuild.

### 3.3 Workspace registry

Workspaces are persisted to `registry.json`. Once registered, they survive daemon restarts without the gateway re-registering. The gateway can still call `workspaces.register` to add new ones at any time.

### 3.4 File watcher

The Python daemon uses **watchdog** for cross-platform file watching. Each registered workspace gets its own `Observer`. Events are:
- Debounced with a 500ms window before queuing.
- Coalesced per workspace — a burst of changes becomes one job.
- Queued in the bounded job queue (cap: `CG_MAX_PENDING_JOBS`, default 50). Overflow is coalesced into a single reconcile job (already implemented in Phase 1).

The TypeScript layer has no file watcher.

### 3.5 Embedding providers

The daemon uses a pluggable `EmbedProvider` interface:

```python
class EmbedProvider(Protocol):
    def get_text_embedding(self, text: str) -> list[float]: ...
    def get_text_embeddings(self, texts: list[str]) -> list[list[float]]: ...
    @property
    def model_id(self) -> str: ...
```

Built-in implementations:
- `OllamaEmbedProvider` — default, uses `OLLAMA_HOST` / `CG_EMBED_MODEL`
- `OpenAIEmbedProvider` — activated when `OPENAI_API_KEY` is set and `CG_EMBED_PROVIDER=openai`

Provider and model are resolved at daemon startup from env vars / config. Changing provider requires a full reindex (daemon warns and refuses to mix embedding spaces).

### 3.6 RPC API (v1)

All requests/responses are newline-delimited JSON over a persistent TCP connection. Each message includes `id`, `method`, `params`. Responses include `id` + either `result` or `error`.

**Workspaces**
```
workspaces.register({ name, root_path, include_globs?, exclude_globs? }) -> { workspace_id }
workspaces.list() -> { workspaces: [{ workspace_id, name, root_path, state, last_indexed_at }] }
workspaces.unregister({ workspace_id, delete_data? }) -> { ok }
workspaces.status({ workspace_id }) -> { state, queue_depth, doc_count, last_indexed_at, watcher_active }
```

**Index**
```
index.enqueue({ workspace_id, changed_paths, deleted_paths }) -> { job_id, queue_depth }
index.rebuild({ workspace_id }) -> { job_id }
index.pause({ workspace_id? }) -> { ok }
index.resume({ workspace_id? }) -> { ok }
```

**Query**
```
query.retrieve({ workspace_id?, query, top_k?, max_chars? }) -> { notes, context_markdown, meta }
query.find_path({ workspace_id?, start, end, max_depth?, edge_types? }) -> { path }
query.stats({ workspace_id? }) -> { doc_count, node_count, edge_count }
```

**Daemon**
```
daemon.health() -> { status, uptime_secs, rss_mb, workspaces }
daemon.shutdown() -> { ok }
```

### 3.7 Retrieval quality (primary pain point)

The current retrieval pipeline returns semantically similar chunks but misses important context. The improved pipeline:

1. **Candidate retrieval**: vector similarity top-K (existing).
2. **Keyword boost**: BM25 over note bodies, merged with vector scores. Prevents pure-semantic misses on exact terms (file names, function names, jargon).
3. **Graph expansion**: from each retrieved note, walk 1–2 hops in the property graph (wikilinks, `related_to`, `depends_on` edges). Add neighbor notes if they're not already in the result set.
4. **Recency boost**: small score bump for recently modified files (configurable weight, default 0.1).
5. **Deduplication**: if two chunks from the same file are retrieved, merge them into one note entry.
6. **Formatting**: produce `context_markdown` with source citations (relative path + line range).

The `query.retrieve` response always includes a `meta` field with retrieval scores and provenance so the TS synthesizer can make informed decisions about what to include.

---

## 4. TypeScript Gateway

### 4.1 Responsibilities

- MCP stdio server (unchanged interface for agent clients).
- Context synthesizer: takes raw retrieval results, calls the LLM to synthesize a focused answer.
- LLM client: OpenAI / Anthropic / Ollama calls for synthesis.
- Markdown tooling: frontmatter parsing, wikilink resolution, vault schema validation.
- Daemon client: connects to `127.0.0.1:7432`, forwards tool calls, handles reconnect.

### 4.2 Daemon client behavior

On startup:
1. Probe `CG_DAEMON_PORT` (default `7432`) with `daemon.health()`.
2. If unreachable, spawn the daemon process (`python -m graph.daemon`) and wait up to 10s for it to be ready.
3. Maintain one persistent TCP connection. On disconnect, retry with exponential backoff (max 30s).

The gateway never loads the knowledge index into memory.

### 4.3 MCP tool mapping

| MCP tool | Daemon call |
|---|---|
| `register_workspace` | `workspaces.register` |
| `list_workspaces` | `workspaces.list` |
| `unregister_workspace` | `workspaces.unregister` |
| `retrieve_context` | `query.retrieve` → synthesizer → response |
| `find_path` | `query.find_path` |
| `get_graph_stats` | `query.stats` |
| `reindex` | `index.rebuild` |

`retrieve_context` is the only tool that goes through the TS synthesizer before returning. All others are pass-through.

---

## 5. Daemon lifecycle

**Normal path (daemon already running)**:
- OS login item / startup script launches `python -m graph.daemon`.
- Daemon loads persisted indexes, starts watchers, listens on TCP.
- Agent starts → gateway spawns, probes port, connects.

**Fallback path (daemon not running)**:
- Gateway probes port on startup → no response.
- Gateway spawns daemon as a child process.
- Child inherits stderr for logging; stdout is not used (RPC is on the TCP socket).
- Gateway waits for `daemon.health()` to succeed before accepting MCP calls.

**Shutdown**:
- Gateway sends `daemon.shutdown()` only if it spawned the daemon itself (not if it was pre-running).
- Daemon drains the job queue, flushes overflow, persists state, then exits.

---

## 6. Configuration

All configuration via environment variables (with sensible defaults). No config file API for now.

| Variable | Default | Purpose |
|---|---|---|
| `CG_DATA_DIR` | `~/.contextgarden` | Root for persisted indexes and registry |
| `CG_DAEMON_PORT` | `7432` | TCP port for daemon RPC |
| `CG_EMBED_PROVIDER` | `ollama` | `ollama` or `openai` |
| `CG_EMBED_MODEL` | `nomic-embed-text` | Model name passed to provider |
| `OLLAMA_HOST` | `http://localhost:11434` | Ollama base URL |
| `CG_MAX_PENDING_JOBS` | `50` | Bounded job queue cap |
| `CG_SKIP_INDEX_ON_BOOT` | unset | If `1`, skip initial drift scan (start degraded) |
| `CG_LOG_LEVEL` | `INFO` | Python logging level |

---

## 7. Implementation phases

### Phase 1 — Stabilize (complete)
- Bounded queue + overflow coalescing.
- Periodic metrics logging (queue depth, RSS).
- `CG_SKIP_INDEX_ON_BOOT` flag.

### Phase 2 — Daemon extraction (complete)
1. ✅ Added `watchdog>=4.0.0` + `rank-bm25>=0.2.2` to `graph/requirements.txt` and `graph/environment.yml`.
2. ✅ Implemented `graph/daemon.py`: asyncio TCP server on port 7432, RPC dispatch, workspace registry persistence (`~/.contextgarden/registry.json`), `file_hashes.json` drift detection on boot, per-workspace watchdog observers (500ms debounce).
3. ✅ Added `EmbedProvider` Protocol to `graph/providers.py`; CG_EMBED_* env vars (with OBSIDI_EMBED_* fallback).
4. ✅ Added `src/engine/daemon-client.ts`: TCP client, spawn-if-not-running fallback, exponential backoff reconnect.
5. ✅ Updated `src/engine/context-engine.ts`: replaced stdio subprocess with DaemonClient; maps old RPC methods to new daemon API (query.retrieve, query.find_path, index.rebuild, index.enqueue, etc.).
6. ⬜ Remove chokidar watcher from TS (kept for now — TS still mirrors source code via chokidar; daemon watches the md_db notes).

### Phase 3 — Tray app (future)
- System tray UI for workspace management, status, pause/resume, logs.
- Process supervision with restart policies.

---

## 8. Open questions (deferred)

- **Cross-workspace retrieval**: federation across multiple workspace indexes. Not needed yet.
- **IPC upgrade**: migrate from JSON-RPC/TCP to gRPC if schema complexity grows.
- **Reranker**: add a reranker model pass after candidate retrieval for further quality improvement.
- **Tray tech**: Tauri vs Electron (Phase 3 decision).
