from __future__ import annotations

import logging

from fossier.config import Config
from fossier.github_api import GitHubAPI
from fossier.models import Decision, Outcome, ScoreResult, TrustTier

logger = logging.getLogger(__name__)


def execute_outcome(
    decision: Decision,
    config: Config,
    api: GitHubAPI,
) -> None:
    """Execute the decided outcome actions on GitHub."""
    if config.dry_run:
        logger.info(
            "[DRY RUN] Would execute %s for %s",
            decision.outcome.value,
            decision.contributor.username,
        )
        return

    if decision.pr_number is None:
        logger.debug("No PR number, skipping GitHub actions")
        return

    owner = config.repo_owner
    repo = config.repo_name
    pr = decision.pr_number

    if decision.outcome == Outcome.DENY:
        _execute_deny(owner, repo, pr, decision, config, api)
    elif decision.outcome == Outcome.REVIEW:
        _execute_review(owner, repo, pr, decision, config, api)
    elif decision.outcome == Outcome.ALLOW:
        _execute_allow(owner, repo, pr, decision, config, api)


def _execute_deny(
    owner: str,
    repo: str,
    pr: int,
    decision: Decision,
    config: Config,
    api: GitHubAPI,
) -> None:
    if config.deny_action.comment:
        body = _format_deny_comment(decision, config.deny_action.contact_url)
        api.post_or_update_comment(owner, repo, pr, body)

    if config.deny_action.label:
        api.add_labels(owner, repo, pr, [config.deny_action.label])

    if config.deny_action.close_pr:
        api.close_pr(owner, repo, pr)


def _execute_review(
    owner: str,
    repo: str,
    pr: int,
    decision: Decision,
    config: Config,
    api: GitHubAPI,
) -> None:
    if config.review_action.comment:
        body = _format_review_comment(decision)
        api.post_or_update_comment(owner, repo, pr, body)

    if config.review_action.label:
        api.add_labels(owner, repo, pr, [config.review_action.label])


def _execute_allow(
    owner: str,
    repo: str,
    pr: int,
    decision: Decision,
    config: Config,
    api: GitHubAPI,
) -> None:
    # Only label KNOWN-tier contributors (scored and passed), not TRUSTED (maintainers/codeowners)
    if decision.trust_tier != TrustTier.KNOWN:
        logger.debug(
            "PR #%d allowed for %s (tier=%s), skipping allow actions",
            pr, decision.contributor.username, decision.trust_tier.value,
        )
        return

    if config.allow_action.label:
        api.add_labels(owner, repo, pr, [config.allow_action.label])

    if config.allow_action.comment:
        body = _format_allow_comment(decision)
        api.post_or_update_comment(owner, repo, pr, body)


def _format_allow_comment(decision: Decision) -> str:
    lines = [
        "## Fossier: Contributor Verified",
        "",
        f"`@{decision.contributor.username}` passed automated trust evaluation.",
    ]
    if decision.score_result:
        lines.append(f"Score: {decision.score_result.total_score}/100")
    return "\n".join(lines)


def _format_deny_comment(decision: Decision, contact_url: str = "") -> str:
    lines = [
        "## Fossier: PR Auto-Closed",
        "",
        f"This PR was automatically closed because `@{decision.contributor.username}` "
        f"did not meet the trust threshold for this repository.",
        "",
    ]

    if decision.score_result:
        lines.extend(_format_score_breakdown(decision.score_result))

    lines.extend(
        [
            "",
            "### Appeal",
            "If you believe this is a mistake, please open an issue to request manual review. "
            "A maintainer can vouch for you by adding your username to the `VOUCHED.td` file.",
        ]
    )
    if contact_url:
        lines.extend(
            [
                "",
                f"You can also reach the maintainers at: {contact_url} to appeal this decision",
            ]
        )

    return "\n".join(lines)


def _format_review_comment(decision: Decision) -> str:
    lines = [
        "## Fossier: Manual Review Requested",
        "",
        f"`@{decision.contributor.username}` is a new contributor. "
        "A maintainer should review this PR before merging.",
        "",
    ]

    if decision.score_result:
        lines.extend(_format_score_breakdown(decision.score_result))

    return "\n".join(lines)


def _format_score_breakdown(score: ScoreResult) -> list[str]:
    lines = [
        "### Score Breakdown",
        "",
        f"**Total Score:** {score.total_score}/100 | "
        f"**Confidence:** {score.confidence:.0%} | "
        f"**Outcome:** {score.outcome.value.upper()}",
        "",
        "| Signal | Value | Score | Weight |",
        "|--------|-------|-------|--------|",
    ]

    for s in score.signals:
        status = f"{s.normalized:.2f}" if s.success else f"FAILED ({s.error})"
        raw = (
            s.raw_value
            if not isinstance(s.raw_value, str) or len(s.raw_value) < 40
            else "..."
        )
        lines.append(f"| {s.name} | {raw} | {status} | {s.weight:.2f} |")

    return lines


def format_decision_text(decision: Decision) -> str:
    """Format a decision for CLI text output."""
    lines = [
        f"User:     {decision.contributor.username}",
        f"Tier:     {decision.trust_tier.value}",
        f"Outcome:  {decision.outcome.value.upper()}",
        f"Reason:   {decision.reason}",
    ]

    if decision.score_result:
        lines.append(
            f"Score:    {decision.score_result.total_score}/100 "
            f"(confidence: {decision.score_result.confidence:.0%})"
        )
        lines.append("")
        lines.append("Signals:")
        for s in decision.score_result.signals:
            if s.success:
                lines.append(f"  {s.name:25s} {s.normalized:.2f}  (raw: {s.raw_value})")
            else:
                lines.append(f"  {s.name:25s} FAILED  ({s.error})")

    return "\n".join(lines)


def format_decision_json(decision: Decision) -> dict:
    """Format a decision for JSON output."""
    result: dict = {
        "username": decision.contributor.username,
        "trust_tier": decision.trust_tier.value,
        "outcome": decision.outcome.value,
        "reason": decision.reason,
    }
    if decision.score_result:
        result["score"] = {
            "total": decision.score_result.total_score,
            "confidence": decision.score_result.confidence,
            "signals": decision.score_result.signal_breakdown,
        }
    if decision.pr_number:
        result["pr_number"] = decision.pr_number
    return result
