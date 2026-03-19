"""GitHub Action entrypoint: reads GITHUB_EVENT_PATH and runs the pipeline."""

from __future__ import annotations

import json
import logging
import os
import sys

from fossier.config import load_config
from fossier.db import Database
from fossier.github_api import GitHubAPI
from fossier.models import Outcome
from fossier.outcomes import execute_outcome, format_decision_json
from fossier.pipeline import evaluate_contributor

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
        decision = evaluate_contributor(username, config, db, api, pr_number)

        # Execute
        execute_outcome(decision, config, api)

        # Set GitHub Action outputs
        outcome = decision.outcome
        _set_output("outcome", outcome.value)
        _set_output("tier", decision.trust_tier.value)
        score_str = str(decision.score_result.total_score) if decision.score_result else ""
        _set_output("score", score_str)
        _set_output("details", json.dumps(format_decision_json(decision)))

        logger.info(
            "Decision: %s (%s) — %s",
            outcome.value, decision.trust_tier.value, decision.reason,
        )

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
