"""Workspace app service: register/list/unregister ownership in Python."""

from __future__ import annotations

import os
import re
import secrets
import shutil
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Protocol

from ..ports.git_client import GitClient
from ..ports.mirror_service import MirrorService
from ..ports.workspace_repository import WorkspaceRepository

NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}[a-z0-9]$")


class JobLike(Protocol):
    workspace_id: str
    status: str
    completed_at: str | None


@dataclass(frozen=True)
class RegisterWorkspaceResult:
    entry: dict[str, Any]
    notes_generated: int
    note_paths: list[str]


@dataclass(frozen=True)
class UnregisterWorkspaceResult:
    removed: dict[str, Any] | None
    deleted_paths: list[str]


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
        result.append(
            {
                "id": entry["id"],
                "name": entry["name"],
                "sourceType": entry.get("sourceType", "local"),
                "active": entry.get("active", True),
                "registeredAt": entry.get("registeredAt", ""),
                "languages": entry.get("languages", []),
                "source": (
                    entry.get("gitlabConfig", {}).get("projectUrl")
                    if entry.get("sourceType") == "gitlab"
                    else entry.get("sourceDir")
                ),
                "sourceDir": entry.get("sourceDir"),
                "branch": entry.get("gitlabConfig", {}).get("branch"),
                "lastSync": (
                    {"status": last_job.status, "completedAt": last_job.completed_at}
                    if last_job
                    else None
                ),
            }
        )
    return result


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

    updated = [*existing, entry]
    workspace_repository.save_all(updated)

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
    )


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

