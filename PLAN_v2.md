# ContextGarden — Implementation Plan v2

## Target Architecture

One Python process. One HTTP port (7433). No TCP daemon. No TypeScript application layer.

```
context_engine/          ← Python package
  server.py              ← aiohttp: webapp + REST API + MCP over StreamableHTTP
  mcp_tools.py           ← 8 MCP tool definitions (Python mcp SDK)
  core/                  ← engine, indexer, retriever, pruner, synthesizer
  domain/ app/ infra/    ← hexagonal layers (unchanged)
  static/index.html      ← webapp

mirror/                  ← standalone TS package (subprocess tool only)
  src/mirror-cli.ts      ← invoked by Python for code → markdown
  src/parsers/           ← ts, python, c parsers
```

Claude Code connects to `http://<host>:7433/mcp` via StreamableHTTP MCP transport.
GitLab webhooks hit `http://<host>:7433/webhooks/gitlab`.
Browser hits `http://<host>:7433/`.

---

## Phase 1 — Restructure Python Package

**Goal:** Rename `graph/` → `context_engine/`, consolidate flat top-level files into
`context_engine/core/`. No behavior changes. All existing tests pass.

**Dependencies:** None — safe starting point.

### Steps

1. Rename directory `graph/` → `context_engine/`

2. Create `context_engine/core/` and move these flat files into it:
   - `engine.py`, `indexer.py`, `retriever.py`, `keyword_retriever.py`
   - `pathfinder.py`, `pruner.py`, `models.py`
   - `markdown_utils.py`, `protocol.py`, `providers.py`, `jobs.py`

3. Update all internal imports across the package (e.g.
   `from .engine import ...` → `from .core.engine import ...`)

4. Update `__main__.py` entrypoint to reflect new module name:
   `python -m context_engine`

5. Update `daemon.py` import of `jobs` and `http_server` references.

6. Update `graph/requirements.txt` path references in any scripts.

7. Run existing tests — all should pass without logic changes.

### Deliverables
- `context_engine/` package with clean submodule layout
- All imports updated
- `python -m context_engine` starts the server

---

## Phase 2 — Python MCP Endpoint

**Goal:** Expose all 8 MCP tools over StreamableHTTP at `/mcp` on the existing
aiohttp server. Claude Code connects via `http://<host>:7433/mcp`.

**Dependencies:** Phase 1 complete (clean package structure).

### New dependency
Add to `requirements.txt`:
```
mcp>=1.0.0
```

### Steps

1. **`context_engine/mcp_tools.py`** — implement all 8 tools using the Python `mcp` SDK:

   | Tool | Implementation |
   |------|---------------|
   | `retrieve_context` | calls `core.engine.KnowledgeGraphEngine.retrieve()` directly |
   | `find_path` | calls `core.pathfinder` directly |
   | `rate_context` | appends to `_rate_ring` in `server.py` |
   | `list_workspaces` | reads from `WorkspaceJsonRepository` |
   | `register_workspace` | calls `app.services.workspace_service.register_workspace()` |
   | `unregister_workspace` | calls `app.services.workspace_service.unregister_workspace()` |
   | `configure` | reads/writes `config.json` via `config.py` (add `write_config()`) |
   | `setup` | composes configure + register + connectivity probe |

   Note: `configure` and `setup` currently live in TS and read TS-side `config.ts`.
   Python `config.py` already reads `config.json` — add a `write_config(patch)` function
   to complete the port. The LLM reachability probe in `setup` becomes a simple
   `urllib.request` call to the configured LLM host (same pattern as `OllamaBackend.check_reachable`).

2. **`context_engine/server.py`** (`make_http_app`) — mount the MCP app:
   ```python
   from mcp.server.aiohttp import SseServerTransport   # or StreamableHTTP variant
   # wire /mcp route → MCP StreamableHTTP handler
   ```
   The MCP server instance is created once and shared with the HTTP app.

3. **Wire daemon reference** — `mcp_tools.py` needs access to the `_DaemonServer`
   instance (for retrieve/find_path). Pass it at startup via a factory function,
   same pattern as `make_http_app(daemon, data_dir)`.

4. **Test** — configure Claude Code with:
   ```json
   { "mcpServers": { "context-garden": { "url": "http://localhost:7433/mcp" } } }
   ```
   Verify all 8 tools appear and `retrieve_context` returns results.

### Deliverables
- `context_engine/mcp_tools.py`
- `config.py` gains `write_config(patch: dict)`
- `/mcp` route live on port 7433
- All 8 tools verified via Claude Code

---

## Phase 3 — Remove TCP Daemon

**Goal:** Strip the TCP JSON-RPC server and all process-management logic from
`daemon.py`. Rename it `server.py`. The HTTP server is now the only entrypoint.

**Dependencies:** Phase 2 complete (MCP must be working before the old TS path is removed).

### What gets removed from `daemon.py`
- `asyncio.start_server` TCP server (`_handle_client`, `_run_server` TCP parts)
- Port probing: `_port_is_open`, `_rpc_request`, `_is_contextgarden_daemon`
- Process management: `_restart_existing_daemon_if_needed`, `_request_existing_shutdown`,
  `_wait_port_free`, `_find_listener_pid`, `_force_kill_pid`, `DaemonAlreadyRunning`
- `DAEMON_HOST`, `DAEMON_PORT` constants
- `_handle_client` coroutine
- `DaemonLogger` SQLite logger (replace with standard `logging` to a file if needed,
  or keep as a separate utility — not core to HTTP serving)
- `_BLOCKING_METHODS` set and `dispatch()` method on `_DaemonServer`
  (handlers are called directly by MCP tools and HTTP handlers, no dispatch table needed)

### What stays
- `_DaemonServer` class and all `handle_*` methods (still used by HTTP + MCP handlers)
- `WorkspaceRuntime`, `WorkspaceRecord`, watchdog watcher, index thread logic
- `_read_workspaces_json`, `boot_workspaces`

### Steps

1. Remove the TCP server block from `daemon.py`.

2. Rename `daemon.py` → `server.py`. Update `__main__.py`:
   ```python
   from .server import main
   main()
   ```

3. Simplify `main()` — it now just:
   - Configures paths
   - Loads registry
   - Inits job queue
   - Starts aiohttp HTTP server

4. Remove `protocol.py` from `context_engine/core/` (TCP RPC protocol — no longer needed).

5. Update `__main__.py` and any remaining import references.

6. Verify the webapp, REST API, MCP, and webhooks all still work.

### Deliverables
- `context_engine/server.py` (was `daemon.py`, TCP stripped)
- `context_engine/core/protocol.py` deleted
- `python -m context_engine` starts cleanly, single HTTP port

---

## Phase 4 — Slim TypeScript to Mirror-Only

**Goal:** Delete all TS application code. Keep only the code→markdown mirror pipeline
as a standalone subprocess tool.

**Dependencies:** Phase 3 complete (TCP gone, no TS client needed).

### What gets deleted
```
src/mcp/
src/engine/context-engine.ts
src/engine/daemon-client.ts
src/engine/review/
src/llm/
src/workspace/
src/config.ts
src/stack.ts
src/data/
bin/context-garden.ts
```

### What gets kept and moved
```
src/mirror/  →  mirror/src/
  parsers/python.ts
  parsers/typescript.ts
  parsers/c.ts
  language-registry.ts
  mirror-cli.ts          ← subprocess entrypoint Python calls
  run-mirror.ts
  cleanup.ts
src/markdown/ →  mirror/src/markdown/   (used by mirror-cli)
src/util/fs.ts →  mirror/src/util/fs.ts
bin/cg-sync.ts  →  mirror/bin/cg-sync.ts  (if still needed)
```

### Steps

1. Create `mirror/` as a standalone package:
   ```
   mirror/
     src/
     bin/
     package.json      ← scoped to mirror only; remove mcp-sdk, express, axios
     tsconfig.json
   ```

2. Update `mirror/package.json` — keep only:
   `typescript`, `tsx`, `@types/node`, tree-sitter parser deps.
   Remove: `@modelcontextprotocol/sdk`, `express`, `axios`, `chokidar`, `zod`.

3. Update Python `context_engine/infra/mirror/mirror_service_legacy.py` —
   change mirror-cli path from `dist/src/mirror/mirror-cli.js`
   to `mirror/dist/src/mirror-cli.js` (or whatever the new build output is).

4. Update `context_engine/server.py` `main()` — mirror-cli path discovery:
   ```python
   _mirror_cli = str(DATA_DIR / "mirror" / "dist" / "src" / "mirror-cli.js")
   ```

5. Delete `src/` and root `package.json` / `tsconfig.json`.
   Replace with `mirror/package.json` and `mirror/tsconfig.json`.

6. Verify mirror works: register a workspace, confirm notes are generated.

### Deliverables
- `mirror/` standalone TS package
- `src/` deleted
- `npm run build` inside `mirror/` produces `mirror/dist/`
- Workspace registration still generates mirror notes

---

## Phase 5 — Containerize

**Goal:** Single Docker image, runs in homelab, GitLab webhooks and git ops work,
data persists across restarts.

**Dependencies:** Phase 4 complete (clean codebase, no dead code).

### Runtime in container
```
python -m context_engine       ← PID 1 (or via tini)
  HTTP :7433                   ← webapp, /api/*, /mcp, /webhooks/gitlab
node (subprocess, short-lived) ← mirror/dist/src/mirror-cli.js
git (subprocess)               ← clone/pull from GitLab
```

### Steps

1. **`Dockerfile`**
   - Base: `python:3.11-slim`
   - Install: `nodejs`, `npm`, `git`, `tini`
   - Install Python deps: `pip install -r context_engine/requirements.txt`
   - Build mirror TS: `cd mirror && npm ci && npm run build`
   - Copy source
   - Entrypoint: `tini -- python -m context_engine --data-dir /data`
   - Expose: `7433`

2. **`docker-compose.yml`**
   ```yaml
   services:
     context-garden:
       build: .
       ports:
         - "7433:7433"
       volumes:
         - cg-data:/data
         - ~/.ssh/id_ed25519:/root/.ssh/id_ed25519:ro   # GitLab SSH key
       environment:
         CG_OLLAMA_HOST: http://ollama:11434     # or your homelab Ollama URL
         CG_DATA_DIR: /data
       restart: unless-stopped
   volumes:
     cg-data:
   ```

3. **Env vars** — document in `.env.example`:
   ```
   CG_OLLAMA_HOST=http://ollama:11434
   CG_EMBED_PROVIDER=ollama
   CG_EMBED_MODEL=nomic-embed-text
   CG_GITLAB_TOKEN=            # optional: token for HTTPS clone
   CG_WEBAPP_PORT=7433         # HTTP port
   ```

4. **GitLab webhook config** — after container is up, in Claude Code:
   ```
   register_workspace(name="my-repo", gitlab_url="https://gitlab.home.lab/org/repo")
   ```
   Then in GitLab → Settings → Webhooks:
   - URL: `http://<homelab-ip>:7433/webhooks/gitlab`
   - Secret: (shown in register_workspace output)
   - Events: Push events only

5. **Reverse proxy snippet** (Nginx/Traefik) for HTTPS termination if desired.

6. **Health check** in Dockerfile:
   ```dockerfile
   HEALTHCHECK --interval=30s --timeout=5s \
     CMD curl -f http://localhost:7433/api/status || exit 1
   ```

### Deliverables
- `Dockerfile`
- `docker-compose.yml`
- `.env.example`
- Container builds and runs
- Webapp reachable, MCP reachable, webhooks functional

---

## Dependency Graph

```
Phase 1 (restructure)
    ↓
Phase 2 (Python MCP endpoint)    ← can test MCP before touching TCP
    ↓
Phase 3 (remove TCP daemon)      ← safe to remove only after MCP is verified
    ↓
Phase 4 (slim TS to mirror-only) ← safe to remove TS app only after TCP is gone
    ↓
Phase 5 (containerize)           ← clean codebase before writing Dockerfile
```

Each phase is independently committable. Phases 1–3 are pure Python.
Phase 4 is pure TS cleanup. Phase 5 adds only ops files.
