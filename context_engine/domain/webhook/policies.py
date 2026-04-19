from __future__ import annotations


def expected_branch_ref(branch: str) -> str:
    return f"refs/heads/{branch}"


def should_ignore_ref(*, payload_ref: str | None, tracked_branch: str) -> bool:
    # Preserve legacy behavior: missing ref does not force ignore.
    if not payload_ref:
        return False
    return payload_ref != expected_branch_ref(tracked_branch)


def build_idempotency_key(
    *,
    workspace_id: str,
    delivery_id: str | None,
    payload_ref: str | None,
    commit_sha: str | None,
) -> str:
    if delivery_id:
        return f"delivery:{workspace_id}:{delivery_id}"
    return f"push:{workspace_id}:{payload_ref or '-'}:{commit_sha or '-'}"

