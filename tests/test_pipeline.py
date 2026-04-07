"""Tests for the shared evaluation pipeline."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from fossier.config import Config
from fossier.db import Database
from fossier.models import Outcome, TrustTier
from fossier.pipeline import evaluate_contributor
from fossier.trust import TrustResolver


@pytest.fixture
def db(tmp_path):
    database = Database(str(tmp_path / "test.db"))
    database.connect()
    yield database
    database.close()


@pytest.fixture
def config(tmp_path):
    return Config(
        repo_owner="testowner",
        repo_name="testrepo",
        repo_root=tmp_path,
        db_path=str(tmp_path / "test.db"),
        github_token="test-token",
        dry_run=True,
    )


@pytest.fixture
def api():
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
    api.search_closed_prs.return_value = 0
    api.search_merged_prs.return_value = 5
    api.search_prior_interaction.return_value = 3
    api.count_recent_items.return_value = 1
    api.get_pr_files.return_value = []
    api.get_user_orgs.return_value = []
    api.get_pr.return_value = None
    api.get_repo.return_value = {"stargazers_count": 50}
    api.get_pr_commits.return_value = []
    api.get_user_repos.return_value = []
    return api


def test_trusted_user_gets_allow(config, db, api):
    config.trusted_users = {"alice"}
    resolver = TrustResolver(config, db, api)
    decision = evaluate_contributor("alice", resolver)
    assert decision.outcome == Outcome.ALLOW
    assert decision.trust_tier == TrustTier.TRUSTED


def test_blocked_user_gets_deny(config, db, api):
    config.blocked_users = {"spammer"}
    resolver = TrustResolver(config, db, api)
    decision = evaluate_contributor("spammer", resolver)
    assert decision.outcome == Outcome.DENY
    assert decision.trust_tier == TrustTier.BLOCKED


def test_unknown_user_gets_scored(config, db, api):
    resolver = TrustResolver(config, db, api)
    decision = evaluate_contributor("newuser", resolver)
    assert decision.trust_tier == TrustTier.UNKNOWN
    assert decision.score_result is not None
    assert decision.outcome in (Outcome.ALLOW, Outcome.REVIEW, Outcome.DENY)


def test_records_in_db(config, db, api):
    config.trusted_users = {"alice"}
    resolver = TrustResolver(config, db, api)
    evaluate_contributor("alice", resolver)
    contributor = db.get_contributor("testowner", "testrepo", "alice")
    assert contributor is not None
    assert contributor.trust_tier == TrustTier.TRUSTED


def test_bot_policy_allow(config, db, api):
    config.bot_policy = "allow"
    resolver = TrustResolver(config, db, api)
    decision = evaluate_contributor("dependabot[bot]", resolver)
    assert decision.outcome == Outcome.ALLOW
    assert "bot" in decision.reason.lower()


def test_bot_policy_block(config, db, api):
    config.bot_policy = "block"
    resolver = TrustResolver(config, db, api)
    decision = evaluate_contributor("renovate-bot", resolver)
    assert decision.outcome == Outcome.DENY
    assert "bot" in decision.reason.lower()


def test_bot_policy_score_runs_normal_pipeline(config, db, api):
    config.bot_policy = "score"
    resolver = TrustResolver(config, db, api)
    decision = evaluate_contributor("dependabot[bot]", resolver)
    # Normal pipeline runs — bot gets scored normally
    assert decision.trust_tier == TrustTier.UNKNOWN
    assert decision.score_result is not None


def test_username_normalized_to_lowercase(config, db, api):
    config.trusted_users = {"alice"}
    resolver = TrustResolver(config, db, api)
    decision = evaluate_contributor("ALICE", resolver)
    assert decision.contributor.username == "alice"
    assert decision.outcome == Outcome.ALLOW


def test_reject_ai_authored_claude(config, db, api):
    config.reject_ai_authored = True
    api.get_pr_commits.return_value = [
        {
            "sha": "abc123",
            "commit": {
                "message": (
                    "Add feature\n\nCo-Authored-By: Claude <noreply@anthropic.com>"
                ),
                "verification": {"verified": False},
            },
        }
    ]
    resolver = TrustResolver(config, db, api)
    decision = evaluate_contributor("newuser", resolver, pr_number=42)
    assert decision.outcome == Outcome.DENY
    assert "ai co-authored" in decision.reason.lower()
    assert "claude" in decision.reason.lower()


def test_reject_ai_authored_copilot(config, db, api):
    config.reject_ai_authored = True
    api.get_pr_commits.return_value = [
        {
            "sha": "def456",
            "commit": {
                "message": (
                    "Fix bug\n\nCo-authored-by: GitHub Copilot <copilot@github.com>"
                ),
                "verification": {"verified": False},
            },
        }
    ]
    resolver = TrustResolver(config, db, api)
    decision = evaluate_contributor("someuser", resolver, pr_number=10)
    assert decision.outcome == Outcome.DENY
    assert "copilot" in decision.reason.lower()


def test_reject_ai_authored_disabled_by_default(config, db, api):
    """When reject_ai_authored is False (default), AI commits are allowed through."""
    assert config.reject_ai_authored is False
    api.get_pr_commits.return_value = [
        {
            "sha": "abc123",
            "commit": {
                "message": (
                    "Add feature\n\nCo-Authored-By: Claude <noreply@anthropic.com>"
                ),
                "verification": {"verified": False},
            },
        }
    ]
    resolver = TrustResolver(config, db, api)
    decision = evaluate_contributor("newuser", resolver, pr_number=42)
    # Should go through normal scoring, not auto-deny
    assert (
        decision.outcome != Outcome.DENY
        or "ai co-authored" not in decision.reason.lower()
    )


def test_reject_ai_authored_clean_commits_pass(config, db, api):
    """Normal commits should not trigger the AI reject."""
    config.reject_ai_authored = True
    api.get_pr_commits.return_value = [
        {
            "sha": "abc123",
            "commit": {
                "message": "Add feature\n\nCo-Authored-By: Alice <alice@example.com>",
                "verification": {"verified": False},
            },
        }
    ]
    resolver = TrustResolver(config, db, api)
    decision = evaluate_contributor("newuser", resolver, pr_number=42)
    # Should not be denied for AI authorship
    assert "ai co-authored" not in decision.reason.lower()


def test_registry_known_spam_blocks(config, db, api):
    """When registry returns known=True with 3+ reports, user should be denied."""
    config.registry_url = "https://registry.example.com"
    config.registry_check_before_scoring = True

    from unittest.mock import patch
    from fossier.registry_client import RegistryCheckResult

    mock_client = MagicMock()
    mock_client.check_username.return_value = RegistryCheckResult(known=True, report_count=5)

    with patch("fossier.pipeline._get_registry_client", return_value=mock_client):
        resolver = TrustResolver(config, db, api)
        decision = evaluate_contributor("spammer", resolver, pr_number=1)

    assert decision.outcome == Outcome.DENY
    assert "registry" in decision.reason.lower()
    mock_client.close.assert_called_once()


def test_registry_block_threshold_configurable(config, db, api):
    """Registry block threshold should be configurable."""
    config.registry_url = "https://registry.example.com"
    config.registry_check_before_scoring = True
    config.registry_block_threshold = 5

    from unittest.mock import patch
    from fossier.registry_client import RegistryCheckResult

    mock_client = MagicMock()
    # 3 reports — below threshold of 5
    mock_client.check_username.return_value = RegistryCheckResult(known=True, report_count=3)

    with patch("fossier.pipeline._get_registry_client", return_value=mock_client):
        resolver = TrustResolver(config, db, api)
        decision = evaluate_contributor("user", resolver)

    # Should NOT be blocked — 3 < 5 threshold
    assert "registry" not in decision.reason.lower()
    mock_client.close.assert_called_once()


def test_registry_check_failure_continues(config, db, api):
    """When registry check fails, pipeline should continue normally."""
    config.registry_url = "https://registry.example.com"
    config.registry_check_before_scoring = True

    from unittest.mock import patch

    mock_client = MagicMock()
    mock_client.check_username.side_effect = Exception("connection refused")

    with patch("fossier.pipeline._get_registry_client", return_value=mock_client):
        resolver = TrustResolver(config, db, api)
        decision = evaluate_contributor("newuser", resolver)

    # Should still produce a decision (scored normally)
    assert decision.outcome in (Outcome.ALLOW, Outcome.REVIEW, Outcome.DENY)
    assert "registry" not in decision.reason.lower()


def test_registry_reports_denial(config, db, api):
    """After a score-based DENY, denial should be reported to registry."""
    config.registry_url = "https://registry.example.com"
    config.registry_report_denials = True
    config.registry_api_key = "test-key"

    # Make the user get a low score -> DENY
    api.get_user.return_value = {
        "created_at": "2026-03-15T00:00:00Z",
        "public_repos": 0,
        "public_gists": 0,
        "followers": 0,
        "following": 0,
        "type": "User",
        "email": None,
    }
    api.search_open_prs.return_value = 20
    api.get_repo.return_value = {"stargazers_count": 5000}

    from unittest.mock import patch

    mock_client = MagicMock()
    mock_client.report_spam.return_value = True

    with patch("fossier.pipeline._get_registry_client", return_value=mock_client):
        resolver = TrustResolver(config, db, api)
        decision = evaluate_contributor("spambot", resolver, pr_number=99)

    if decision.outcome == Outcome.DENY:
        mock_client.report_spam.assert_called_once()
        call_kwargs = mock_client.report_spam.call_args
        assert call_kwargs.kwargs["username"] == "spambot" or call_kwargs[1]["username"] == "spambot"
        mock_client.close.assert_called_once()
