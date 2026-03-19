"""Tests for scoring algorithm."""

from fossier.config import Config, ThresholdConfig
from fossier.models import Outcome, SignalResult
from fossier.scoring import compute_score


def _make_config(**kwargs) -> Config:
    config = Config()
    for k, v in kwargs.items():
        setattr(config.thresholds, k, v)
    return config


def test_all_signals_pass_high_score():
    signals = [
        SignalResult("account_age", 730, 1.0, 0.15),
        SignalResult("public_repos", 25, 1.0, 0.10),
        SignalResult("contribution_history", 300, 1.0, 0.10),
        SignalResult("open_prs_elsewhere", 2, 0.87, 0.15),
        SignalResult("prior_interaction", True, 1.0, 0.15),
        SignalResult("pr_content", "good", 0.9, 0.15),
        SignalResult("follower_ratio", 3.0, 1.0, 0.10),
        SignalResult("bot_signals", False, 1.0, 0.10),
    ]
    result = compute_score(signals, _make_config())
    assert result.total_score >= 70
    assert result.outcome == Outcome.ALLOW
    assert result.confidence > 0.9


def test_all_signals_fail():
    signals = [
        SignalResult("account_age", 0, 0.0, 0.15, success=False, error="fail"),
        SignalResult("public_repos", 0, 0.0, 0.10, success=False, error="fail"),
        SignalResult("contribution_history", 0, 0.0, 0.10, success=False, error="fail"),
        SignalResult("open_prs_elsewhere", 0, 0.0, 0.15, success=False, error="fail"),
        SignalResult("prior_interaction", 0, 0.0, 0.15, success=False, error="fail"),
        SignalResult("pr_content", 0, 0.0, 0.15, success=False, error="fail"),
        SignalResult("follower_ratio", 0, 0.0, 0.10, success=False, error="fail"),
        SignalResult("bot_signals", 0, 0.0, 0.10, success=False, error="fail"),
    ]
    result = compute_score(signals, _make_config())
    assert result.total_score == 50.0
    assert result.confidence == 0.0
    assert result.outcome == Outcome.REVIEW


def test_low_score_deny():
    signals = [
        SignalResult("account_age", 10, 0.03, 0.15),
        SignalResult("public_repos", 0, 0.0, 0.10),
        SignalResult("contribution_history", 0, 0.0, 0.10),
        SignalResult("open_prs_elsewhere", 20, 0.0, 0.15),
        SignalResult("prior_interaction", False, 0.0, 0.15),
        SignalResult("pr_content", "docs_only", 0.1, 0.15),
        SignalResult("follower_ratio", 0.0, 0.0, 0.10),
        SignalResult("bot_signals", False, 1.0, 0.10),
    ]
    result = compute_score(signals, _make_config())
    assert result.total_score < 40
    assert result.outcome == Outcome.DENY


def test_mid_score_review():
    signals = [
        SignalResult("account_age", 180, 0.49, 0.15),
        SignalResult("public_repos", 5, 0.25, 0.10),
        SignalResult("contribution_history", 10, 0.05, 0.10),
        SignalResult("open_prs_elsewhere", 5, 0.67, 0.15),
        SignalResult("prior_interaction", False, 0.0, 0.15),
        SignalResult("pr_content", "mixed", 0.6, 0.15),
        SignalResult("follower_ratio", 0.5, 0.25, 0.10),
        SignalResult("bot_signals", False, 1.0, 0.10),
    ]
    result = compute_score(signals, _make_config())
    assert 40 <= result.total_score < 70
    assert result.outcome == Outcome.REVIEW


def test_low_confidence_forces_review():
    # Only 2 signals succeed (weight < 0.5)
    signals = [
        SignalResult("account_age", 730, 1.0, 0.15),
        SignalResult("public_repos", 25, 1.0, 0.10),
        SignalResult("contribution_history", 0, 0.0, 0.10, success=False, error="fail"),
        SignalResult("open_prs_elsewhere", 0, 0.0, 0.15, success=False, error="fail"),
        SignalResult("prior_interaction", 0, 0.0, 0.15, success=False, error="fail"),
        SignalResult("pr_content", 0, 0.0, 0.15, success=False, error="fail"),
        SignalResult("follower_ratio", 0, 0.0, 0.10, success=False, error="fail"),
        SignalResult("bot_signals", 0, 0.0, 0.10, success=False, error="fail"),
    ]
    result = compute_score(signals, _make_config())
    assert result.confidence < 0.5
    assert result.outcome == Outcome.REVIEW


def test_weight_redistribution():
    # One signal fails, its weight should be redistributed
    signals = [
        SignalResult("account_age", 365, 1.0, 0.5),
        SignalResult("public_repos", 0, 0.0, 0.5, success=False, error="fail"),
    ]
    result = compute_score(signals, _make_config())
    # account_age has normalized=1.0 and should get full weight after redistribution
    assert result.total_score == 100.0


def test_custom_thresholds():
    signals = [
        SignalResult("account_age", 365, 0.8, 0.5),
        SignalResult("public_repos", 15, 0.75, 0.5),
    ]
    # Lower allow threshold
    result = compute_score(signals, _make_config(allow_score=50.0))
    assert result.outcome == Outcome.ALLOW
