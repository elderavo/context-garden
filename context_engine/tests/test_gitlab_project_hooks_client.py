from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from context_engine.infra.gitlab.project_hooks_client import (
    GitLabHookError,
    create_project_hook,
    list_project_hooks,
    parse_gitlab_api_base,
    update_project_hook,
)


class ParseGitlabApiBaseTests(unittest.TestCase):
    def test_https_url(self):
        api_base, encoded = parse_gitlab_api_base("https://gitlab.home.lab/user/repo")
        self.assertEqual(api_base, "https://gitlab.home.lab/api/v4")
        self.assertEqual(encoded, "user%2Frepo")

    def test_https_url_with_git_suffix(self):
        api_base, encoded = parse_gitlab_api_base("https://gitlab.home.lab/user/repo.git")
        self.assertEqual(api_base, "https://gitlab.home.lab/api/v4")
        self.assertEqual(encoded, "user%2Frepo")

    def test_ssh_url(self):
        api_base, encoded = parse_gitlab_api_base("git@gitlab.home.lab:user/repo.git")
        self.assertEqual(api_base, "https://gitlab.home.lab/api/v4")
        self.assertEqual(encoded, "user%2Frepo")

    def test_ssh_url_nested_path(self):
        api_base, encoded = parse_gitlab_api_base("git@gitlab.home.lab:group/sub/repo.git")
        self.assertEqual(api_base, "https://gitlab.home.lab/api/v4")
        self.assertEqual(encoded, "group%2Fsub%2Frepo")

    def test_unsupported_url_raises(self):
        with self.assertRaises(ValueError):
            parse_gitlab_api_base("https://github.com/user/repo")

    def test_invalid_url_raises(self):
        with self.assertRaises(ValueError):
            parse_gitlab_api_base("not-a-url")


def _mock_response(status_code: int, json_data):
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = json_data
    resp.text = str(json_data)
    return resp


class ListProjectHooksTests(unittest.IsolatedAsyncioTestCase):
    async def test_success(self):
        hooks = [{"id": 1, "url": "http://cg/webhooks/gitlab/abc"}]
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.get = AsyncMock(return_value=_mock_response(200, hooks))

        with patch("httpx.AsyncClient", return_value=mock_client):
            result = await list_project_hooks("https://gitlab.home.lab/user/repo", "token123")

        self.assertEqual(result, hooks)

    async def test_non_200_raises(self):
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.get = AsyncMock(return_value=_mock_response(401, {"message": "Unauthorized"}))

        with patch("httpx.AsyncClient", return_value=mock_client):
            with self.assertRaises(GitLabHookError) as ctx:
                await list_project_hooks("https://gitlab.home.lab/user/repo", "bad-token")

        self.assertEqual(ctx.exception.status_code, 401)


class CreateProjectHookTests(unittest.IsolatedAsyncioTestCase):
    async def test_success(self):
        created = {"id": 42, "url": "http://cg/webhooks/gitlab/ws1"}
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = AsyncMock(return_value=_mock_response(201, created))

        with patch("httpx.AsyncClient", return_value=mock_client):
            result = await create_project_hook(
                "https://gitlab.home.lab/user/repo",
                "token123",
                "http://cg/webhooks/gitlab/ws1",
                "secret",
            )

        self.assertEqual(result["id"], 42)

    async def test_non_2xx_raises(self):
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.post = AsyncMock(return_value=_mock_response(403, {"message": "Forbidden"}))

        with patch("httpx.AsyncClient", return_value=mock_client):
            with self.assertRaises(GitLabHookError):
                await create_project_hook(
                    "https://gitlab.home.lab/user/repo", "token", "http://url", "secret"
                )


class UpdateProjectHookTests(unittest.IsolatedAsyncioTestCase):
    async def test_success(self):
        updated = {"id": 42, "url": "http://cg/webhooks/gitlab/ws1"}
        mock_client = AsyncMock()
        mock_client.__aenter__ = AsyncMock(return_value=mock_client)
        mock_client.__aexit__ = AsyncMock(return_value=False)
        mock_client.put = AsyncMock(return_value=_mock_response(200, updated))

        with patch("httpx.AsyncClient", return_value=mock_client):
            result = await update_project_hook(
                "https://gitlab.home.lab/user/repo",
                "token123",
                42,
                "http://cg/webhooks/gitlab/ws1",
                "secret",
            )

        self.assertEqual(result["id"], 42)


if __name__ == "__main__":
    unittest.main()
