"""Handle /fossier slash commands from PR comments."""

from __future__ import annotations

import logging
import os
import re

from fossier.config import Config
from fossier.db import Database
from fossier.github_api import GitHubAPI
from fossier.models import Outcome
from fossier.outcomes import (
    execute_outcome,
    format_approved_comment,
    format_rejected_comment,
    format_score_reply,
    format_vouched_comment,
)
from fossier.pipeline import evaluate_contributor
from fossier.scoring import score_contributor
from fossier.trust import TrustResolver
from fossier.trustdown import add_denounce, add_vouch, parse_vouched

logger = logging.getLogger(__name__)

_COMMAND_RE = re.compile(r"^/fossier[ \t]+(\S+)(?:[ \t]+(.+))?$", re.MULTILINE)

VALID_COMMANDS = {"approve", "vouch", "reject", "check", "score", "vouch-all"}


def parse_command(body: str) -> tuple[str, str] | None:
    """Extract a /fossier command and its arguments from a comment body.

    Returns (command, args) or None if no command found.
    """
    match = _COMMAND_RE.search(body)
    if not match:
        return None
    command = match.group(1).lower()
    args = (match.group(2) or "").strip()
    return command, args


def is_authorized(api: GitHubAPI, owner: str, repo: str, username: str) -> bool:
    """Check if a user has write or admin access to the repo."""
    permission = api.get_collaborator_permission(owner, repo, username)
    if permission:
        return permission in ("admin", "write")
    # Fallback: check collaborator list (less precise, no permission level)
    collaborators = api.get_collaborators(owner, repo)
    return username.lower() in collaborators


def _set_trust_changed() -> None:
    """Signal the workflow to commit VOUCHED.td changes."""
    env_file = os.environ.get("GITHUB_ENV")
    if env_file:
        with open(env_file, "a") as f:
            f.write("FOSSIER_TRUST_CHANGED=true\n")


class CommentCommandHandler:
    """Dispatch and execute /fossier commands from PR comments."""

    def __init__(
        self,
        config: Config,
        api: GitHubAPI,
        db: Database,
        event: dict,
    ):
        self.config = config
        self.api = api
        self.db = db
        self.event = event
        self.owner = config.repo_owner
        self.repo = config.repo_name

        comment = event["comment"]
        self.comment_body = comment["body"]
        self.comment_id = comment["id"]
        self.commenter = comment["user"]["login"].lower()
        self.pr_number = event["issue"]["number"]

    def run(self) -> int:
        """Parse, authorize, and dispatch the command. Returns exit code."""
        parsed = parse_command(self.comment_body)
        if not parsed:
            return 0

        command, args = parsed
        logger.info(
            "/fossier %s (by @%s on PR #%d)", command, self.commenter, self.pr_number
        )

        # Acknowledge receipt
        self.api.add_reaction(self.owner, self.repo, self.comment_id, "eyes")

        if command not in VALID_COMMANDS:
            self._reply_error(
                f"Unknown command: `{command}`. "
                f"Available: {', '.join(sorted(VALID_COMMANDS))}"
            )
            return 0

        if not is_authorized(self.api, self.owner, self.repo, self.commenter):
            self._reply_error(
                "You must be a repository collaborator with write access "
                "to use fossier commands."
            )
            return 0

        # Get PR author
        pr_author = self._get_pr_author()
        if not pr_author:
            self._reply_error("Could not determine PR author.")
            return 3

        try:
            handler = {
                "approve": self._handle_approve,
                "vouch": self._handle_vouch,
                "reject": self._handle_reject,
                "check": self._handle_check,
                "score": self._handle_score,
                "vouch-all": self._handle_vouch_all,
            }[command]
            handler(pr_author, args)
            self.api.add_reaction(self.owner, self.repo, self.comment_id, "+1")
            return 0
        except Exception:
            logger.exception("Error handling /fossier %s", command)
            self._reply_error("Fossier encountered an error processing this command.")
            return 3

    def _get_pr_author(self) -> str | None:
        pr = self.api.get_pr(self.owner, self.repo, self.pr_number)
        if pr and isinstance(pr, dict):
            user = pr.get("user", {})
            login = user.get("login")
            if login:
                return login.lower()
        return None

    def _reply_error(self, message: str) -> None:
        self.api.add_reaction(self.owner, self.repo, self.comment_id, "-1")
        self.api.post_comment(
            self.owner, self.repo, self.pr_number, f"**Fossier:** {message}"
        )

    def _handle_approve(self, pr_author: str, _args: str) -> None:
        """Override review decision: remove label, update comment, reopen if needed."""
        # Remove fossier labels
        if self.config.review_action.label:
            self.api.remove_label(
                self.owner, self.repo, self.pr_number, self.config.review_action.label
            )
        if self.config.deny_action.label:
            self.api.remove_label(
                self.owner, self.repo, self.pr_number, self.config.deny_action.label
            )

        # Update the fossier status comment
        body = format_approved_comment(self.commenter)
        self.api.post_or_update_comment(self.owner, self.repo, self.pr_number, body)

        # Reopen PR if it was closed
        pr = self.api.get_pr(self.owner, self.repo, self.pr_number)
        if pr and pr.get("state") == "closed":
            self.api.reopen_pr(self.owner, self.repo, self.pr_number)

    def _handle_vouch(self, pr_author: str, _args: str) -> None:
        """Vouch for the PR author and approve."""
        add_vouch(self.config.repo_root, pr_author)
        _set_trust_changed()

        # Remove fossier labels
        if self.config.review_action.label:
            self.api.remove_label(
                self.owner, self.repo, self.pr_number, self.config.review_action.label
            )
        if self.config.deny_action.label:
            self.api.remove_label(
                self.owner, self.repo, self.pr_number, self.config.deny_action.label
            )

        # Update the fossier status comment
        body = format_vouched_comment(self.commenter, pr_author)
        self.api.post_or_update_comment(self.owner, self.repo, self.pr_number, body)

        # Reopen PR if it was closed
        pr = self.api.get_pr(self.owner, self.repo, self.pr_number)
        if pr and pr.get("state") == "closed":
            self.api.reopen_pr(self.owner, self.repo, self.pr_number)

    def _handle_reject(self, pr_author: str, args: str) -> None:
        """Reject the PR: denounce, report to registry, close."""
        reason = args.strip()
        if not reason:
            self._reply_error(
                "Usage: `/fossier reject <reason>`. A reason is required."
            )
            # Undo the eyes reaction by adding -1, but don't double-react
            return

        add_denounce(self.config.repo_root, pr_author, reason)
        _set_trust_changed()

        # Report to global registry if configured
        if self.config.registry_url and self.config.registry_api_key:
            from fossier.registry_client import RegistryClient

            reg = RegistryClient(self.config.registry_url, self.config.registry_api_key)
            try:
                reg.report_spam(
                    username=pr_author,
                    repo_owner=self.owner,
                    repo_name=self.repo,
                    score=0.0,
                    reason=f"Manual rejection by @{self.commenter}: {reason}",
                    pr_number=self.pr_number,
                )
            except Exception:
                logger.warning("Failed to report to registry", exc_info=True)
            finally:
                reg.close()

        # Add spam label and close
        if self.config.deny_action.label:
            self.api.add_labels(
                self.owner, self.repo, self.pr_number, [self.config.deny_action.label]
            )
        self.api.close_pr(self.owner, self.repo, self.pr_number)

        # Update the fossier status comment
        body = format_rejected_comment(self.commenter, reason)
        self.api.post_or_update_comment(self.owner, self.repo, self.pr_number, body)

    def _handle_check(self, pr_author: str, _args: str) -> None:
        """Re-run the full evaluation pipeline."""
        resolver = TrustResolver(self.config, self.db, self.api)
        decision = evaluate_contributor(pr_author, resolver, self.pr_number)
        execute_outcome(decision, self.config, self.api)

    def _handle_score(self, pr_author: str, _args: str) -> None:
        """Post the score breakdown as a reply comment."""
        score_result = score_contributor(
            self.api, self.config, pr_author, self.pr_number
        )
        body = format_score_reply(score_result, pr_author)
        self.api.post_comment(self.owner, self.repo, self.pr_number, body)

    def _handle_vouch_all(self, _pr_author: str, _args: str) -> None:
        """Vouch for all existing repo contributors."""
        contributors = self.api.get_contributors(self.owner, self.repo)
        if not contributors:
            self.api.post_comment(
                self.owner,
                self.repo,
                self.pr_number,
                "**Fossier:** No contributors found for this repository.",
            )
            return

        existing = parse_vouched(self.config.repo_root)
        added = 0
        for username in contributors:
            if username not in existing.vouched:
                add_vouch(self.config.repo_root, username)
                added += 1

        if added > 0:
            _set_trust_changed()

        already = len(contributors) - added
        self.api.post_comment(
            self.owner,
            self.repo,
            self.pr_number,
            f"**Fossier:** Vouched for {added} new contributor(s) "
            f"({already} already vouched).",
        )
