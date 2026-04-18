# Refactor Program: Python Ownership + FP Core / OO Shell

This document is a long-running implementation program, not just an architecture sketch.

Goals:
- Make Python authoritative for business logic and state.
- Keep TypeScript as a thin MCP protocol adapter.
- Refactor safely on existing production logic using seams and staged cutovers.
- Adopt a "functional core, OO shell" style for maintainability.

Non-goals:
- Big-bang rewrite.
- Large behavior changes during seam creation.

---

## Problem Summary

Current architecture has overlapping responsibilities across Python and TypeScript:

| Concern | Python (`graph/`) | TypeScript (`src/`) |
|---|---|---|
| Workspace registry | `workspaces.json` + daemon loading | `src/workspace/registry.ts` |
| File watching | watchdog in daemon | `mirror-watcher.ts`, `lint-watcher.ts` |
| GitLab sync | `jobs.py` + webhook in `http_server.py` | `gitlab-manager.ts` |
| Mirroring (code->md) | partial/none | `src/mirror/` parsers |
| Query transport | TCP JSON-RPC handlers | bridge/state in `context-engine.ts` |

Result: duplicate orchestration, drift risk, and expensive redesigns.

---

## Target Architecture

```text
Claude
  -> stdio
src/mcp/server.ts              (thin MCP adapter; presentation formatting)
  -> HTTP (:7433)
graph/http_server.py           (HTTP adapter only)
graph/app/*                    (OO shell: orchestration/use-cases)
graph/domain/*                 (FP core: pure transforms/rules)
graph/infra/*                  (adapters: git, fs, queue, persistence)
graph/daemon.py                (runtime host + engines)
```

Ownership:
- Python owns registry, webhook handling, sync jobs, mirroring, indexing, and query APIs.
- TypeScript owns MCP wire protocol and Claude-facing response formatting.

---

## Implementation Guidelines

### 1) Delivery Strategy

Use **Strangler + Branch-by-Abstraction** with explicit dual implementations:
- Add seams first.
- Keep one stable adapter per capability (for example `webhook_service.py`).
- Maintain side-by-side implementations (`*_legacy.py`, `*_v2.py`).
- Migrate one vertical slice at a time.
- Remove old path only after soak period.

No big-bang merges.

### 2) Legacy/V2 Conventions

Required naming and switch pattern:
- `service_legacy.py`: existing behavior, frozen except critical fixes.
- `service_v2.py`: new behavior, introduced incrementally.
- `service.py`: active adapter that selects one implementation.

Selection policy:
- Primary switch is code-level adapter selection (easy to read and review).
- Runtime env flags are optional and only for operational rollback/canary.

Migration pattern inside v2:
- Stage 1: v2 delegates completely to legacy.
- Stage 2: v2 replaces one step and delegates the rest.
- Stage 3: v2 owns the full flow; legacy becomes removable.

### 3) Functional Core, OO Shell

Functional Core:
- Pure functions for parsing, normalization, matching, diffing, and policy decisions.
- No network, filesystem, subprocess, or mutable global state.
- Deterministic inputs/outputs for easy tests.

OO Shell:
- Classes orchestrate IO and workflows.
- Dependencies injected via interfaces (ports).
- Shell publishes domain events and calls pure functions.

Rule of thumb:
- If code "decides", prefer FP.
- If code "does", prefer OO shell.

### 4) Seams and Ports

Introduce explicit ports:
- `WorkspaceRepository`
- `WebhookInboxRepository` (dedupe/replay safety)
- `EventBus`
- `JobQueue`
- `GitClient`
- `MirrorService`
- `IndexerService`
- `Clock`/`IdGenerator` where useful for determinism

Adapters implement ports (filesystem, subprocess git, in-memory queue, sqlite, etc.).

### 5) Event-Driven Pattern

Adopt in-process pub/sub first:
- `WebhookReceived`
- `WorkspaceSyncRequested`
- `SyncStarted`
- `MirrorCompleted`
- `ReindexQueued`
- `SyncFailed`

Requirements:
- Idempotency key on webhook delivery (`delivery_id` or `workspace+commit_sha`).
- At-least-once handling with dedupe.
- Per-workspace ordering guarantees.

### 6) Testing Rules

Before moving behavior:
- Add characterization/snapshot tests for current outputs.
- Build parser parity harness (TS vs Python note output) before deleting TS parsers.

For each seam:
- Contract tests for port interfaces.
- Unit tests for pure functions.
- Integration tests for orchestration paths.

### 7) Rollout and Safety

Every cutover must have:
- Adapter-based rollback (`*_legacy` and `*_v2` both callable).
- Shadow mode option (run v2 in compare mode before ownership change).
- Clear deletion criteria for legacy code.

Optional operational safety:
- Add runtime flag only where no-redeploy rollback is required.

PR constraints:
- Small, monotonic, releasable.
- Avoid mixing broad refactor + behavior change.

### 8) Observability Baseline

Emit structured logs and metrics for:
- Webhook acceptance/ignore/reject reasons.
- Dedupe hits.
- Queue depth and processing latency by workspace.
- Failure stage (`fetch`, `mirror`, `reindex`).

---

## Refactor Instructions

### Phase 0: Baseline Harness and Guardrails

1. Add characterization tests for:
   - webhook branch filtering behavior
   - sync job status transitions
   - current mirror output shape
2. Establish legacy/v2 adapter skeletons for the first seam:
   - `webhook_service_legacy.py`
   - `webhook_service_v2.py`
   - `webhook_service.py` (active adapter)
3. Add structured correlation IDs across webhook -> job -> reindex.

Exit criteria:
- Existing behavior captured in tests.
- Adapter points exist and default to legacy.

### Phase 1: Create Seams (No Behavior Change)

1. Introduce app/domain/infra modules and ports.
2. Wrap existing implementations behind ports:
   - jobs queue
   - workspace read/write
   - git operations
   - mirror invocation
3. Keep current endpoint and daemon behavior, but route through new abstractions.
4. Add parallel sync seam modules:
   - `sync_orchestrator_legacy.py`
   - `sync_orchestrator_v2.py`
   - `sync_orchestrator.py` (active adapter)

Exit criteria:
- No user-facing behavior change.
- Existing tests still pass with seam wrappers.

### Phase 2: Webhook Pipeline (Pub/Sub + Idempotency)

1. Implement v2 webhook flow in `webhook_service_v2.py`:
   - verify token
   - normalize payload
   - dedupe via inbox
   - publish `WorkspaceSyncRequested`
2. Introduce in-process event bus.
3. Keep `webhook_service_legacy.py` intact and switch ownership only via `webhook_service.py` adapter.
4. Optional: add runtime flag only if operational rollback without deploy is required.

Current reference path to replace incrementally:
- `graph/http_server.py` webhook route
- `graph/jobs.py` direct enqueue call path

Exit criteria:
- Duplicate webhook deliveries do not enqueue duplicate sync jobs.
- Per-workspace ordering preserved.

### Phase 3: Workspace API Ownership in Python

1. Implement/standardize:
   - `POST /api/workspaces` (register)
   - `GET /api/workspaces`
   - `POST /api/workspaces/{id}/unregister`
2. Move registration rules from TS workspace layer into Python app service.
3. TS calls Python APIs only; no local registry ownership.

Exit criteria:
- TS `workspace/registry.ts` and GitLab manager no longer authoritative.

### Phase 4: HTTP Query Endpoint Completion

Add thin HTTP wrappers for existing daemon handlers:
- `POST /api/retrieve`
- `POST /api/find-path`
- `POST /api/rate`
- `GET /api/note`
- `GET /api/stats`

TS MCP layer switches from TCP client orchestration to HTTP calls via adapter seam migration.

Exit criteria:
- MCP tools work with HTTP-only backend path.

### Phase 5: Queue and Worker Model Upgrade

1. Move from single global pending scan to partitioned queue by `workspace_id`.
2. Allow concurrency across workspaces with in-order execution per workspace.
3. Add backpressure and retry policy by stage.

Exit criteria:
- No cross-workspace head-of-line blocking.
- Clear failure reporting per stage.

### Phase 6: Mirror Port (TS -> Python)

1. Build `graph/mirror/` Python implementation in v2 mirror service.
2. Run parity harness against TS parser outputs.
3. Enable v2 shadow mode; compare output drift.
4. Switch active adapter from legacy mirror service to v2 mirror service.
5. Remove TS mirror only after parity threshold is met.

Exit criteria:
- Stable parity and indexing quality.

### Phase 7: TS MCP Slim-Down

1. Shrink `src/mcp/server.ts` to protocol adapter + response formatting.
2. Remove daemon spawn/stateful orchestration from TS.
3. Keep `formatContext`-style presentation logic in TS.

Exit criteria:
- TS has no business-logic ownership.

### Phase 8: Cleanup and Deletion

Delete deprecated modules after soak:
- `*_legacy.py` modules with passed deletion criteria
- TS registry/watcher/sync/mirror orchestration code
- legacy bridge paths superseded by HTTP + Python services

Exit criteria:
- Single source of truth and no dead compatibility code.

---

## Suggested Target Structure (Python)

```text
graph/
  app/
    services/
      webhook_service.py
      webhook_service_legacy.py
      webhook_service_v2.py
      sync_orchestrator.py
      sync_orchestrator_legacy.py
      sync_orchestrator_v2.py
      workspace_service.py
    ports/
      workspace_repository.py
      inbox_repository.py
      event_bus.py
      git_client.py
      mirror_service.py
      indexer_service.py
  domain/
    webhook/
      models.py
      normalize.py
      policies.py
    sync/
      models.py
      state_machine.py
  infra/
    repo/
      workspace_json_repo.py
      webhook_inbox_sqlite.py
    queue/
      inproc_event_bus.py
      partitioned_job_queue.py
    git/
      subprocess_git_client.py
    mirror/
      mirror_service_legacy.py
      mirror_service_v2.py
```

---

## Working Agreement

Team rules for this refactor program:

1. No new feature work in `*_legacy.py`.
   - Allowed in legacy: critical bug fixes, security fixes, and compatibility fixes needed to keep production stable.
   - Required for feature requests: implement in `*_v2.py` only.
2. Every v2 cutover must include explicit legacy deletion criteria.
   - Each cutover PR must state:
     - parity checks passed (tests and/or snapshot comparisons),
     - soak window complete (duration and observation notes),
     - rollback path validated,
     - exact legacy files scheduled for deletion.
3. Legacy ownership must be temporary.
   - When deletion criteria are met, remove legacy code in the next cleanup PR.
   - Do not defer cleanup behind unrelated roadmap work.

PR checklist item (required):
- `Legacy touched?` If yes, justify why it is an allowed exception.
- `V2 cutover?` If yes, include legacy deletion criteria in the PR description.

---

## Risks and Controls

1. Mirror parity drift.
   - Control: snapshot parity harness + shadow cutover.
2. Regressions during seam introduction.
   - Control: no-behavior-change Phase 1 + characterization tests.
3. Operational complexity from event flow.
   - Control: start with in-process pub/sub and typed events before external brokers.
4. Migration fatigue.
   - Control: vertical slices with concrete exit criteria and deletions at end of each milestone.
