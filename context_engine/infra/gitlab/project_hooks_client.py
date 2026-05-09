"""GitLab project hooks REST client (v4 API)."""

from __future__ import annotations

import re
from urllib.parse import quote


class GitLabHookError(Exception):
    def __init__(self, status_code: int, body: str) -> None:
        super().__init__(f"GitLab API {status_code}: {body}")
        self.status_code = status_code
        self.body = body


_NON_GITLAB_HOSTS = {"github.com", "bitbucket.org", "dev.azure.com"}


def parse_gitlab_api_base(project_url: str) -> tuple[str, str]:
    """Return (api_base, url_encoded_project_path) for a GitLab project URL.

    Accepts HTTPS:  https://gitlab.home.lab/user/repo[.git]
    Accepts SSH:    git@gitlab.home.lab:user/repo[.git]
    Raises ValueError for unrecognised formats or known non-GitLab hosts.
    """
    project_url = project_url.strip()

    # SSH form: git@host:path/to/repo[.git]
    ssh_match = re.match(r"^git@([^:]+):(.+?)(?:\.git)?$", project_url)
    if ssh_match:
        host = ssh_match.group(1)
        if host in _NON_GITLAB_HOSTS:
            raise ValueError(f"Host {host!r} is not a supported GitLab instance")
        path = ssh_match.group(2)
        api_base = f"https://{host}/api/v4"
        encoded = quote(path, safe="")
        return api_base, encoded

    # HTTPS form
    https_match = re.match(r"^https?://([^/:]+)(?::\d+)?/(.+?)(?:\.git)?$", project_url)
    if https_match:
        host = https_match.group(1)
        if host in _NON_GITLAB_HOSTS:
            raise ValueError(f"Host {host!r} is not a supported GitLab instance")
        path = https_match.group(2)
        api_base = f"https://{host}/api/v4"
        encoded = quote(path, safe="")
        return api_base, encoded

    raise ValueError(f"Unsupported GitLab project URL format: {project_url!r}")


async def list_project_hooks(project_url: str, token: str) -> list[dict]:
    import httpx
    api_base, encoded_path = parse_gitlab_api_base(project_url)
    url = f"{api_base}/projects/{encoded_path}/hooks"
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(url, headers={"PRIVATE-TOKEN": token})
    if resp.status_code != 200:
        raise GitLabHookError(resp.status_code, resp.text)
    return resp.json()


async def create_project_hook(
    project_url: str,
    token: str,
    hook_url: str,
    secret: str,
    push_events: bool = True,
) -> dict:
    import httpx
    api_base, encoded_path = parse_gitlab_api_base(project_url)
    url = f"{api_base}/projects/{encoded_path}/hooks"
    payload = {"url": hook_url, "token": secret, "push_events": push_events}
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(url, json=payload, headers={"PRIVATE-TOKEN": token})
    if resp.status_code not in (200, 201):
        raise GitLabHookError(resp.status_code, resp.text)
    return resp.json()


async def update_project_hook(
    project_url: str,
    token: str,
    hook_id: int,
    hook_url: str,
    secret: str,
    push_events: bool = True,
) -> dict:
    import httpx
    api_base, encoded_path = parse_gitlab_api_base(project_url)
    url = f"{api_base}/projects/{encoded_path}/hooks/{hook_id}"
    payload = {"url": hook_url, "token": secret, "push_events": push_events}
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.put(url, json=payload, headers={"PRIVATE-TOKEN": token})
    if resp.status_code != 200:
        raise GitLabHookError(resp.status_code, resp.text)
    return resp.json()
