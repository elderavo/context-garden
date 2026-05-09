"""Workspace app service: register/list/unregister ownership in Python."""

from __future__ import annotations

import logging
import os
import re
import secrets
import shutil
import subprocess
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Protocol

from ..ports.git_client import GitClient
from ..ports.mirror_service import MirrorService
from ..ports.workspace_repository import WorkspaceRepository

log = logging.getLogger(__name__)

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}[a-z0-9]$")
_WORKSPACE_STATUS_DEFAULTS: dict[str, Any] = {
    "lastWebhookReceivedAt": None,
    "lastWebhookStatus": "",
    "lastWebhookReason": "",
    "lastWebhookRef": "",
    "lastWebhookCommit": "",
    "lastWebhookDeliveryId": "",
    "lastSyncStartedAt": None,
    "lastSyncCompletedAt": None,
    "lastSyncStatus": "",
    "lastSyncError": "",
    "lastSyncedCommit": "",
    "currentCloneCommit": "",
}


class JobLike(Protocol):
    workspace_id: str
    status: str
    completed_at: str | None


_DEFAULT_CG_HOST = "http://localhost:7433"


@dataclass(frozen=True)
class RegisterWorkspaceResult:
    entry: dict[str, Any]
    notes_generated: int
    note_paths: list[str]
    webhook_provision: dict[str, Any] | None = None


@dataclass(frozen=True)
class UnregisterWorkspaceResult:
    removed: dict[str, Any] | None
    deleted_paths: list[str]


def webhook_path_for_workspace(workspace_id: str) -> str:
    return f"/webhooks/gitlab/{workspace_id}"


def get_workspace_status(entry: dict[str, Any]) -> dict[str, Any]:
    status = dict(_WORKSPACE_STATUS_DEFAULTS)
    raw = entry.get("status")
    if isinstance(raw, dict):
        status.update(raw)
    return status


def resolve_workspace_clone_commit(entry: dict[str, Any]) -> str:
    clone_dir = (
        entry.get("gitlabConfig", {}).get("cloneDir")
        if entry.get("sourceType") == "gitlab"
        else entry.get("sourceDir")
    )
    if not clone_dir:
        return ""
    try:
        result = subprocess.run(
            ["git", "-C", str(clone_dir), "rev-parse", "HEAD"],
            check=True,
            capture_output=True,
            text=True,
        )
    except Exception:
        return ""
    return result.stdout.strip()


def update_workspace_status(
    *,
    workspace_repository: WorkspaceRepository,
    workspace_id: str,
    patch: dict[str, Any],
) -> dict[str, Any] | None:
    entries = workspace_repository.list_all()
    updated: dict[str, Any] | None = None
    for entry in entries:
        if entry.get("id") != workspace_id:
            continue
        status = get_workspace_status(entry)
        status.update(patch)
        entry["status"] = status
        updated = entry
        break
    if updated is None:
        return None
    workspace_repository.save_all(entries)
    return updated


def enrich_workspace_summary(entry: dict[str, Any]) -> dict[str, Any]:
    status = get_workspace_status(entry)
    current_clone_commit = status.get("currentCloneCommit", "") or ""
    last_synced_commit = status.get("lastSyncedCommit", "") or ""
    raw_gc = entry.get("gitlabConfig", {}) or {}
    # Expose non-sensitive provisioning fields only (no token, no secret)
    gitlab_config_public = {
        k: raw_gc[k]
        for k in ("webhookId", "webhookUrl", "webhookInstalledAt", "webhookProvisionStatus", "webhookProvisionError")
        if k in raw_gc
    } if raw_gc else {}
    return {
        "id": entry["id"],
        "name": entry["name"],
        "sourceType": entry.get("sourceType", "local"),
        "active": entry.get("active", True),
        "registeredAt": entry.get("registeredAt", ""),
        "languages": entry.get("languages", []),
        "source": (
            raw_gc.get("projectUrl")
            if entry.get("sourceType") == "gitlab"
            else entry.get("sourceDir")
        ),
        "sourceDir": entry.get("sourceDir"),
        "branch": raw_gc.get("branch"),
        "webhookPath": webhook_path_for_workspace(entry["id"]) if entry.get("sourceType") == "gitlab" else "",
        "gitlabConfig": gitlab_config_public,
        "status": status,
        "lastSync": (
            {
                "status": status.get("lastSyncStatus", ""),
                "completedAt": status.get("lastSyncCompletedAt"),
                "commit": last_synced_commit,
            }
            if status.get("lastSyncStatus") or status.get("lastSyncCompletedAt") or last_synced_commit
            else None
        ),
        "isCurrent": bool(current_clone_commit and current_clone_commit == last_synced_commit),
        "drifted": bool(current_clone_commit and last_synced_commit and current_clone_commit != last_synced_commit),
    }


def list_workspace_summaries(
    *,
    entries: list[dict[str, Any]],
    jobs: Iterable[JobLike],
) -> list[dict[str, Any]]:
    jobs_by_workspace = {}
    for job in jobs:
        jobs_by_workspace.setdefault(job.workspace_id, job)

    result = []
    for entry in entries:
        last_job = jobs_by_workspace.get(entry["id"])
        summary = enrich_workspace_summary(entry)
        if summary["lastSync"] is None and last_job:
            summary["lastSync"] = {"status": last_job.status, "completedAt": last_job.completed_at}
        result.append(summary)
    return result


# TODO: multi-branch support — currently one workspace = one branch. To query
# across branches of the same repo (e.g. feature branch vs master), a user must
# register separate workspaces. A proper solution would let a single workspace
# track multiple branches, with notes namespaced by branch and retrieve_context
# accepting an optional branch filter. Webhook routing and sync jobs would need
# to fan out per-branch on push events.
async def register_workspace(
    *,
    payload: dict[str, Any],
    data_dir: Path,
    workspace_repository: WorkspaceRepository,
    git_client: GitClient,
    mirror_service: MirrorService,
) -> RegisterWorkspaceResult:
    name = str(payload.get("name", "")).strip()
    gitlab_url = payload.get("gitlab_url")
    gitlab_branch = str(payload.get("gitlab_branch") or "main")
    gitlab_token = payload.get("gitlab_token")
    ssh_key_file = payload.get("ssh_key_file") or None
    source_dir = payload.get("source_dir")
    source_type = payload.get("source_type")
    languages = payload.get("languages") or []
    omit_patterns = payload.get("omit_patterns")

    _validate_name(name)
    _validate_languages(languages)

    existing = workspace_repository.list_all()
    if any(entry.get("name") == name for entry in existing):
        raise ValueError(f'Workspace "{name}" already exists')

    if not gitlab_url and not source_dir:
        raise ValueError("Provide either gitlab_url or source_dir.")

    if gitlab_url and source_dir:
        # Keep TS semantics: gitlab_url takes precedence.
        source_dir = None

    if gitlab_url:
        clone_dir = str((data_dir / ".context-garden" / "clones" / name).resolve())
        Path(clone_dir).parent.mkdir(parents=True, exist_ok=True)
        webhook_secret = secrets.token_hex(32)
        token = gitlab_token or _resolve_token_from_env()
        await git_client.clone_repo(
            project_url=str(gitlab_url),
            clone_dir=clone_dir,
            branch=gitlab_branch,
            token=token,
            ssh_key_file=ssh_key_file,
        )
        resolved_source_dir = clone_dir
        resolved_source_type = "gitlab"
        gitlab_config = {
            "projectUrl": str(gitlab_url),
            "branch": gitlab_branch,
            "accessToken": gitlab_token,
            "cloneDir": clone_dir,
            "webhookSecret": webhook_secret,
        }
    else:
        resolved_source_dir = str(Path(str(source_dir)).resolve())
        # Path existence is validated at boot time, not registration time —
        # allows registering paths that are mounted volumes or not yet present.
        resolved_source_type = "local" if not source_type else str(source_type)
        gitlab_config = None

    entry: dict[str, Any] = {
        "id": str(uuid.uuid4()),
        "name": name,
        "sourceDir": resolved_source_dir,
        "languages": list(dict.fromkeys(languages)),
        "active": True,
        "registeredAt": datetime.now(timezone.utc).isoformat(),
        "sourceType": resolved_source_type,
    }
    if omit_patterns:
        entry["omitPatterns"] = omit_patterns
    if ssh_key_file:
        entry["sshKeyFile"] = ssh_key_file
    if gitlab_config is not None:
        entry["gitlabConfig"] = gitlab_config
    entry["status"] = get_workspace_status(entry)
    if resolved_source_type == "gitlab":
        current_clone_commit = resolve_workspace_clone_commit(entry)
        entry["status"].update(
            {
                "currentCloneCommit": current_clone_commit,
                "lastSyncedCommit": current_clone_commit,
                "lastSyncStatus": "registered",
            }
        )

    updated = [*existing, entry]
    workspace_repository.save_all(updated)

    webhook_provision: dict[str, Any] | None = None
    if resolved_source_type == "gitlab":
        cg_host = _resolve_cg_host()
        token = gitlab_token or _resolve_token_from_env()
        webhook_provision = await _provision_gitlab_webhook(entry, cg_host, token)
        if webhook_provision:
            entry["gitlabConfig"].update(webhook_provision)
            workspace_repository.save_all([*[e for e in workspace_repository.list_all() if e["id"] != entry["id"]], entry])

    mirror_config = {
        "cloneDir": resolved_source_dir,
    }
    result = await mirror_service.run(workspace_entry=entry, gitlab_config=mirror_config)
    notes_generated = (
        sum(result.get("written", {}).values())
        if isinstance(result.get("written"), dict)
        else 0
    )
    note_paths = _collect_md_note_paths(
        data_dir=data_dir,
        workspace_name=name,
    )
    return RegisterWorkspaceResult(
        entry=entry,
        notes_generated=notes_generated,
        note_paths=note_paths if notes_generated > 0 else [],
        webhook_provision=webhook_provision,
    )


async def _provision_gitlab_webhook(
    entry: dict[str, Any],
    cg_host: str,
    token: str | None,
) -> dict[str, Any]:
    """Try to create/update the GitLab hook for this workspace. Always returns a dict of gitlabConfig fields to merge."""
    from ...infra.gitlab.project_hooks_client import (
        GitLabHookError,
        create_project_hook,
        list_project_hooks,
        parse_gitlab_api_base,
        update_project_hook,
    )

    workspace_id = entry["id"]
    gitlab_config = entry.get("gitlabConfig", {})
    project_url = gitlab_config.get("projectUrl", "")
    secret = gitlab_config.get("webhookSecret", "")
    hook_url = f"{cg_host.rstrip('/')}/webhooks/gitlab/{workspace_id}"

    if not token:
        return {
            "webhookProvisionStatus": "skipped",
            "webhookProvisionError": "no token available",
            "webhookUrl": hook_url,
        }

    try:
        parse_gitlab_api_base(project_url)
    except ValueError as exc:
        return {
            "webhookProvisionStatus": "skipped",
            "webhookProvisionError": str(exc),
            "webhookUrl": hook_url,
        }

    try:
        hooks = await list_project_hooks(project_url, token)
        existing = next((h for h in hooks if h.get("url") == hook_url), None)
        if existing:
            result = await update_project_hook(project_url, token, existing["id"], hook_url, secret)
        else:
            result = await create_project_hook(project_url, token, hook_url, secret)
        return {
            "webhookId": result.get("id"),
            "webhookUrl": hook_url,
            "webhookInstalledAt": datetime.now(timezone.utc).isoformat(),
            "webhookProvisionStatus": "installed",
            "webhookProvisionError": "",
        }
    except (GitLabHookError, Exception) as exc:
        log.warning("Webhook provisioning failed for workspace %s: %s", workspace_id, exc)
        return {
            "webhookProvisionStatus": "failed",
            "webhookProvisionError": str(exc),
            "webhookUrl": hook_url,
        }


async def repair_workspace_webhook(
    *,
    workspace_id: str,
    workspace_repository: WorkspaceRepository,
    cg_host: str | None = None,
) -> dict[str, Any]:
    entries = workspace_repository.list_all()
    entry = next((e for e in entries if e.get("id") == workspace_id), None)
    if entry is None:
        raise KeyError(f"Workspace {workspace_id!r} not found")
    if entry.get("sourceType") != "gitlab":
        raise ValueError("Webhook repair is only supported for GitLab workspaces")

    resolved_host = cg_host or _resolve_cg_host()
    token = entry.get("gitlabConfig", {}).get("accessToken") or _resolve_token_from_env()
    provision = await _provision_gitlab_webhook(entry, resolved_host, token)

    entry["gitlabConfig"].update(provision)
    workspace_repository.save_all(entries)

    hook_url = provision.get("webhookUrl", "")
    secret = entry["gitlabConfig"].get("webhookSecret", "")
    return {
        "status": provision.get("webhookProvisionStatus"),
        "webhookUrl": hook_url,
        "hookId": provision.get("webhookId"),
        "error": provision.get("webhookProvisionError") or None,
        "manualFallback": {
            "url": hook_url,
            "secret": secret,
            "events": ["push"],
        },
    }


def unregister_workspace(
    *,
    workspace_id: str,
    data_dir: Path,
    workspace_repository: WorkspaceRepository,
) -> UnregisterWorkspaceResult:
    entries = workspace_repository.list_all()
    idx = next((i for i, entry in enumerate(entries) if entry.get("id") == workspace_id), None)
    if idx is None:
        return UnregisterWorkspaceResult(removed=None, deleted_paths=[])

    entry = entries[idx]
    updated = [ws for ws in entries if ws.get("id") != workspace_id]
    workspace_repository.save_all(updated)

    deleted_paths = _collect_md_note_paths(data_dir=data_dir, workspace_name=entry["name"])

    mirror_dir = data_dir / "md_db" / "code" / entry["name"]
    shutil.rmtree(str(mirror_dir), ignore_errors=True)

    if entry.get("sourceType") == "gitlab":
        clone_dir = entry.get("gitlabConfig", {}).get("cloneDir")
        if clone_dir:
            shutil.rmtree(str(clone_dir), ignore_errors=True)

    return UnregisterWorkspaceResult(removed=entry, deleted_paths=deleted_paths)


def _validate_name(name: str) -> None:
    if len(name) < 2 or len(name) > 64:
        raise ValueError(f'Workspace name must be 2-64 characters, got "{name}"')
    if not NAME_RE.match(name):
        raise ValueError(
            "Workspace name must be lowercase alphanumeric + hyphens/underscores "
            '(no leading/trailing hyphen/underscore), got "{name}"'.format(name=name)
        )


def _validate_languages(languages: list[Any]) -> None:
    if not isinstance(languages, list):
        raise ValueError("languages must be a list.")
    if any(not isinstance(lang, str) or not lang for lang in languages):
        raise ValueError("Languages must be non-empty strings.")


def _resolve_cg_host() -> str:
    return os.environ.get("CONTEXT_GARDEN_HOST", _DEFAULT_CG_HOST).rstrip("/")


def _resolve_token_from_env() -> str | None:
    if os.environ.get("CG_GITLAB_TOKEN"):
        return os.environ["CG_GITLAB_TOKEN"]

    env_path = Path.home() / ".context-garden" / ".env"
    if not env_path.exists():
        return None

    for line in env_path.read_text("utf-8").splitlines():
        stripped = line.strip()
        if stripped.startswith("CG_GITLAB_TOKEN="):
            value = stripped[len("CG_GITLAB_TOKEN="):].strip()
            return value or None
    return None


def _collect_md_note_paths(*, data_dir: Path, workspace_name: str) -> list[str]:
    md_db_root = data_dir / "md_db"
    workspace_root = md_db_root / "code" / workspace_name
    if not workspace_root.exists():
        return []

    paths: list[str] = []
    for file_path in workspace_root.rglob("*.md"):
        try:
            rel = file_path.relative_to(md_db_root).as_posix()
        except ValueError:
            continue
        paths.append(rel)
    return paths
