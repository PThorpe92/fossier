"""Tests for outcomes module: comment formatting and execution."""

from __future__ import annotations

import json
from unittest.mock import MagicMock

from fossier.config import Config
from fossier.models import Contributor, Decision, Outcome, ScoreResult, SignalResult, TrustTier
from fossier.outcomes import (
    execute_outcome,
    format_decision_json,
    format_decision_text,
    _format_deny_comment,
    _format_review_comment,
    _format_score_breakdown,
)


def _make_decision(
    outcome: Outcome = Outcome.ALLOW,
    tier: TrustTier = TrustTier.UNKNOWN,
    score: float | None = None,
    pr_number: int | None = 42,
) -> Decision:
    contributor = Contributor(
        username="testuser",
        repo_owner="owner",
        repo_name="repo",
        trust_tier=tier,
    )
    score_result = None
    if score is not None:
        score_result = ScoreResult(
            total_score=score,
            confidence=0.85,
            signals=[
                SignalResult("account_age", 365, 0.8, 0.15),
                SignalResult("public_repos", 10, 0.5, 0.10),
                SignalResult("bad_signal", 0, 0.0, 0.10, success=False, error="fail"),
            ],
            outcome=outcome,
        )
    return Decision(
        contributor=contributor,
        trust_tier=tier,
        outcome=outcome,
        reason="test reason",
        score_result=score_result,
        pr_number=pr_number,
    )


class TestExecuteOutcome:
    def test_dry_run_skips_actions(self):
        config = Config(dry_run=True)
        api = MagicMock()
        decision = _make_decision(Outcome.DENY, score=30.0)
        execute_outcome(decision, config, api)
        api.post_or_update_comment.assert_not_called()
        api.add_labels.assert_not_called()
        api.close_pr.assert_not_called()

    def test_no_pr_skips_actions(self):
        config = Config()
        api = MagicMock()
        decision = _make_decision(Outcome.DENY, pr_number=None, score=30.0)
        execute_outcome(decision, config, api)
        api.post_or_update_comment.assert_not_called()

    def test_deny_posts_comment_and_closes(self):
        config = Config(repo_owner="o", repo_name="r")
        api = MagicMock()
        decision = _make_decision(Outcome.DENY, score=30.0)
        execute_outcome(decision, config, api)
        api.post_or_update_comment.assert_called_once()
        api.add_labels.assert_called_once()
        api.close_pr.assert_called_once_with("o", "r", 42)

    def test_deny_no_close_if_config_off(self):
        config = Config(repo_owner="o", repo_name="r")
        config.deny_action.close_pr = False
        api = MagicMock()
        decision = _make_decision(Outcome.DENY, score=30.0)
        execute_outcome(decision, config, api)
        api.close_pr.assert_not_called()

    def test_review_posts_comment_and_label(self):
        config = Config(repo_owner="o", repo_name="r")
        api = MagicMock()
        decision = _make_decision(Outcome.REVIEW, score=55.0)
        execute_outcome(decision, config, api)
        api.post_or_update_comment.assert_called_once()
        api.add_labels.assert_called_once()
        api.close_pr.assert_not_called()

    def test_allow_is_silent(self):
        config = Config(repo_owner="o", repo_name="r")
        api = MagicMock()
        decision = _make_decision(Outcome.ALLOW)
        execute_outcome(decision, config, api)
        api.post_or_update_comment.assert_not_called()
        api.add_labels.assert_not_called()
        api.close_pr.assert_not_called()


class TestCommentFormatting:
    def test_deny_comment_has_header(self):
        decision = _make_decision(Outcome.DENY, score=30.0)
        body = _format_deny_comment(decision)
        assert "## Fossier: PR Auto-Closed" in body
        assert "testuser" in body
        assert "Appeal" in body

    def test_deny_comment_has_score_breakdown(self):
        decision = _make_decision(Outcome.DENY, score=30.0)
        body = _format_deny_comment(decision)
        assert "Score Breakdown" in body
        assert "account_age" in body

    def test_deny_comment_includes_contact_url(self):
        decision = _make_decision(Outcome.DENY, score=30.0)
        body = _format_deny_comment(decision, contact_url="https://discord.gg/test")
        assert "https://discord.gg/test" in body
        assert "appeal" in body.lower()

    def test_deny_comment_no_contact_url(self):
        decision = _make_decision(Outcome.DENY, score=30.0)
        body = _format_deny_comment(decision)
        assert "reach the maintainers" not in body

    def test_review_comment_has_header(self):
        decision = _make_decision(Outcome.REVIEW, score=55.0)
        body = _format_review_comment(decision)
        assert "## Fossier: Manual Review Requested" in body
        assert "testuser" in body

    def test_score_breakdown_shows_failed_signals(self):
        score = ScoreResult(
            total_score=50.0,
            confidence=0.5,
            signals=[
                SignalResult("good", 1, 0.8, 0.5),
                SignalResult("bad", 0, 0.0, 0.5, success=False, error="API error"),
            ],
            outcome=Outcome.REVIEW,
        )
        lines = _format_score_breakdown(score)
        text = "\n".join(lines)
        assert "FAILED" in text
        assert "API error" in text


class TestFormatDecisionText:
    def test_basic_output(self):
        decision = _make_decision(Outcome.ALLOW, tier=TrustTier.TRUSTED)
        text = format_decision_text(decision)
        assert "testuser" in text
        assert "trusted" in text
        assert "ALLOW" in text

    def test_with_score(self):
        decision = _make_decision(Outcome.REVIEW, score=55.0)
        text = format_decision_text(decision)
        assert "55.0/100" in text
        assert "Signals:" in text
        assert "account_age" in text

    def test_shows_failed_signals(self):
        decision = _make_decision(Outcome.REVIEW, score=55.0)
        text = format_decision_text(decision)
        assert "FAILED" in text


class TestFormatDecisionJson:
    def test_basic_structure(self):
        decision = _make_decision(Outcome.ALLOW, tier=TrustTier.TRUSTED)
        data = format_decision_json(decision)
        assert data["username"] == "testuser"
        assert data["trust_tier"] == "trusted"
        assert data["outcome"] == "allow"
        assert data["reason"] == "test reason"
        assert data["pr_number"] == 42

    def test_with_score(self):
        decision = _make_decision(Outcome.REVIEW, score=55.0)
        data = format_decision_json(decision)
        assert "score" in data
        assert data["score"]["total"] == 55.0
        assert data["score"]["confidence"] == 0.85

    def test_json_serializable(self):
        decision = _make_decision(Outcome.REVIEW, score=55.0)
        data = format_decision_json(decision)
        # Should not raise
        json.dumps(data)

    def test_no_pr_number(self):
        decision = _make_decision(Outcome.ALLOW, pr_number=None)
        data = format_decision_json(decision)
        assert "pr_number" not in data
