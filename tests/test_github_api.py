"""Tests for GitHub API client."""

from __future__ import annotations

import json
import time
from unittest.mock import MagicMock

import httpx
import pytest

from fossier.config import Config
from fossier.db import Database
from fossier.github_api import GitHubAPI, RateLimitError


@pytest.fixture
def api(tmp_path):
    """GitHubAPI with a real DB but mocked HTTP client."""
    config = Config(
        repo_owner="testowner",
        repo_name="testrepo",
        github_token="test-token",
        db_path=str(tmp_path / "test.db"),
    )
    db = Database(config.db_path)
    db.connect()
    api = GitHubAPI(config, db)
    api._client = MagicMock()
    yield api
    db.close()


def _mock_response(status=200, json_data=None, headers=None):
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = status
    resp.json.return_value = json_data or {}
    resp.headers = headers or {}
    resp.text = json.dumps(json_data or {})
    return resp


class TestGet:
    def test_basic_get(self, api):
        api._client.get.return_value = _mock_response(
            json_data={"login": "testuser"}, headers={"etag": '"abc"'}
        )
        result = api.get("/users/testuser")
        assert result == {"login": "testuser"}

    def test_caches_response(self, api):
        api._client.get.return_value = _mock_response(
            json_data={"login": "testuser"}, headers={"etag": '"abc"'}
        )
        api.get("/users/testuser")
        # Second call should hit cache
        result = api.get("/users/testuser")
        assert result == {"login": "testuser"}
        assert api._client.get.call_count == 1

    def test_404_returns_none(self, api):
        api._client.get.return_value = _mock_response(status=404)
        assert api.get("/users/ghost") is None

    def test_server_error_returns_none(self, api):
        api._client.get.return_value = _mock_response(status=500, json_data={"message": "Internal Server Error"})
        assert api.get("/some/path") is None

    def test_http_error_returns_none(self, api):
        api._client.get.side_effect = httpx.ConnectError("Connection refused")
        assert api.get("/users/testuser") is None

    def test_rate_limit_tracking(self, api):
        api._client.get.return_value = _mock_response(
            json_data={},
            headers={"x-ratelimit-remaining": "4999", "x-ratelimit-reset": "9999999999"},
        )
        api.get("/test")
        assert api._rate_remaining["core"] == 4999

    def test_rate_limit_exhausted_raises(self, api):
        api._rate_remaining["core"] = 0
        api._rate_reset["core"] = time.time() + 60
        with pytest.raises(RateLimitError):
            api.get("/test")

    def test_etag_conditional_request(self, api):
        """After cache expires, should use etag for conditional request."""
        # First: populate cache
        api._client.get.return_value = _mock_response(
            json_data={"data": 1}, headers={"etag": '"etag123"'}
        )
        api.get("/test/path")

        # Expire the cache manually
        api.db.conn.execute(
            "UPDATE api_cache SET expires_at = '2000-01-01 00:00:00'"
        )
        api.db.conn.commit()

        # Second request should send If-None-Match and get 304
        api._client.get.return_value = _mock_response(status=304)
        result = api.get("/test/path")
        assert result == {"data": 1}

        # Verify If-None-Match was sent
        call_args = api._client.get.call_args
        assert call_args.kwargs.get("headers", {}).get("If-None-Match") == '"etag123"'


class TestPost:
    def test_basic_post(self, api):
        api._client.post.return_value = _mock_response(
            status=201, json_data={"id": 1}
        )
        result = api.post("/repos/o/r/issues/1/comments", {"body": "test"})
        assert result == {"id": 1}

    def test_post_error_returns_none(self, api):
        api._client.post.return_value = _mock_response(status=422)
        assert api.post("/test", {}) is None


class TestPatch:
    def test_basic_patch(self, api):
        api._client.patch.return_value = _mock_response(
            json_data={"state": "closed"}
        )
        result = api.patch("/repos/o/r/pulls/1", {"state": "closed"})
        assert result == {"state": "closed"}


class TestHelperMethods:
    def test_get_user(self, api):
        api._client.get.return_value = _mock_response(
            json_data={"login": "alice", "type": "User"}
        )
        user = api.get_user("alice")
        assert user["login"] == "alice"

    def test_get_collaborators_pagination(self, api):
        # First page: 100 entries
        page1 = [{"login": f"user{i}"} for i in range(100)]
        page2 = [{"login": f"user{i}"} for i in range(100, 110)]

        api._client.get.side_effect = [
            _mock_response(json_data=page1),
            _mock_response(json_data=page2),
        ]
        collabs = api.get_collaborators("o", "r")
        assert len(collabs) == 110

    def test_get_collaborators_single_page(self, api):
        api._client.get.return_value = _mock_response(
            json_data=[{"login": "alice"}, {"login": "bob"}]
        )
        collabs = api.get_collaborators("o", "r")
        assert collabs == ["alice", "bob"]

    def test_get_pr_files(self, api):
        api._client.get.return_value = _mock_response(
            json_data=[{"filename": "src/main.py", "additions": 10}]
        )
        files = api.get_pr_files("o", "r", 1)
        assert len(files) == 1

    def test_search_open_prs(self, api):
        api._client.get.return_value = _mock_response(
            json_data={"total_count": 5}
        )
        count = api.search_open_prs("alice")
        assert count == 5

    def test_search_prior_interaction(self, api):
        api._client.get.return_value = _mock_response(
            json_data={"total_count": 3}
        )
        assert api.search_prior_interaction("o", "r", "alice") is True

    def test_search_prior_interaction_none(self, api):
        api._client.get.return_value = _mock_response(
            json_data={"total_count": 0}
        )
        assert api.search_prior_interaction("o", "r", "alice") is False

    def test_post_comment(self, api):
        api._client.post.return_value = _mock_response(
            status=201, json_data={"id": 1}
        )
        result = api.post_comment("o", "r", 1, "test comment")
        assert result["id"] == 1

    def test_add_labels(self, api):
        api._client.post.return_value = _mock_response(
            status=200, json_data=[{"name": "spam"}]
        )
        result = api.add_labels("o", "r", 1, ["spam"])
        assert result is not None

    def test_close_pr(self, api):
        api._client.patch.return_value = _mock_response(
            json_data={"state": "closed"}
        )
        result = api.close_pr("o", "r", 1)
        assert result["state"] == "closed"

    def test_find_fossier_comment_found(self, api):
        api._client.get.return_value = _mock_response(
            json_data=[
                {"id": 100, "body": "some other comment"},
                {"id": 200, "body": "## Fossier: PR Auto-Closed\ndetails..."},
            ]
        )
        comment_id = api.find_fossier_comment("o", "r", 1)
        assert comment_id == 200

    def test_find_fossier_comment_not_found(self, api):
        api._client.get.return_value = _mock_response(
            json_data=[
                {"id": 100, "body": "just a regular comment"},
            ]
        )
        comment_id = api.find_fossier_comment("o", "r", 1)
        assert comment_id is None

    def test_post_or_update_comment_creates_new(self, api):
        # No existing fossier comment
        api._client.get.return_value = _mock_response(json_data=[])
        api._client.post.return_value = _mock_response(
            status=201, json_data={"id": 300}
        )
        result = api.post_or_update_comment("o", "r", 1, "## Fossier: test")
        assert result["id"] == 300

    def test_post_or_update_comment_updates_existing(self, api):
        # Existing fossier comment
        api._client.get.return_value = _mock_response(
            json_data=[{"id": 200, "body": "## Fossier: old content"}]
        )
        api._client.patch.return_value = _mock_response(
            json_data={"id": 200, "body": "## Fossier: updated"}
        )
        result = api.post_or_update_comment("o", "r", 1, "## Fossier: updated")
        api._client.patch.assert_called_once()
        assert result["id"] == 200

    def test_get_user_orgs(self, api):
        api._client.get.return_value = _mock_response(
            json_data=[{"login": "MyOrg"}, {"login": "AnotherOrg"}]
        )
        orgs = api.get_user_orgs("alice")
        assert orgs == ["myorg", "anotherorg"]

    def test_get_pr_commits(self, api):
        api._client.get.return_value = _mock_response(
            json_data=[
                {"sha": "abc", "commit": {"verification": {"verified": True}}},
            ]
        )
        commits = api.get_pr_commits("o", "r", 1)
        assert len(commits) == 1
        assert commits[0]["commit"]["verification"]["verified"] is True


class TestValidateToken:
    def test_valid_token(self, api):
        api._client.get.return_value = _mock_response(
            status=200,
            headers={"x-oauth-scopes": "public_repo, read:org"},
        )
        api.validate_token()  # Should not raise

    def test_invalid_token(self, api):
        api._client.get.return_value = _mock_response(status=401)
        api.validate_token()  # Should log warning, not raise

    def test_missing_scopes(self, api):
        api._client.get.return_value = _mock_response(
            status=200,
            headers={"x-oauth-scopes": ""},
        )
        api.validate_token()  # Should not raise
