"""Tests for config loading."""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch


from fossier.config import Config, load_config, _normalize_weights


def test_default_config():
    config = Config()
    assert config.repo_owner == ""
    assert config.repo_name == ""
    assert config.db_path == ".fossier.db"
    assert config.thresholds.allow_score == 70.0
    assert config.thresholds.deny_score == 40.0
    assert config.thresholds.min_confidence == 0.5
    assert config.dry_run is False
    assert config.output_format == "text"
    assert config.bot_policy == "score"


def test_load_config_from_toml(tmp_path):
    toml_content = """
[repo]
owner = "myorg"
name = "myrepo"
db_path = "custom.db"

[thresholds]
allow_score = 80.0
deny_score = 30.0

[weights]
account_age = 0.5
public_repos = 0.5

[actions.deny]
close_pr = false
label = "spam"

[actions.review]
comment = false
label = "review-needed"

[cache_ttl]
user_profile_hours = 48
search_hours = 2
collaborators_hours = 12

[trust]
trusted_users = ["Alice", "Bob"]
blocked_users = ["Spammer"]
bot_policy = "allow"
"""
    toml_path = tmp_path / "fossier.toml"
    toml_path.write_text(toml_content)

    with patch("fossier.config._detect_git_root", return_value=tmp_path):
        config = load_config(repo_root=tmp_path)

    assert config.repo_owner == "myorg"
    assert config.repo_name == "myrepo"
    assert config.thresholds.allow_score == 80.0
    assert config.thresholds.deny_score == 30.0
    assert config.deny_action.close_pr is False
    assert config.deny_action.label == "spam"
    assert config.review_action.comment is False
    assert config.cache_ttl.user_profile_hours == 48
    assert "alice" in config.trusted_users
    assert "bob" in config.trusted_users
    assert "spammer" in config.blocked_users
    assert config.bot_policy == "allow"


def test_load_config_github_dir(tmp_path):
    github_dir = tmp_path / ".github"
    github_dir.mkdir()
    (github_dir / "fossier.toml").write_text('[repo]\nowner = "ghorg"\nname = "ghrepo"\n')

    with patch("fossier.config._detect_git_root", return_value=tmp_path):
        config = load_config(repo_root=tmp_path)

    assert config.repo_owner == "ghorg"
    assert config.repo_name == "ghrepo"


def test_env_overrides(tmp_path):
    env = {
        "GITHUB_TOKEN": "env-token-123",
        "GITHUB_REPOSITORY": "envowner/envrepo",
    }
    with (
        patch("fossier.config._detect_git_root", return_value=tmp_path),
        patch.dict(os.environ, env),
    ):
        config = load_config(repo_root=tmp_path)

    assert config.github_token == "env-token-123"
    assert config.repo_owner == "envowner"
    assert config.repo_name == "envrepo"


def test_env_gh_token(tmp_path):
    env = {"GH_TOKEN": "gh-token-456"}
    cleaned = {k: v for k, v in os.environ.items() if k not in ("GITHUB_TOKEN", "GH_TOKEN", "GITHUB_REPOSITORY")}
    cleaned.update(env)
    with (
        patch("fossier.config._detect_git_root", return_value=tmp_path),
        patch.dict(os.environ, cleaned, clear=True),
    ):
        config = load_config(repo_root=tmp_path)

    assert config.github_token == "gh-token-456"


def test_cli_overrides(tmp_path):
    with patch("fossier.config._detect_git_root", return_value=tmp_path):
        config = load_config(
            repo_root=tmp_path,
            cli_overrides={
                "repo": "cliowner/clirepo",
                "verbose": True,
                "dry_run": True,
                "format": "json",
                "db_path": "/custom/path.db",
            },
        )

    assert config.repo_owner == "cliowner"
    assert config.repo_name == "clirepo"
    assert config.verbose is True
    assert config.dry_run is True
    assert config.output_format == "json"
    assert config.db_path == "/custom/path.db"


def test_toml_overridden_by_env(tmp_path):
    """Env vars should take precedence over TOML for token."""
    (tmp_path / "fossier.toml").write_text('[repo]\nowner = "tomlowner"\nname = "tomlrepo"\n')

    env = {"GITHUB_TOKEN": "env-token"}
    with (
        patch("fossier.config._detect_git_root", return_value=tmp_path),
        patch.dict(os.environ, env),
    ):
        config = load_config(repo_root=tmp_path)

    assert config.github_token == "env-token"
    assert config.repo_owner == "tomlowner"  # TOML not overridden by env since it was set


def test_normalize_weights():
    config = Config()
    config.signal_weights = {"a": 2.0, "b": 3.0}
    _normalize_weights(config)
    total = sum(config.signal_weights.values())
    assert abs(total - 1.0) < 0.01


def test_normalize_weights_already_normalized():
    config = Config()
    config.signal_weights = {"a": 0.5, "b": 0.5}
    _normalize_weights(config)
    assert config.signal_weights["a"] == 0.5
    assert config.signal_weights["b"] == 0.5


def test_git_root_detection(tmp_path):
    """If no git root, should still work with default path."""
    with patch("fossier.config._detect_git_root", return_value=None):
        config = load_config()
    assert config.repo_root == Path(".")
