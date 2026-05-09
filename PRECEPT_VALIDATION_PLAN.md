# Precept Validation Plan

## What We're Building

Per-workspace (and global) coding conventions stored as natural-language rules. On every sync, changed files are checked against those rules by an LLM pass. Violations are persisted and surfaced in the UI and via MCP.

Example precepts:
- `"use camelCase for all identifiers"`
- `"this is a functional project — no classes or mutable global state"`
- `"all public functions must have return type annotations"`
- `"never catch exceptions silently — always log or re-raise"`

---

## Architecture

```
precepts (workspace entry + global file)
         ↓
sync pipeline (after mirror, per changed file)
         ↓
PrecepValidator (llama.cpp LLM pass → structured JSON)
         ↓
ViolationsSqliteRepository
         ↓
HTTP API + UI + MCP tool
```

---

## 1. Precept Storage

### Workspace-level
Add `precepts: list[str]` to workspace entries in `workspaces.json`. Empty list = no validation for that workspace.

Register/update via existing workspace config or new dedicated endpoints.

### Global
New file: `data/.context-garden/precepts.json` — `{"precepts": ["...", "..."]}`.  
Global precepts apply to every workspace and stack with workspace-specific ones at validation time.

---

## 2. Mirror CLI: Surface Changed File Paths

**Current**: mirror returns `{"written": {"tier1": 3, "tier2": 1}, ...}` — counts only.

**Change**: extend mirror result to include `changedFiles: string[]` — the source file paths that were written/updated (not the generated note paths). The TS mirror CLI already iterates changed files; just append them to the JSON output.

File: `src/mirror/run-mirror.ts` — add `changedFiles` to the return object.  
File: `context_engine/infra/mirror/mirror_service.py` — pass through `result.get("changedFiles", [])`.

---

## 3. Violation Storage

New SQLite table in `data/.context-garden/violations.db` (or extend `webhook_inbox.db`):

```sql
CREATE TABLE IF NOT EXISTS violations (
    id          TEXT PRIMARY KEY,
    workspace_id TEXT NOT NULL,
    file_path   TEXT NOT NULL,
    precept     TEXT NOT NULL,
    description TEXT NOT NULL,
    line_hint   TEXT,          -- optional, best-effort from LLM
    detected_at TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'open'  -- open | dismissed
);
CREATE INDEX IF NOT EXISTS idx_violations_workspace ON violations(workspace_id, status);
```

New file: `context_engine/infra/repo/violations_sqlite.py`  
- `upsert_violations(workspace_id, file_path, violations: list[dict]) → None` — replaces all open violations for that (workspace, file_path) pair on each sync (fixing a file clears its violations automatically)
- `list_violations(workspace_id=None, status="open") → list[dict]`
- `dismiss(violation_id) → None`

---

## 4. Precept Validator Service

New file: `context_engine/infra/precepts/precept_validator.py`

Uses the same llama.cpp server as `CodeNoteSummarizer` (`CG_LLAMA_HOST`, default `http://10.0.132.7:8080`).

### Prompt structure
```
You are a code reviewer enforcing project conventions.

Precepts (rules this project must follow):
1. {precept_1}
2. {precept_2}
...

File: {file_path}
---
{file_content}
---

List every violation of the above precepts in this file as a JSON array.
Each item: {"precept": "<the rule text>", "description": "<what's wrong>", "line_hint": "<line number or range, if identifiable, else null>"}
If there are no violations, return [].
Return only the JSON array, no other text.
```

### Interface
```python
@dataclass
class PrecepViolation:
    precept: str
    description: str
    line_hint: str | None

async def validate_file(
    file_path: str,
    precepts: list[str],
    *,
    llama_host: str,
) -> list[PrecepViolation]:
    ...
```

Robust to LLM JSON parse failures — log warning, return `[]` (never fail the sync).

### Workspace validator
```python
async def validate_workspace_changes(
    *,
    workspace_id: str,
    workspace_name: str,
    changed_files: list[str],
    workspace_precepts: list[str],
    global_precepts: list[str],
    violations_repo: ViolationsSqliteRepository,
    llama_host: str,
    log_fn: Callable[[str], None],
) -> int:  # returns violation count
```

Validates files concurrently (asyncio.gather with a semaphore, max 4 parallel LLM calls). Upserts results. Returns total violation count written.

---

## 5. Sync Pipeline Integration

In `context_engine/app/services/sync_orchestrator.py`, after the mirror + summarize steps:

```python
if self.precept_validator is not None:
    changed_files = result.get("changedFiles", [])
    combined_precepts = global_precepts + workspace_precepts
    if changed_files and combined_precepts:
        log(f"Validating {len(changed_files)} changed files against {len(combined_precepts)} precepts...")
        violation_count = await self.precept_validator.validate_workspace_changes(
            workspace_id=entry["id"],
            workspace_name=entry["name"],
            changed_files=changed_files,
            workspace_precepts=workspace_precepts,
            global_precepts=global_precepts,
            ...
        )
        log(f"Precept validation complete. Violations found: {violation_count}.")
```

`SyncOrchestrator` gets a new optional `precept_validator` field (same pattern as `note_summarizer`). `build_default()` wires it up if llama host is available.

Applies to both `execute_sync` and `execute_rebuild`.

---

## 6. HTTP API

| Method | Route | Description |
|--------|-------|-------------|
| `GET` | `/api/workspaces/{id}/precepts` | Get workspace precepts |
| `PUT` | `/api/workspaces/{id}/precepts` | Replace workspace precepts (`{"precepts": [...]}`) |
| `GET` | `/api/precepts` | Get global precepts |
| `PUT` | `/api/precepts` | Replace global precepts |
| `GET` | `/api/violations` | All open violations (optional `?workspace_id=`) |
| `GET` | `/api/workspaces/{id}/violations` | Violations for one workspace |
| `POST` | `/api/violations/{id}/dismiss` | Dismiss a violation |

---

## 7. UI

### Workspace card
- Violation count badge next to workspace name: `3 violations` in amber/red if > 0
- Clicking it jumps to Violations tab filtered to that workspace

### Violations tab (new tab, between Jobs and Config)
- Table: Workspace | File | Precept | Description | Line | Detected | Actions
- Filter by workspace (dropdown)
- Dismiss button per row
- "Show dismissed" toggle

### Precepts editor
- In workspace detail / register form: textarea for workspace precepts (one per line)
- In Config tab: global precepts textarea
- Save button → PUT to respective endpoints

---

## 8. MCP Tool

New tool: `check_violations`

```
check_violations(workspace?: string) -> string
```

Returns a markdown-formatted list of open violations. If `workspace` is omitted, returns violations across all workspaces. Useful for agents to self-check before finishing a task.

---

## Critical Files

| File | Change |
|------|--------|
| `src/mirror/run-mirror.ts` | Add `changedFiles` to mirror output |
| `context_engine/infra/mirror/mirror_service.py` | Pass through `changedFiles` |
| `context_engine/infra/precepts/precept_validator.py` | **New** — LLM validation pass |
| `context_engine/infra/repo/violations_sqlite.py` | **New** — violation persistence |
| `context_engine/app/services/sync_orchestrator.py` | Wire in validator after mirror |
| `context_engine/http_server.py` | Add precept + violation routes |
| `context_engine/mcp_tools.py` | Add `check_violations` tool |
| `context_engine/static/index.html` | Violations tab + precepts editor + badge |

---

## Implementation Order

1. Mirror CLI change + `changedFiles` passthrough (unblocks everything else)
2. `violations_sqlite.py` + `precept_validator.py` (core logic, testable in isolation)
3. Sync orchestrator wiring
4. HTTP API routes
5. UI (violations tab + precepts editor)
6. MCP tool

---

## Out of Scope (for now)

- Auto-fix suggestions (LLM proposes a fix, not just flags)
- Precept severity levels (error vs warning)
- Per-file precept overrides
- Precept templates / presets (e.g. "Python FP preset")
- Notification on violation (Slack, etc.)
