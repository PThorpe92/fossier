"""Tests for the shared evaluation pipeline."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from fossier.config import Config
from fossier.db import Database
from fossier.models import Outcome, TrustTier
from fossier.pipeline import evaluate_contributor


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
    api.search_prior_interaction.return_value = False
    api.get_pr_files.return_value = []
    api.get_user_orgs.return_value = []
    api.get_pr.return_value = None
    api.get_repo.return_value = {"stargazers_count": 50}
    api.get_pr_commits.return_value = []
    return api


def test_trusted_user_gets_allow(config, db, api):
    config.trusted_users = {"alice"}
    decision = evaluate_contributor("alice", config, db, api)
    assert decision.outcome == Outcome.ALLOW
    assert decision.trust_tier == TrustTier.TRUSTED


def test_blocked_user_gets_deny(config, db, api):
    config.blocked_users = {"spammer"}
    decision = evaluate_contributor("spammer", config, db, api)
    assert decision.outcome == Outcome.DENY
    assert decision.trust_tier == TrustTier.BLOCKED


def test_unknown_user_gets_scored(config, db, api):
    decision = evaluate_contributor("newuser", config, db, api)
    assert decision.trust_tier == TrustTier.UNKNOWN
    assert decision.score_result is not None
    assert decision.outcome in (Outcome.ALLOW, Outcome.REVIEW, Outcome.DENY)


def test_records_in_db(config, db, api):
    config.trusted_users = {"alice"}
    evaluate_contributor("alice", config, db, api)
    contributor = db.get_contributor("testowner", "testrepo", "alice")
    assert contributor is not None
    assert contributor.trust_tier == TrustTier.TRUSTED


def test_bot_policy_allow(config, db, api):
    config.bot_policy = "allow"
    decision = evaluate_contributor("dependabot[bot]", config, db, api)
    assert decision.outcome == Outcome.ALLOW
    assert "bot" in decision.reason.lower()


def test_bot_policy_block(config, db, api):
    config.bot_policy = "block"
    decision = evaluate_contributor("renovate-bot", config, db, api)
    assert decision.outcome == Outcome.DENY
    assert "bot" in decision.reason.lower()


def test_bot_policy_score_runs_normal_pipeline(config, db, api):
    config.bot_policy = "score"
    decision = evaluate_contributor("dependabot[bot]", config, db, api)
    # Normal pipeline runs — bot gets scored normally
    assert decision.trust_tier == TrustTier.UNKNOWN
    assert decision.score_result is not None


def test_username_normalized_to_lowercase(config, db, api):
    config.trusted_users = {"alice"}
    decision = evaluate_contributor("ALICE", config, db, api)
    assert decision.contributor.username == "alice"
    assert decision.outcome == Outcome.ALLOW
