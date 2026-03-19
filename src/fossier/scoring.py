"""Composite scoring: collect signals, weight, threshold → outcome."""

from __future__ import annotations

import logging

from fossier.config import Config
from fossier.github_api import GitHubAPI
from fossier.models import Outcome, ScoreResult, SignalResult
from fossier.signals import collect_signals

logger = logging.getLogger(__name__)


def score_contributor(
    api: GitHubAPI,
    config: Config,
    username: str,
    pr_number: int | None = None,
) -> ScoreResult:
    """Run the full scoring algorithm for a contributor."""
    signals = collect_signals(
        api=api,
        username=username,
        repo_owner=config.repo_owner,
        repo_name=config.repo_name,
        pr_number=pr_number,
        weights=config.signal_weights,
    )

    return compute_score(signals, config)


def compute_score(signals: list[SignalResult], config: Config) -> ScoreResult:
    """Compute composite score from collected signals."""
    successful = [s for s in signals if s.success]
    failed = [s for s in signals if not s.success]

    if not successful:
        # All signals failed
        return ScoreResult(
            total_score=50.0,
            confidence=0.0,
            signals=signals,
            outcome=Outcome.REVIEW,
        )

    # Redistribute failed signal weights proportionally
    total_success_weight = sum(s.weight for s in successful)
    total_failed_weight = sum(s.weight for s in failed)

    if total_success_weight > 0 and total_failed_weight > 0:
        redistribution_factor = (
            total_success_weight + total_failed_weight
        ) / total_success_weight
        for s in successful:
            s.weight *= redistribution_factor

    # Compute weighted score
    total_score = 0.0
    for s in successful:
        total_score += s.normalized * s.weight * 100

    confidence = sum(s.weight for s in successful if s.success)
    # Normalize confidence: original weights sum to 1.0, redistributed may be higher
    confidence = min(confidence / max(sum(s.weight for s in signals), 1.0), 1.0)
    # Use original success ratio as confidence
    confidence = total_success_weight / max(
        total_success_weight + total_failed_weight, 0.001
    )

    total_score = max(0.0, min(100.0, total_score))

    # Determine outcome from thresholds
    outcome = _apply_thresholds(total_score, confidence, config)

    return ScoreResult(
        total_score=round(total_score, 1),
        confidence=round(confidence, 3),
        signals=signals,
        outcome=outcome,
    )


def _apply_thresholds(score: float, confidence: float, config: Config) -> Outcome:
    """Map score + confidence to outcome."""
    # Low confidence forces REVIEW
    if confidence < config.thresholds.min_confidence:
        logger.info("Low confidence (%.2f), forcing REVIEW", confidence)
        return Outcome.REVIEW

    if score >= config.thresholds.allow_score:
        return Outcome.ALLOW
    elif score < config.thresholds.deny_score:
        return Outcome.DENY
    else:
        return Outcome.REVIEW
