"""Shared evaluation pipeline used by both CLI and GitHub Action."""

from __future__ import annotations

import logging

from fossier.config import Config
from fossier.db import Database
from fossier.github_api import GitHubAPI
from fossier.models import Contributor, Decision, Outcome, TrustTier
from fossier.scoring import score_contributor
from fossier.signals import _BOT_USERNAME_PATTERNS
from fossier.trust import resolve_tier

logger = logging.getLogger(__name__)


def evaluate_contributor(
    username: str,
    config: Config,
    db: Database,
    api: GitHubAPI,
    pr_number: int | None = None,
) -> Decision:
    """Run the full evaluation pipeline: tier -> score (if needed) -> decision.

    Records the contributor, score, and decision in the database.
    Returns the Decision object (does NOT execute outcome actions).
    """
    username = username.lower()

    # Check bot policy before full pipeline
    if config.bot_policy != "score" and _BOT_USERNAME_PATTERNS.search(username):
        if config.bot_policy == "allow":
            tier, outcome = TrustTier.TRUSTED, Outcome.ALLOW
            reason = "Bot auto-allowed by bot_policy config"
        else:
            tier, outcome = TrustTier.BLOCKED, Outcome.DENY
            reason = "Bot auto-blocked by bot_policy config"

        contributor = Contributor(
            username=username,
            repo_owner=config.repo_owner,
            repo_name=config.repo_name,
            trust_tier=tier,
            blocked_reason=reason if outcome == Outcome.DENY else None,
        )
        decision = Decision(
            contributor=contributor,
            trust_tier=tier,
            outcome=outcome,
            reason=reason,
            pr_number=pr_number,
        )
        contributor_id = db.upsert_contributor(contributor)
        db.record_decision(contributor_id, decision, None)
        return decision

    # Resolve trust tier
    tier, reason = resolve_tier(username, config, db, api)
    contributor = Contributor(
        username=username,
        repo_owner=config.repo_owner,
        repo_name=config.repo_name,
        trust_tier=tier,
    )

    score_result = None

    if tier == TrustTier.BLOCKED:
        outcome = Outcome.DENY
        contributor.blocked_reason = reason
    elif tier in (TrustTier.TRUSTED, TrustTier.KNOWN):
        outcome = Outcome.ALLOW
    else:
        # Unknown -> run scoring
        score_result = score_contributor(api, config, username, pr_number)
        outcome = score_result.outcome
        contributor.latest_score = score_result.total_score
        if outcome == Outcome.ALLOW:
            contributor.trust_tier = TrustTier.KNOWN

    # Build reason string
    if tier != TrustTier.UNKNOWN:
        decision_reason = reason
    elif score_result:
        decision_reason = f"Score: {score_result.total_score}"
    else:
        decision_reason = reason

    decision = Decision(
        contributor=contributor,
        trust_tier=tier,
        outcome=outcome,
        reason=decision_reason,
        score_result=score_result,
        pr_number=pr_number,
    )

    # Record in DB
    contributor_id = db.upsert_contributor(contributor)
    score_history_id = None
    if score_result:
        score_history_id = db.record_score(contributor_id, score_result, pr_number)
    db.record_decision(contributor_id, decision, score_history_id)

    return decision
