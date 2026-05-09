# Context Garden Best Practices for Agents

This guide is for MCP clients and coding agents using Context Garden against a real codebase. It is intentionally operational: what to do, in what order, and what to avoid.

## Core Rule

Call `retrieve_context` before relying on your own memory for project-specific questions.

Context Garden is designed to front-load repository context. The intended agent flow in this repo is:

1. Ensure Context Garden is configured and the workspace exists.
2. Make sure sync and indexing have completed.
3. Retrieve context for orientation or a concrete question.
4. Use `find_path` if you need structural links between concepts.
5. Answer or act using the retrieved notes, not unstated assumptions.
6. Optionally call `rate_context` with the returned `retrieval_id`.

## Recommended Workflow

### 1. Start With Setup and Workspace State

Use these tools first when you do not yet trust the environment:

- `setup` for first-run configuration or quick status.
- `configure` to inspect or update embedding and LLM providers.
- `list_workspaces` to confirm the target workspace exists.
- `register_workspace` if the repo has not been added yet.

Best practices:

- Prefer GitLab-backed workspaces when possible. They are the primary workflow in this project and support webhook-driven sync.
- Prefer one workspace per branch when branch context matters. Current behavior is one branch per workspace.
- Keep workspace names short, stable, and lowercase. Registration enforces a slug-like format.
- Register the narrowest useful set of languages so mirroring stays relevant.

### 2. Do Not Query Too Early

Workspace registration generates notes immediately, but sync and reindex continue in the background. A newly registered workspace may exist before retrieval quality is actually good.

Before depending on answers:

- Confirm the workspace is registered.
- Give the background indexing pass time to finish.
- If results look thin or obviously stale, retry after sync/reindex rather than forcing an answer from weak context.

For GitLab workspaces, remember the expected lifecycle is:

`register_workspace` -> clone -> mirror -> background sync/reindex -> webhook-driven updates on push

### 3. Use The Right Retrieval Mode

Context Garden supports two retrieval modes:

- `discover`: orientation, entrypoints, module map, high-level structure.
- `targeted`: concrete implementation questions, symbols, files, behaviors, bugs, and flows.

Use `discover` when you need questions like:

- "Give me a codebase map."
- "Where should I start?"
- "What are the entrypoints?"
- "How is this organized?"

Use `targeted` when you need questions like:

- "How does auth token refresh work?"
- "Where is webhook verification implemented?"
- "Which code writes `workspaces.json`?"
- "What calls `trigger_reindex`?"

The engine can auto-select a mode, but agents should prefer being explicit when intent is clear.

### 4. Scope Retrieval Deliberately

If you know the repository you want, pass `workspace`.

Why:

- It reduces irrelevant hits.
- It avoids mixing notes from unrelated workspaces.
- It makes graph expansion and synthesis more reliable.

In a multi-workspace environment, do not assume leaving `workspace` unset will perform a useful global search. In this repository's current setup, unscoped retrieval can error and ask for an explicit workspace instead.

### 5. Write Better Queries Than "Explain The Repo"

Good retrieval depends on query quality.

Prefer:

- A concrete task or decision.
- Exact component names when you have them.
- User intent in normal language.
- `search_terms` when you want retrieval seeded with sharper keywords than the natural-language query.

Examples:

```text
retrieve_context(
  query="Where does webhook processing validate GitLab push requests, and what happens after validation?",
  workspace="context_garden",
  mode="targeted"
)
```

```text
retrieve_context(
  query="Orient me to the codebase areas involved in workspace sync and indexing.",
  workspace="context_garden",
  mode="discover"
)
```

```text
retrieve_context(
  query="I need the control flow for post-register indexing.",
  search_terms="post register sync reindex workspace",
  workspace="context_garden",
  mode="targeted"
)
```

## How To Interpret Results

### `retrieve_context`

`retrieve_context` returns markdown context, not just raw search hits.

Important behaviors from the implementation:

- Responses may be truncated to fit `max_chars`.
- The result ends with an HTML comment containing `retrieval_id` and the final mode.
- Targeted retrieval combines semantic/keyword seeds with graph expansion and inter-seed pathfinding.
- Discovery mode is a curated orientation map built from entrypoint tags, module notes, and graph degree.

Best practices:

- Treat the output as evidence to reason from, not as a final answer to quote blindly.
- If the result is truncated, rerun with a tighter query before increasing `max_chars` aggressively.
- Preserve the `retrieval_id` if you may call `rate_context` later.

### `find_path`

Treat `find_path` as experimental until you verify it against the current workspace.

In this repository's current environment, `find_path` failed to resolve valid `context_garden` note/file identifiers and returned an unrelated note from another workspace. Until that behavior is fixed, agents should not rely on it for proof of structure or ownership.

If you try it anyway:

- Prefer exact note identifiers from retrieved context, not guessed source-file paths.
- Keep `workspace` explicit.
- Verify that the resolved start/end values and returned notes are actually from the intended workspace.

If any of those checks fail, fall back to another targeted `retrieve_context` query or direct code inspection.

### `rate_context`

Use `rate_context` when the result was especially good or clearly lacking.

Include:

- `helpful` for notes or sections that were useful.
- `missing` for the gap that blocked the task.

This project records those ratings explicitly, so agents should use them when there is a real signal to provide.

## Agent Behavior Patterns That Work Well

### For Codebase Orientation

1. `list_workspaces`
2. `retrieve_context(..., mode="discover")`
3. `retrieve_context(..., mode="targeted")` for the specific subsystem
4. Prefer another targeted retrieval before using `find_path`; use `find_path` only if you verify it resolves correctly in the current workspace

### For Bug Fixing Or Implementation

1. Retrieve targeted context for the failing behavior.
2. Retrieve again with exact file/function names once they are known.
3. Prefer another targeted retrieval if ownership or call flow is still ambiguous; use `find_path` only as a checked experiment.
4. Only then form the code-change plan.

### For Reviewing A PR Or Design

1. Use targeted retrieval on the exact subsystem.
2. Use discover mode only if the local change touches unfamiliar architecture.
3. Rate poor retrieval when key notes are missing so the graph can improve over time.

## Common Mistakes

- Querying before the initial index or post-push sync is done.
- Leaving `workspace` unset when you already know the target repo.
- Using broad discovery prompts for narrow implementation questions.
- Using targeted prompts for "where do I start?" orientation.
- Treating one retrieval as exhaustive when the first result is truncated or vague.
- Answering from prior knowledge after retrieval returns weak evidence instead of refining the query.
- Forgetting that branch coverage is workspace-specific.

## Practical Heuristics

- Start with `discover` when you are new to the repo; switch to `targeted` as soon as you have concrete names.
- Prefer two small retrievals over one oversized vague retrieval.
- Include exact filenames, symbols, error strings, or job names once you learn them.
- Use `max_chars` as a budget control, not a substitute for query precision.
- If a result is empty or noisy, retry with a better query before concluding the knowledge base is bad.
- For GitLab-backed workspaces, assume push-driven freshness only if webhook sync is actually configured and healthy.

## Minimal Playbook

```text
1. list_workspaces
2. setup/configure if providers are missing
3. register_workspace if needed
4. wait for indexing if the workspace is new or recently synced
5. retrieve_context(mode="discover") for orientation
6. retrieve_context(mode="targeted") for the actual task
7. use `find_path` only after verifying it resolves correctly for the current workspace
8. rate_context when the retrieval quality provides useful feedback
```

That is the intended agent loop for Context Garden in this repository.
