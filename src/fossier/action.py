"""GitHub Action entrypoint: reads GITHUB_EVENT_PATH and runs the pipeline."""

from __future__ import annotations

import json
import logging
import os
import sys

from fossier.config import load_config
from fossier.db import Database
from fossier.github_api import GitHubAPI
from fossier.models import Contributor, Decision, Outcome, TrustTier
from fossier.outcomes import execute_outcome, format_decision_json
from fossier.scoring import score_contributor
from fossier.trust import resolve_tier

logger = logging.getLogger(__name__)


def action_main() -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s: %(message)s",
    )

    event_path = os.environ.get("GITHUB_EVENT_PATH")
    if not event_path:
        logger.error("GITHUB_EVENT_PATH not set - not running in GitHub Actions?")
        return 3

    with open(event_path) as f:
        event = json.load(f)

    pr = event.get("pull_request") or event.get("number")
    if isinstance(pr, dict):
        pr_number = pr["number"]
        username = pr["user"]["login"].lower()
    else:
        logger.error("Could not extract PR info from event payload")
        return 3

    logger.info("Evaluating PR #%d by @%s", pr_number, username)

    config = load_config()
    db = Database(config.db_path)
    db.connect()
    api = GitHubAPI(config, db)

    try:
        # Resolve tier
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
            score_result = score_contributor(api, config, username, pr_number)
            outcome = score_result.outcome
            contributor.latest_score = score_result.total_score
            if outcome == Outcome.ALLOW:
                contributor.trust_tier = TrustTier.KNOWN

        decision = Decision(
            contributor=contributor,
            trust_tier=tier,
            outcome=outcome,
            reason=reason
            if tier != TrustTier.UNKNOWN
            else f"Score: {score_result.total_score}"
            if score_result
            else reason,
            score_result=score_result,
            pr_number=pr_number,
        )

        # Record
        contributor_id = db.upsert_contributor(contributor)
        score_history_id = None
        if score_result:
            score_history_id = db.record_score(contributor_id, score_result, pr_number)
        db.record_decision(contributor_id, decision, score_history_id)

        # Execute
        execute_outcome(decision, config, api)

        # Set GitHub Action outputs
        _set_output("outcome", outcome.value)
        _set_output("tier", tier.value)
        _set_output("score", str(score_result.total_score) if score_result else "")
        _set_output("details", json.dumps(format_decision_json(decision)))

        logger.info("Decision: %s (%s) — %s", outcome.value, tier.value, reason)

        return {Outcome.ALLOW: 0, Outcome.DENY: 1, Outcome.REVIEW: 2}[outcome]
    finally:
        api.close()
        db.close()


def _set_output(name: str, value: str) -> None:
    """Set a GitHub Actions output variable."""
    output_file = os.environ.get("GITHUB_OUTPUT")
    if output_file:
        with open(output_file, "a") as f:
            f.write(f"{name}={value}\n")


if __name__ == "__main__":
    sys.exit(action_main())
