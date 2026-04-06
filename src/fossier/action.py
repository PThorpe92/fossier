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
from fossier.trust import TrustResolver

logger = logging.getLogger(__name__)


class GithubAction:
    def __init__(self):
        logging.basicConfig(
            level=logging.WARN,
            format="%(levelname)s: %(message)s",
        )

    def run(self) -> int:
        event_path = os.environ.get("GITHUB_EVENT_PATH")
        if not event_path:
            logger.error("GITHUB_EVENT_PATH not set - not running in GitHub Actions?")
            return 3

        with open(event_path) as f:
            event = json.load(f)

        event_name = os.environ.get("GITHUB_EVENT_NAME", "")
        if event_name == "issue_comment":
            return self._handle_comment(event)
        return self._handle_pr(event)

    def _handle_comment(self, event: dict) -> int:
        """Handle an issue_comment event — dispatch /fossier commands."""
        # Only process comments on PRs, not issues
        if "pull_request" not in event.get("issue", {}):
            return 0

        from fossier.comment_commands import CommentCommandHandler, parse_command

        # Quick check: skip if no /fossier command in the comment
        comment_body = event.get("comment", {}).get("body", "")
        if not parse_command(comment_body):
            return 0

        config = load_config()
        db = Database(config.db_path)
        db.connect()
        api = GitHubAPI(config, db)

        try:
            handler = CommentCommandHandler(config, api, db, event)
            return handler.run()
        finally:
            api.close()
            db.close()

    def _handle_pr(self, event: dict) -> int:
        """Handle a pull_request event — the original evaluation flow."""
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
        resolver = TrustResolver(config, db, api)

        try:
            decision = evaluate_contributor(username, resolver, pr_number)
            execute_outcome(decision, config, api)

            # Set GitHub Action outputs
            outcome = decision.outcome
            self._set_output("outcome", outcome.value)
            self._set_output("tier", decision.trust_tier.value)
            score_str = (
                str(decision.score_result.total_score) if decision.score_result else ""
            )
            self._set_output("score", score_str)
            self._set_output("details", json.dumps(format_decision_json(decision)))

            logger.info(
                "Decision: %s (%s) — %s",
                outcome.value,
                decision.trust_tier.value,
                decision.reason,
            )

            return {Outcome.ALLOW: 0, Outcome.DENY: 1, Outcome.REVIEW: 2}[outcome]
        finally:
            api.close()
            db.close()

    def _set_output(self, name: str, value: str) -> None:
        """Set a GitHub Actions output variable."""

        output_file = os.environ.get("GITHUB_OUTPUT")
        if output_file:
            with open(output_file, "a") as f:
                f.write(f"{name}={value}\n")


if __name__ == "__main__":
    action = GithubAction()
    sys.exit(action.run())
