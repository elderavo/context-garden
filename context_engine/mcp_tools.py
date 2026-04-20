"""MCP tool definitions for ContextGarden.

Exposes 8 tools over StreamableHTTP via FastMCP:
  retrieve_context, find_path, rate_context,
  list_workspaces, register_workspace, unregister_workspace,
  configure, setup
"""

from __future__ import annotations

import asyncio
import collections
import datetime
import logging
import shutil
import urllib.request
import urllib.error
from pathlib import Path
from typing import TYPE_CHECKING, Any

from mcp.server.fastmcp import FastMCP

from .config import get_config_snapshot, write_config, get_synth_config

if TYPE_CHECKING:
    from .server import _DaemonServer

log = logging.getLogger(__name__)

DEFAULT_MAX_CHARS = 15_000


def create_mcp_server(
    daemon: "_DaemonServer",
    data_dir: Path,
    job_queue: Any,
    workspace_repo: Any,
    mirror_service: Any,
    activity_ring: collections.deque,
    rate_ring: collections.deque,
) -> FastMCP:
    """Build and return the FastMCP server wired to the live daemon."""

    mcp = FastMCP("context-garden", streamable_http_path="/")

    # ── retrieve_context ──────────────────────────────────────────────────────

    @mcp.tool(description=(
        "Search the knowledge base for relevant notes, tools, concepts, and best "
        "practices. Returns markdown-formatted context from the knowledge graph. "
        "Call this before relying on your own knowledge for any project-specific question."
    ))
    async def retrieve_context(
        query: str,
        search_terms: str | None = None,
        workspace: str | None = None,
        max_chars: int | None = None,
    ) -> str:
        """
        query: Your intent in natural language.
        search_terms: Keyword/embedding string for vector DB seed retrieval. Omit to use query.
        workspace: Limit to a specific workspace by name. Omit to search all.
        max_chars: Maximum characters to return (default 15000).
        """
        import time
        retrieval_id = f"r-{int(time.time()*1000):x}-{id(query) & 0xffff:04x}"

        params: dict[str, Any] = {"query": search_terms or query}
        if workspace:
            params["workspace"] = workspace

        result = await asyncio.to_thread(daemon.handle_query_retrieve, params)

        # Record to activity ring for webapp
        seed_notes = result.get("seed_notes", [])
        expanded_notes = result.get("expanded_notes", [])
        notes = [
            {"title": Path(n.get("noteId", "")).stem, "workspace": n.get("workspace", ""),
             "score": round(n.get("score", 0), 3), "via": None}
            for n in seed_notes[:8]
        ] + [
            {"title": Path(n.get("noteId", "")).stem, "workspace": n.get("workspace", ""),
             "score": round(n.get("score", 0), 3), "via": n.get("viaEdge")}
            for n in expanded_notes[:4]
        ]
        activity_ring.appendleft({
            "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "query": query,
            "noteCount": len(seed_notes) + len(expanded_notes),
            "notes": notes,
        })

        formatted = result.get("formattedContext", "")
        budget = max(500, max_chars or DEFAULT_MAX_CHARS)
        if len(formatted) > budget:
            formatted = formatted[:budget] + "\n\n_(context truncated to fit budget)_\n<!-- End ContextGarden Context -->"
        formatted += f"\n\n<!-- retrieval_id: {retrieval_id} -->"
        return formatted

    # ── find_path ─────────────────────────────────────────────────────────────

    @mcp.tool(description=(
        "Find the shortest path between two concepts in the knowledge graph. "
        "Returns the chain of notes and edge types connecting them."
    ))
    async def find_path(
        start: str,
        end: str,
        edge_types: list[str] | None = None,
        max_depth: int | None = None,
        workspace: str | None = None,
    ) -> str:
        """
        start: Starting concept or note path.
        end: Ending concept or note path.
        edge_types: Edge types to traverse. Default: all.
        max_depth: Maximum path length in hops (default 8).
        workspace: Limit search to one workspace.
        """
        params: dict[str, Any] = {"start": start, "end": end}
        if edge_types:
            params["edge_types"] = edge_types
        if max_depth:
            params["max_depth"] = max_depth
        if workspace:
            params["workspace"] = workspace

        result = await asyncio.to_thread(daemon.handle_query_find_path, params)

        if result.get("no_path"):
            parts = [f'No path found between "{start}" and "{end}".']
            if result.get("startId"):
                parts.append(f'Start resolved to: {result["startId"]} ({result.get("startResolvedBy")})')
            if result.get("endId"):
                parts.append(f'End resolved to: {result["endId"]} ({result.get("endResolvedBy")})')
            return "\n".join(parts)

        text = result.get("formattedContext", "")
        if len(text) > DEFAULT_MAX_CHARS:
            text = text[:DEFAULT_MAX_CHARS] + "\n\n_(path truncated)_"
        header = (
            f'_Start: "{start}" → {result.get("startId")} ({result.get("startResolvedBy")}) '
            f'| End: "{end}" → {result.get("endId")} ({result.get("endResolvedBy")})_\n\n'
        )
        return header + text

    # ── rate_context ──────────────────────────────────────────────────────────

    @mcp.tool(description=(
        "Rate how well the last retrieve_context result answered your query. "
        "Your rating helps the knowledge base improve over time."
    ))
    async def rate_context(
        retrieval_id: str,
        query: str,
        score: int,
        missing: str = "",
        helpful: str = "",
    ) -> str:
        """
        retrieval_id: The retrieval_id from the <!-- retrieval_id: ... --> comment.
        query: The original query.
        score: 1 (irrelevant) to 5 (exactly what was needed).
        missing: What information was missing.
        helpful: Which notes were most useful.
        """
        if not 1 <= score <= 5:
            return "score must be between 1 and 5."
        rate_ring.appendleft({
            "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "retrieval_id": retrieval_id,
            "query": query,
            "score": score,
            "helpful": helpful,
            "missing": missing,
        })
        return f"Rating recorded: {score}/5. Thank you — this helps the knowledge base improve."

    # ── list_workspaces ───────────────────────────────────────────────────────

    @mcp.tool(description="List all registered workspaces and their status.")
    async def list_workspaces() -> str:
        from .app.services import workspace_service
        entries = workspace_repo.list_all()
        all_jobs = job_queue.list_jobs()
        summaries = workspace_service.list_workspace_summaries(entries=entries, jobs=all_jobs)
        if not summaries:
            return "No workspaces registered."
        lines = [
            "| Name | Type | Languages | Source | Active | Registered |",
            "|------|------|-----------|--------|--------|------------|",
        ]
        for e in summaries:
            src_type = (
                f'gitlab:{e.get("branch") or "?"}' if e.get("sourceType") == "gitlab" else "local"
            )
            langs = ", ".join(e.get("languages") or [])
            source = e.get("source") or ""
            lines.append(
                f'| {e["name"]} | {src_type} | {langs} | {source} | '
                f'{"yes" if e.get("active") else "no"} | {str(e.get("registeredAt", ""))[:10]} |'
            )
        return "\n".join(lines)

    # ── register_workspace ────────────────────────────────────────────────────

    @mcp.tool(description=(
        "Register a workspace for code mirroring. "
        "Pass gitlab_url to register a GitLab-backed repo (recommended). "
        "Pass source_dir for a local directory."
    ))
    async def register_workspace(
        name: str,
        languages: list[str],
        gitlab_url: str | None = None,
        gitlab_branch: str = "main",
        gitlab_token: str | None = None,
        source_dir: str | None = None,
    ) -> str:
        """
        name: Unique slug (lowercase alphanumeric + hyphens).
        languages: Languages to mirror (e.g. ["ts", "py"]).
        gitlab_url: GitLab project URL. Preferred over source_dir.
        gitlab_branch: Branch to track (default: main).
        gitlab_token: Personal access token. Falls back to CG_GITLAB_TOKEN env var.
        source_dir: Absolute path to a local directory (use gitlab_url instead).
        """
        from .app.services import workspace_service
        from .infra.git.subprocess_git_client import SubprocessGitClient
        git_client = SubprocessGitClient()
        payload = {
            "name": name,
            "languages": languages,
            "gitlab_url": gitlab_url,
            "gitlab_branch": gitlab_branch,
            "gitlab_token": gitlab_token,
            "source_dir": source_dir,
        }
        result = await workspace_service.register_workspace(
            payload=payload,
            data_dir=data_dir,
            workspace_repository=workspace_repo,
            git_client=git_client,
            mirror_service=mirror_service,
        )
        entry = result.entry
        # Trigger background sync + reindex
        asyncio.create_task(_post_register_sync(daemon, entry.get("name", name)))

        if entry.get("sourceType") == "gitlab":
            gc = entry.get("gitlabConfig", {})
            return (
                f'Workspace "{entry["name"]}" registered (GitLab).\n'
                f'Project: {gc.get("projectUrl")} @ {gc.get("branch")}\n'
                f'Languages: {", ".join(entry.get("languages", []))}\n'
                f'Notes generated: {result.notes_generated}\n'
                f'Indexing: running in background\n\n'
                f'Webhook setup (GitLab → Settings → Webhooks):\n'
                f'  URL:    http://<cg-host>:7433/webhooks/gitlab\n'
                f'  Secret: {gc.get("webhookSecret")}\n'
                f'  Events: Push events only'
            )
        return (
            f'Workspace "{entry["name"]}" registered.\n'
            f'Source: {entry.get("sourceDir")}\n'
            f'Languages: {", ".join(entry.get("languages", []))}\n'
            f'Notes generated: {result.notes_generated}\n'
            f'Indexing: running in background'
        )

    # ── unregister_workspace ──────────────────────────────────────────────────

    @mcp.tool(description=(
        "Unregister a workspace: stops watcher, deletes mirrored notes, removes from registry."
    ))
    async def unregister_workspace(name: str) -> str:
        """name: Workspace name to remove."""
        from .app.services import workspace_service
        entries = workspace_repo.list_all()
        target = next((e for e in entries if e.get("name") == name), None)
        if not target:
            return f'Workspace "{name}" not found.'
        result = workspace_service.unregister_workspace(
            workspace_id=target["id"],
            data_dir=data_dir,
            workspace_repository=workspace_repo,
        )
        if not result.removed:
            return f'Workspace "{name}" not found.'
        await asyncio.to_thread(daemon.handle_workspaces_sync, {})
        return (
            f'Workspace "{name}" unregistered.\n'
            f'Notes deleted: {len(result.deleted_paths)}'
        )

    # ── configure ─────────────────────────────────────────────────────────────

    @mcp.tool(description=(
        "View or update ContextGarden configuration at runtime. "
        "Pass no arguments to view current config. "
        "Set persist=true to save changes to config.json."
    ))
    async def configure(
        embed_provider: str | None = None,
        embed_model: str | None = None,
        embed_host: str | None = None,
        embed_api_key: str | None = None,
        llm_provider: str | None = None,
        llm_model: str | None = None,
        llm_host: str | None = None,
        llm_api_key: str | None = None,
        persist: bool = False,
    ) -> str:
        """
        embed_provider: ollama | openai | local
        llm_provider: ollama | openai | anthropic
        persist: Save to config.json (default false — session only).
        """
        patch: dict[str, Any] = {}
        if embed_provider:  patch["embedProvider"] = embed_provider
        if embed_model:     patch["embedModel"] = embed_model
        if embed_host:      patch["embedHost"] = embed_host
        if embed_api_key:   patch["embedApiKey"] = embed_api_key
        if llm_provider:    patch["llmProvider"] = llm_provider
        if llm_model:       patch["llmModel"] = llm_model
        if llm_host:        patch["llmHost"] = llm_host
        if llm_api_key:     patch["llmApiKey"] = llm_api_key

        if patch:
            write_config(patch, persist=persist)

        snap = get_config_snapshot()
        lines = [
            "# ContextGarden Configuration", "",
            "## Embedding",
            f'- Provider: {snap["embedProvider"]}',
            f'- Model: {snap["embedModel"]}',
            f'- Host: {snap["embedHost"]}',
            f'- API Key: {"set" if snap["embedApiKey"] else "not set"}',
            "",
            "## LLM (Synthesizer)",
            f'- Provider: {snap["llmProvider"]}',
            f'- Model: {snap["llmModel"]}',
            f'- Host: {snap["llmHost"]}',
            f'- API Key: {"set" if snap["llmApiKey"] else "not set"}',
        ]
        if patch:
            lines += ["", f'_Changes {"saved to config.json" if persist else "applied for this session only"}._']
        return "\n".join(lines)

    # ── setup ─────────────────────────────────────────────────────────────────

    @mcp.tool(description=(
        "Guided 0-to-running setup. Call with no arguments to see current status. "
        "Pass provider config and/or workspace info to configure in one call."
    ))
    async def setup(
        embed_provider: str | None = None,
        embed_model: str | None = None,
        embed_host: str | None = None,
        embed_api_key: str | None = None,
        llm_provider: str | None = None,
        llm_model: str | None = None,
        llm_host: str | None = None,
        llm_api_key: str | None = None,
        workspace_name: str | None = None,
        workspace_gitlab_url: str | None = None,
        workspace_source_dir: str | None = None,
        workspace_languages: list[str] | None = None,
        persist: bool = True,
        test_connection: bool = False,
    ) -> str:
        """
        Provider defaults applied automatically when provider is set without model/host.
        embed_provider: ollama | openai | local
        llm_provider: ollama | openai | anthropic
        workspace_name: Slug for the workspace.
        workspace_gitlab_url: GitLab project URL (preferred).
        workspace_source_dir: Local path (use gitlab_url instead).
        workspace_languages: Languages to mirror.
        persist: Persist provider config (default true).
        test_connection: Test LLM provider connectivity.
        """
        lines: list[str] = []

        # Apply provider defaults
        resolved_embed_model = embed_model or (
            "text-embedding-3-small" if embed_provider == "openai" else
            "nomic-embed-text:latest" if embed_provider in ("local", "ollama") else None
        )
        resolved_embed_host = embed_host or (
            "https://api.openai.com" if embed_provider == "openai" else
            "http://localhost:11434" if embed_provider in ("local", "ollama") else None
        )
        resolved_llm_model = llm_model or (
            "gpt-4o" if llm_provider == "openai" else
            "claude-3-5-haiku-latest" if llm_provider == "anthropic" else None
        )
        resolved_llm_host = llm_host or (
            "https://api.openai.com" if llm_provider == "openai" else
            "https://api.anthropic.com" if llm_provider == "anthropic" else
            "http://localhost:11434" if llm_provider == "ollama" else None
        )

        patch: dict[str, Any] = {}
        if embed_provider:        patch["embedProvider"] = embed_provider
        if resolved_embed_model:  patch["embedModel"] = resolved_embed_model
        if resolved_embed_host:   patch["embedHost"] = resolved_embed_host
        if embed_api_key:         patch["embedApiKey"] = embed_api_key
        if llm_provider:          patch["llmProvider"] = llm_provider
        if resolved_llm_model:    patch["llmModel"] = resolved_llm_model
        if resolved_llm_host:     patch["llmHost"] = resolved_llm_host
        if llm_api_key:           patch["llmApiKey"] = llm_api_key

        if patch:
            write_config(patch, persist=persist)
            lines.append("_Provider config updated._\n")

        # Register workspace if requested
        ws_result = ""
        if workspace_name and (workspace_gitlab_url or workspace_source_dir):
            from .app.services import workspace_service
            from .infra.git.subprocess_git_client import SubprocessGitClient
            git_client = SubprocessGitClient()
            payload = {
                "name": workspace_name,
                "languages": workspace_languages or ["ts"],
                "gitlab_url": workspace_gitlab_url,
                "source_dir": workspace_source_dir,
            }
            try:
                result = await workspace_service.register_workspace(
                    payload=payload,
                    data_dir=data_dir,
                    workspace_repository=workspace_repo,
                    git_client=git_client,
                    mirror_service=mirror_service,
                )
                asyncio.create_task(_post_register_sync(daemon, workspace_name))
                ws_result = f'✓ Workspace "{workspace_name}" registered — {result.notes_generated} notes, indexing in background.'
            except Exception as exc:
                ws_result = f'⚠ Workspace registration failed: {exc}'
        elif workspace_name or workspace_gitlab_url or workspace_source_dir:
            ws_result = "⚠ Provide workspace_name and one of workspace_gitlab_url / workspace_source_dir."

        # Connectivity test
        conn_result = ""
        if test_connection:
            conn_result = "✓ LLM reachable." if _check_llm_reachable() else "⚠ LLM NOT reachable — check host, model, and API key."

        # Status report
        snap = get_config_snapshot()
        embed_key_needed = snap["embedProvider"] == "openai" and not snap["embedApiKey"]
        llm_key_needed = snap["llmProvider"] in ("openai", "anthropic") and not snap["llmApiKey"]

        lines += [
            "## ContextGarden Setup\n",
            "### Embedding Provider",
            f'- Provider: {snap["embedProvider"]}',
            f'- Model:    {snap["embedModel"]}',
            f'- Host:     {snap["embedHost"]}',
            f'- API Key:  {"✓ set" if snap["embedApiKey"] else ("⚠ required" if embed_key_needed else "— not required")}',
            "",
            "### LLM Provider",
            f'- Provider: {snap["llmProvider"]}',
            f'- Model:    {snap["llmModel"]}',
            f'- Host:     {snap["llmHost"]}',
            f'- API Key:  {"✓ set" if snap["llmApiKey"] else ("⚠ required" if llm_key_needed else "— not required")}',
            "",
        ]

        entries = workspace_repo.list_all()
        lines.append("### Workspaces")
        if ws_result:
            lines.append(ws_result)
        if not entries:
            lines.append("_(none registered)_")
        else:
            for e in entries:
                langs = ", ".join(e.get("languages") or [])
                src = e.get("gitlabConfig", {}).get("projectUrl") if e.get("sourceType") == "gitlab" else e.get("sourceDir")
                lines.append(f'- ✓ **{e["name"]}** — {src}  [{langs}]')
        lines.append("")

        if conn_result:
            lines += ["### Connectivity", conn_result, ""]

        issues = []
        if embed_key_needed:
            issues.append("- Provide `embed_api_key`")
        if llm_key_needed:
            issues.append("- Provide `llm_api_key`")
        if not entries:
            issues.append("- Register a workspace: provide `workspace_name` + `workspace_gitlab_url`")

        lines.append("### Next Steps")
        if not issues:
            lines.append("✓ Setup complete. Try `retrieve_context` with a query about your codebase.")
        else:
            lines.extend(issues)

        return "\n".join(lines)

    return mcp


# ── Helpers ───────────────────────────────────────────────────────────────────


async def _post_register_sync(daemon: "_DaemonServer", workspace_name: str) -> None:
    try:
        await asyncio.to_thread(daemon.handle_workspaces_sync, {})
        await asyncio.to_thread(daemon.handle_index_rebuild, {"name": workspace_name})
    except Exception as exc:
        log.error("Post-register sync/reindex failed for %s: %s", workspace_name, exc)


def _check_llm_reachable() -> bool:
    synth = get_synth_config()
    host = synth.get("host", "").rstrip("/")
    if not host:
        return False
    try:
        req = urllib.request.Request(f"{host}/api/version", method="GET")
        with urllib.request.urlopen(req, timeout=3.0):
            return True
    except Exception:
        pass
    try:
        req = urllib.request.Request(f"{host}/v1/models", method="GET")
        with urllib.request.urlopen(req, timeout=3.0):
            return True
    except Exception:
        return False
