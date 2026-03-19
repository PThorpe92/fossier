"""Tests for CLI commands."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from fossier.cli import main, EXIT_ALLOW, EXIT_DENY, EXIT_REVIEW, EXIT_ERROR


@pytest.fixture
def _patch_load_config(tmp_path):
    """Patch load_config to return a test config with tmp_path DB."""
    from fossier.config import Config

    config = Config(
        repo_owner="testowner",
        repo_name="testrepo",
        repo_root=tmp_path,
        db_path=str(tmp_path / "test.db"),
        github_token="test-token",
        dry_run=True,
    )
    with patch("fossier.cli.load_config", return_value=config) as mock:
        mock._config = config
        yield mock


@pytest.fixture
def _patch_api():
    """Patch GitHubAPI to avoid real HTTP calls."""
    with patch("fossier.cli.GitHubAPI") as mock_cls:
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
        mock_cls.return_value = api
        yield api


class TestCheckCommand:
    def test_check_unknown_user(self, _patch_load_config, _patch_api, capsys):
        result = main(["check", "newuser", "--repo", "owner/repo"])
        assert result in (EXIT_ALLOW, EXIT_DENY, EXIT_REVIEW)
        captured = capsys.readouterr()
        assert "newuser" in captured.out.lower()

    def test_check_blocked_user(self, _patch_load_config, _patch_api, tmp_path, capsys):
        _patch_load_config._config.blocked_users = {"blockeduser"}
        result = main(["check", "blockeduser"])
        assert result == EXIT_DENY
        captured = capsys.readouterr()
        assert "DENY" in captured.out

    def test_check_trusted_user(self, _patch_load_config, _patch_api, capsys):
        _patch_load_config._config.trusted_users = {"trusteduser"}
        result = main(["check", "trusteduser"])
        assert result == EXIT_ALLOW
        captured = capsys.readouterr()
        assert "ALLOW" in captured.out

    def test_check_json_output(self, _patch_load_config, _patch_api, capsys):
        _patch_load_config._config.trusted_users = {"alice"}
        _patch_load_config._config.output_format = "json"
        result = main(["check", "alice", "--format", "json"])
        assert result == EXIT_ALLOW
        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert data["outcome"] == "allow"
        assert data["username"] == "alice"

    def test_check_table_output(self, _patch_load_config, _patch_api, capsys):
        _patch_load_config._config.trusted_users = {"alice"}
        _patch_load_config._config.output_format = "table"
        result = main(["check", "alice", "--format", "table"])
        assert result == EXIT_ALLOW


class TestScoreCommand:
    def test_score_text(self, _patch_load_config, _patch_api, capsys):
        result = main(["score", "someuser"])
        captured = capsys.readouterr()
        assert "Score for" in captured.out or "Outcome:" in captured.out
        assert result in (EXIT_ALLOW, EXIT_DENY, EXIT_REVIEW)

    def test_score_json(self, _patch_load_config, _patch_api, capsys):
        _patch_load_config._config.output_format = "json"
        result = main(["score", "someuser", "--format", "json"])
        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert "total_score" in data
        assert "confidence" in data
        assert "signals" in data


class TestTierCommand:
    def test_tier_unknown(self, _patch_load_config, _patch_api, capsys):
        result = main(["tier", "newuser"])
        assert result == EXIT_ALLOW
        captured = capsys.readouterr()
        assert "unknown" in captured.out.lower()

    def test_tier_trusted(self, _patch_load_config, _patch_api, capsys):
        _patch_load_config._config.trusted_users = {"trusteduser"}
        result = main(["tier", "trusteduser"])
        assert result == EXIT_ALLOW
        captured = capsys.readouterr()
        assert "trusted" in captured.out.lower()

    def test_tier_json(self, _patch_load_config, _patch_api, capsys):
        _patch_load_config._config.output_format = "json"
        result = main(["tier", "newuser", "--format", "json"])
        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert "tier" in data
        assert "username" in data


class TestHistoryCommand:
    def test_history_empty(self, _patch_load_config, capsys):
        result = main(["history", "nobody"])
        assert result == EXIT_ALLOW
        captured = capsys.readouterr()
        assert "No history" in captured.out

    def test_history_json_empty(self, _patch_load_config, capsys):
        result = main(["history", "nobody"])
        assert result == EXIT_ALLOW


class TestVouchDenouce:
    def test_vouch(self, _patch_load_config, capsys, tmp_path):
        result = main(["vouch", "gooduser"])
        assert result == EXIT_ALLOW
        captured = capsys.readouterr()
        assert "Vouched" in captured.out
        assert "gooduser" in captured.out

    def test_denounce(self, _patch_load_config, capsys, tmp_path):
        result = main(["denounce", "baduser", "--reason", "spammer"])
        assert result == EXIT_ALLOW
        captured = capsys.readouterr()
        assert "Denounced" in captured.out


class TestDbCommands:
    def test_db_migrate(self, _patch_load_config, capsys):
        result = main(["db", "migrate"])
        assert result == EXIT_ALLOW
        captured = capsys.readouterr()
        assert "migrations complete" in captured.out.lower()

    def test_db_stats(self, _patch_load_config, capsys):
        result = main(["db", "stats"])
        assert result == EXIT_ALLOW
        captured = capsys.readouterr()
        assert "Contributors by tier" in captured.out

    def test_db_stats_json(self, _patch_load_config, capsys):
        _patch_load_config._config.output_format = "json"
        result = main(["db", "stats", "--format", "json"])
        assert result == EXIT_ALLOW
        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert "known" in data

    def test_db_prune(self, _patch_load_config, capsys):
        result = main(["db", "prune"])
        assert result == EXIT_ALLOW
        captured = capsys.readouterr()
        assert "Pruned" in captured.out


class TestInitCommand:
    def test_init_creates_files(self, _patch_load_config, capsys, tmp_path):
        result = main(["init"])
        assert result == EXIT_ALLOW
        captured = capsys.readouterr()
        assert "Created" in captured.out
        assert (tmp_path / "fossier.toml").exists()
        assert (tmp_path / "VOUCHED.td").exists()

    def test_init_skips_existing(self, _patch_load_config, capsys, tmp_path):
        (tmp_path / "fossier.toml").write_text("[repo]\n")
        (tmp_path / "VOUCHED.td").write_text("# empty\n")
        result = main(["init"])
        assert result == EXIT_ALLOW
        captured = capsys.readouterr()
        assert "already exists" in captured.out


class TestNoSubcommand:
    def test_no_args(self, capsys):
        result = main([])
        assert result == EXIT_ERROR
