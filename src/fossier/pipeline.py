"""Shared evaluation pipeline used by both CLI and GitHub Action."""

from __future__ import annotations

import logging
import re

from datetime import datetime, timedelta, timezone

from fossier.models import Contributor, Decision, Outcome, TrustTier
from fossier.scoring import score_contributor
from fossier.signals import is_bot_username
from fossier.trust import TrustResolver

logger = logging.getLogger(__name__)

# Patterns for AI agent co-author lines in commit messages.
# Match only the name portion before the email angle bracket.
_AI_COAUTHOR_RE = re.compile(
    r"co-authored-by:\s*([^<\n]*)\b("
    r"claude|copilot|github\s*copilot|gpt|openai|chatgpt"
    r"|cursor|codeium|windsurf|devin|anthropic|gemini|codex"
    r"|tabnine|amazon\s*q|cody"
    r")\b",
    re.IGNORECASE,
)


def _check_ai_authored(resolver: TrustResolver, pr_number: int) -> str | None:
    """Check PR commits for AI co-author signatures. Returns agent name or None."""
    commits = resolver.api.get_pr_commits(
        resolver.config.repo_owner, resolver.config.repo_name, pr_number
    )
    for commit in commits:
        message = commit.get("commit", {}).get("message", "")
        match = _AI_COAUTHOR_RE.search(message)
        if match:
            return match.group(2)
    return None


def evaluate_contributor(
    username: str,
    resolver: TrustResolver,
    pr_number: int | None = None,
) -> Decision:
    """Run the full evaluation pipeline: tier -> score (if needed) -> decision.

    Records the contributor, score, and decision in the database.
    Returns the Decision object (does NOT execute outcome actions).
    """
    username = username.lower()
    api = resolver.api
    config = resolver.config
    db = resolver.db

    # Check for AI-authored commits (hard reject if enabled)
    if resolver.config.reject_ai_authored and pr_number is not None:
        agent = _check_ai_authored(resolver, pr_number)
        if agent:
            reason = f"PR contains AI co-authored commits ({agent})"
            logger.info("Rejecting PR #%d: %s", pr_number, reason)
            contributor = Contributor(
                username=username,
                repo_owner=config.repo_owner,
                repo_name=config.repo_name,
                trust_tier=TrustTier.UNKNOWN,
                blocked_reason=reason,
            )
            decision = Decision(
                contributor=contributor,
                trust_tier=TrustTier.UNKNOWN,
                outcome=Outcome.DENY,
                reason=reason,
                pr_number=pr_number,
            )
            contributor_id = db.upsert_contributor(contributor)
            db.record_decision(contributor_id, decision, None)
            return decision

    # Check bot policy before full pipeline
    if config.bot_policy != "score" and is_bot_username(username):
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

    # Optional registry pre-check: block users with multiple spam reports
    if config.registry_url and config.registry_check_before_scoring:
        try:
            from fossier.registry_client import RegistryClient

            reg = RegistryClient(config.registry_url, config.registry_api_key)
            try:
                check = reg.check_username(username)
                if check and check.known and check.report_count >= 3:
                    reason = f"Known spam in global registry ({check.report_count} reports)"
                    logger.info("Blocking %s: %s", username, reason)
                    contributor = Contributor(
                        username=username,
                        repo_owner=config.repo_owner,
                        repo_name=config.repo_name,
                        trust_tier=TrustTier.BLOCKED,
                        blocked_reason=reason,
                    )
                    decision = Decision(
                        contributor=contributor,
                        trust_tier=TrustTier.BLOCKED,
                        outcome=Outcome.DENY,
                        reason=reason,
                        pr_number=pr_number,
                    )
                    contributor_id = db.upsert_contributor(contributor)
                    db.record_decision(contributor_id, decision, None)
                    return decision
            finally:
                reg.close()
        except Exception as e:
            logger.warning("Registry pre-check failed, continuing: %s", e)

    # Flood detection: block users mass-opening PRs/issues
    if config.flood_threshold > 0:
        tier_pre, _ = resolver.resolve_tier(username)
        if tier_pre not in (TrustTier.TRUSTED, TrustTier.KNOWN):
            since = (
                datetime.now(timezone.utc) - timedelta(hours=config.flood_window_hours)
            ).strftime("%Y-%m-%dT%H:%M:%SZ")
            try:
                count = api.count_recent_items(
                    config.repo_owner, config.repo_name, username, since
                )
                if count >= config.flood_threshold:
                    reason = (
                        f"Flood detected: {count} PRs/issues in "
                        f"{config.flood_window_hours}h (threshold: {config.flood_threshold})"
                    )
                    logger.info("Blocking %s: %s", username, reason)
                    contributor = Contributor(
                        username=username,
                        repo_owner=config.repo_owner,
                        repo_name=config.repo_name,
                        trust_tier=TrustTier.BLOCKED,
                        blocked_reason=reason,
                    )
                    decision = Decision(
                        contributor=contributor,
                        trust_tier=TrustTier.BLOCKED,
                        outcome=Outcome.DENY,
                        reason=reason,
                        pr_number=pr_number,
                    )
                    contributor_id = db.upsert_contributor(contributor)
                    db.record_decision(contributor_id, decision, None)
                    return decision
            except Exception as e:
                logger.warning("Flood detection failed, continuing: %s", e)

    tier, reason = resolver.resolve_tier(username)
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

    # Report denial to global registry
    if (
        config.registry_url
        and config.registry_report_denials
        and decision.outcome == Outcome.DENY
        and score_result  # only report score-based denials, not tier-based
    ):
        try:
            from fossier.registry_client import RegistryClient

            reg = RegistryClient(config.registry_url, config.registry_api_key)
            try:
                reg.report_spam(
                    username=username,
                    repo_owner=config.repo_owner,
                    repo_name=config.repo_name,
                    score=score_result.total_score,
                    reason=decision_reason,
                    pr_number=pr_number,
                    signals=score_result.signal_breakdown,
                )
            finally:
                reg.close()
        except Exception as e:
            logger.warning("Failed to report to registry: %s", e)

    return decision
