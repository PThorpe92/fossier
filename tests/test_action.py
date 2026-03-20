"""Tests for GitHub Action entrypoint."""

from __future__ import annotations

import json
import os
from unittest.mock import MagicMock, patch

import pytest

from fossier.action import GithubAction


def action_main() -> int:
    action = GithubAction()
    return action.run()


@pytest.fixture
def event_file(tmp_path):
    """Create a PR event payload file."""

    def _make(payload: dict) -> str:
        path = tmp_path / "event.json"
        path.write_text(json.dumps(payload))
        return str(path)

    return _make


@pytest.fixture
def pr_event(event_file):
    """Standard PR opened event."""
    return event_file(
        {
            "pull_request": {
                "number": 42,
                "user": {"login": "testuser"},
                "title": "Add feature",
                "body": "Description here",
            }
        }
    )


@pytest.fixture
def _patch_for_action(tmp_path):
    """Patch config and API for action tests."""
    from fossier.config import Config

    config = Config(
        repo_owner="testowner",
        repo_name="testrepo",
        repo_root=tmp_path,
        db_path=str(tmp_path / "test.db"),
        github_token="test-token",
        dry_run=True,
    )

    api = MagicMock()
    api.get_collaborators.return_value = []
    api.get_user.return_value = {
        "created_at": "2020-01-01T00:00:00Z",
        "public_repos": 10,
        "public_gists": 2,
        "followers": 5,
        "following": 10,
        "type": "User",
        "email": "user@example.com",
    }
    api.search_open_prs.return_value = 2
    api.search_prior_interaction.return_value = False
    api.get_pr_files.return_value = []
    api.get_user_orgs.return_value = []
    api.get_pr.return_value = None
    api.get_repo.return_value = {"stargazers_count": 50}
    api.get_pr_commits.return_value = []

    with (
        patch("fossier.action.load_config", return_value=config),
        patch("fossier.action.GitHubAPI", return_value=api),
    ):
        yield config, api


def test_no_event_path():
    """Should fail with exit code 3 if GITHUB_EVENT_PATH not set."""
    env = {k: v for k, v in os.environ.items() if k != "GITHUB_EVENT_PATH"}
    with patch.dict(os.environ, env, clear=True):
        result = action_main()
    assert result == 3


def test_invalid_event(event_file):
    """Should fail with exit code 3 for non-PR event."""
    path = event_file({"action": "created"})
    with patch.dict(os.environ, {"GITHUB_EVENT_PATH": path}):
        result = action_main()
    assert result == 3


def test_pr_event_runs_pipeline(pr_event, _patch_for_action, tmp_path):
    """Should run full pipeline and return outcome code."""
    output_file = str(tmp_path / "github_output")
    with open(output_file, "w"):
        pass

    env = {
        "GITHUB_EVENT_PATH": pr_event,
        "GITHUB_OUTPUT": output_file,
    }
    with patch.dict(os.environ, env):
        result = action_main()

    assert result in (0, 1, 2)  # ALLOW, DENY, or REVIEW

    # Check that outputs were written
    with open(output_file) as f:
        output_content = f.read()
    assert "outcome=" in output_content
    assert "tier=" in output_content


def test_pr_event_trusted_user(pr_event, _patch_for_action, tmp_path):
    """Trusted user should get ALLOW (exit code 0)."""
    config, api = _patch_for_action
    config.trusted_users = {"testuser"}

    output_file = str(tmp_path / "github_output")
    with open(output_file, "w"):
        pass

    env = {
        "GITHUB_EVENT_PATH": pr_event,
        "GITHUB_OUTPUT": output_file,
    }
    with patch.dict(os.environ, env):
        result = action_main()

    assert result == 0


def test_pr_event_blocked_user(pr_event, _patch_for_action, tmp_path):
    """Blocked user should get DENY (exit code 1)."""
    config, api = _patch_for_action
    config.blocked_users = {"testuser"}

    output_file = str(tmp_path / "github_output")
    with open(output_file, "w"):
        pass

    env = {
        "GITHUB_EVENT_PATH": pr_event,
        "GITHUB_OUTPUT": output_file,
    }
    with patch.dict(os.environ, env):
        result = action_main()

    assert result == 1
