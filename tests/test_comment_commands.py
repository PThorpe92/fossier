"""Tests for PR comment slash commands."""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from fossier.comment_commands import (
    CommentCommandHandler,
    parse_command,
    is_authorized,
    VALID_COMMANDS,
)
from fossier.config import Config
from fossier.outcomes import (
    format_approved_comment,
    format_rejected_comment,
    format_vouched_comment,
    format_score_reply,
)
from fossier.models import Outcome, ScoreResult, SignalResult


# --- parse_command tests ---


class TestParseCommand:
    def test_approve(self):
        assert parse_command("/fossier approve") == ("approve", "")

    def test_vouch(self):
        assert parse_command("/fossier vouch") == ("vouch", "")

    def test_reject_with_reason(self):
        assert parse_command("/fossier reject spammer") == ("reject", "spammer")

    def test_reject_multi_word_reason(self):
        assert parse_command("/fossier reject SEO link spam") == (
            "reject",
            "SEO link spam",
        )

    def test_check(self):
        assert parse_command("/fossier check") == ("check", "")

    def test_score(self):
        assert parse_command("/fossier score") == ("score", "")

    def test_vouch_all(self):
        assert parse_command("/fossier vouch-all") == ("vouch-all", "")

    def test_command_in_middle_of_comment(self):
        body = "I've reviewed this PR.\n/fossier approve\nLooks good."
        assert parse_command(body) == ("approve", "")

    def test_no_command(self):
        assert parse_command("This is a normal comment") is None

    def test_empty_comment(self):
        assert parse_command("") is None

    def test_fossier_alone(self):
        # /fossier with no subcommand should not match
        assert parse_command("/fossier") is None

    def test_case_insensitive_command(self):
        assert parse_command("/fossier APPROVE") == ("approve", "")

    def test_extra_whitespace(self):
        assert parse_command("/fossier   approve") == ("approve", "")

    def test_not_at_line_start_but_on_own_line(self):
        body = "text\n/fossier approve"
        assert parse_command(body) == ("approve", "")

    def test_unknown_command_still_parses(self):
        result = parse_command("/fossier unknown")
        assert result == ("unknown", "")


# --- is_authorized tests ---


class TestIsAuthorized:
    def test_admin_authorized(self):
        api = MagicMock()
        api.get_collaborator_permission.return_value = "admin"
        assert is_authorized(api, "owner", "repo", "user") is True

    def test_write_authorized(self):
        api = MagicMock()
        api.get_collaborator_permission.return_value = "write"
        assert is_authorized(api, "owner", "repo", "user") is True

    def test_read_not_authorized(self):
        api = MagicMock()
        api.get_collaborator_permission.return_value = "read"
        assert is_authorized(api, "owner", "repo", "user") is False

    def test_none_not_authorized(self):
        api = MagicMock()
        api.get_collaborator_permission.return_value = "none"
        assert is_authorized(api, "owner", "repo", "user") is False

    def test_fallback_to_collaborator_list(self):
        api = MagicMock()
        api.get_collaborator_permission.return_value = None
        api.get_collaborators.return_value = ["user", "other"]
        assert is_authorized(api, "owner", "repo", "user") is True

    def test_fallback_not_in_list(self):
        api = MagicMock()
        api.get_collaborator_permission.return_value = None
        api.get_collaborators.return_value = ["other"]
        assert is_authorized(api, "owner", "repo", "user") is False


# --- Comment formatting tests ---


class TestCommentFormatting:
    def test_approved_comment(self):
        body = format_approved_comment("maintainer")
        assert "## Fossier: Approved by Maintainer" in body
        assert "@maintainer" in body

    def test_rejected_comment(self):
        body = format_rejected_comment("maintainer", "SEO spam")
        assert "## Fossier: Rejected by Maintainer" in body
        assert "@maintainer" in body
        assert "SEO spam" in body

    def test_vouched_comment(self):
        body = format_vouched_comment("maintainer", "newuser")
        assert "## Fossier: Vouched and Approved" in body
        assert "@maintainer" in body
        assert "@newuser" in body

    def test_score_reply(self):
        score = ScoreResult(
            total_score=65.0,
            confidence=0.9,
            signals=[
                SignalResult("account_age", 365, 0.8, 0.15),
            ],
            outcome=Outcome.REVIEW,
        )
        body = format_score_reply(score, "testuser")
        assert "## Fossier: Score Breakdown" in body
        assert "testuser" in body
        assert "account_age" in body


# --- Fixtures ---


def _make_event(
    comment_body: str = "/fossier approve",
    commenter: str = "maintainer",
    pr_number: int = 42,
    comment_id: int = 12345,
    pr_author: str = "prauthor",
    pr_state: str = "open",
) -> dict:
    return {
        "comment": {
            "body": comment_body,
            "user": {"login": commenter},
            "id": comment_id,
        },
        "issue": {
            "number": pr_number,
            "state": pr_state,
            "user": {"login": pr_author},
            "pull_request": {"url": "https://api.github.com/repos/o/r/pulls/42"},
        },
    }


@pytest.fixture
def handler_setup(tmp_path):
    """Create a CommentCommandHandler with mocked dependencies."""

    def _make(
        comment_body="/fossier approve",
        commenter="maintainer",
        pr_state="open",
    ):
        config = Config(
            repo_owner="owner",
            repo_name="repo",
            repo_root=tmp_path,
            db_path=str(tmp_path / "test.db"),
            github_token="test-token",
        )
        api = MagicMock()
        api.get_collaborator_permission.return_value = "write"
        api.get_pr.return_value = {
            "number": 42,
            "user": {"login": "prauthor"},
            "state": pr_state,
        }
        api.remove_label.return_value = True
        api.add_reaction.return_value = {}
        api.post_or_update_comment.return_value = {}
        api.post_comment.return_value = {}
        api.add_labels.return_value = {}
        api.close_pr.return_value = {}
        api.reopen_pr.return_value = {}

        db = MagicMock()
        event = _make_event(
            comment_body=comment_body, commenter=commenter, pr_state=pr_state
        )
        handler = CommentCommandHandler(config, api, db, event)
        return handler, config, api, db

    return _make


# --- CommentCommandHandler tests ---


class TestHandlerRun:
    def test_no_command_returns_zero(self, handler_setup):
        handler, _, api, _ = handler_setup("Just a regular comment")
        result = handler.run()
        assert result == 0
        api.add_reaction.assert_not_called()

    def test_unknown_command_replies_error(self, handler_setup):
        handler, _, api, _ = handler_setup("/fossier badcmd")
        result = handler.run()
        assert result == 0
        # Should have eyes + -1 reactions
        assert api.add_reaction.call_count >= 2
        api.post_comment.assert_called_once()
        body = api.post_comment.call_args[0][3]
        assert "Unknown command" in body

    def test_unauthorized_replies_error(self, handler_setup):
        handler, _, api, _ = handler_setup("/fossier approve", commenter="stranger")
        api.get_collaborator_permission.return_value = "read"
        api.get_collaborators.return_value = []
        result = handler.run()
        assert result == 0
        api.post_comment.assert_called_once()
        body = api.post_comment.call_args[0][3]
        assert "write access" in body


class TestHandleApprove:
    def test_removes_labels_and_updates_comment(self, handler_setup):
        handler, config, api, _ = handler_setup("/fossier approve")
        result = handler.run()
        assert result == 0

        # Should remove review and deny labels
        assert api.remove_label.call_count == 2
        api.post_or_update_comment.assert_called_once()
        body = api.post_or_update_comment.call_args[0][3]
        assert "Approved by Maintainer" in body

    def test_falls_back_to_api_when_event_lacks_author(self, handler_setup):
        # If a future webhook payload ever omits issue.user, the API fetch
        # should still recover the author rather than aborting.
        handler, _, api, _ = handler_setup("/fossier approve")
        handler.event["issue"]["user"] = None
        result = handler.run()
        assert result == 0

    def test_errors_when_author_unrecoverable(self, handler_setup):
        # Both the event and the API fail to surface a login — only then do
        # we surface the "Could not determine PR author" error.
        handler, _, api, _ = handler_setup("/fossier approve")
        handler.event["issue"]["user"] = None
        api.get_pr.return_value = None
        result = handler.run()
        assert result == 3
        api.post_comment.assert_called_once()
        body = api.post_comment.call_args[0][3]
        assert "Could not determine PR author" in body

    def test_adds_manual_approval_label(self, handler_setup):
        handler, config, api, _ = handler_setup("/fossier approve")
        handler.run()
        api.add_labels.assert_called_once_with(
            "owner", "repo", 42, [config.manual_approval_label]
        )

    def test_skips_label_when_disabled(self, handler_setup):
        handler, config, api, _ = handler_setup("/fossier approve")
        config.manual_approval_label = ""
        handler.run()
        api.add_labels.assert_not_called()

    def test_deletes_registry_report_when_configured(self, handler_setup):
        handler, config, api, _ = handler_setup("/fossier approve")
        config.registry_url = "https://registry.example.com"
        config.registry_api_key = "test-key"

        mock_reg = MagicMock()
        with patch(
            "fossier.registry_client.RegistryClient", return_value=mock_reg
        ) as reg_cls:
            handler.run()

        reg_cls.assert_called_once_with("https://registry.example.com", "test-key")
        mock_reg.delete_report.assert_called_once_with("prauthor", "owner", "repo")
        mock_reg.close.assert_called_once()

    def test_skips_registry_when_not_configured(self, handler_setup):
        handler, config, api, _ = handler_setup("/fossier approve")
        # No registry_url/api_key
        with patch("fossier.registry_client.RegistryClient") as reg_cls:
            handler.run()
        reg_cls.assert_not_called()

    def test_registry_delete_failure_does_not_abort_approval(self, handler_setup):
        handler, config, api, _ = handler_setup("/fossier approve")
        config.registry_url = "https://registry.example.com"
        config.registry_api_key = "test-key"

        mock_reg = MagicMock()
        mock_reg.delete_report.side_effect = RuntimeError("network down")
        with patch("fossier.registry_client.RegistryClient", return_value=mock_reg):
            result = handler.run()

        # Approval still succeeds even if registry cleanup errors out.
        assert result == 0
        api.post_or_update_comment.assert_called_once()
        mock_reg.close.assert_called_once()


class TestHandleVouch:
    def test_vouches_and_approves(self, handler_setup, tmp_path):
        handler, config, api, _ = handler_setup("/fossier vouch")

        # Create VOUCHED.td
        (tmp_path / "VOUCHED.td").write_text("# vouches\n")

        env_file = str(tmp_path / "github_env")
        with open(env_file, "w"):
            pass

        with patch.dict(os.environ, {"GITHUB_ENV": env_file}):
            result = handler.run()

        assert result == 0

        # Check VOUCHED.td was updated
        content = (tmp_path / "VOUCHED.td").read_text()
        assert "prauthor" in content

        # Check env vars were set so action.yml can open a PR
        with open(env_file) as f:
            env_contents = f.read()
        assert "FOSSIER_TRUST_CHANGED=true" in env_contents
        assert "FOSSIER_TRUST_BRANCH=fossier/vouch-prauthor" in env_contents
        assert "FOSSIER_TRUST_PR_TITLE=" in env_contents
        assert "FOSSIER_TRUST_PR_BODY=" in env_contents

        # Check comment
        api.post_or_update_comment.assert_called_once()
        body = api.post_or_update_comment.call_args[0][3]
        assert "Vouched and Approved" in body

    def test_adds_manual_approval_label(self, handler_setup, tmp_path):
        handler, config, api, _ = handler_setup("/fossier vouch")
        (tmp_path / "VOUCHED.td").write_text("# vouches\n")
        env_file = str(tmp_path / "github_env")
        with open(env_file, "w"):
            pass
        with patch.dict(os.environ, {"GITHUB_ENV": env_file}):
            handler.run()
        api.add_labels.assert_called_once_with(
            "owner", "repo", 42, [config.manual_approval_label]
        )

    def test_deletes_registry_report_when_configured(self, handler_setup, tmp_path):
        handler, config, api, _ = handler_setup("/fossier vouch")
        config.registry_url = "https://registry.example.com"
        config.registry_api_key = "test-key"

        (tmp_path / "VOUCHED.td").write_text("# vouches\n")
        env_file = str(tmp_path / "github_env")
        with open(env_file, "w"):
            pass

        mock_reg = MagicMock()
        with (
            patch.dict(os.environ, {"GITHUB_ENV": env_file}),
            patch(
                "fossier.registry_client.RegistryClient", return_value=mock_reg
            ) as reg_cls,
        ):
            handler.run()

        reg_cls.assert_called_once_with("https://registry.example.com", "test-key")
        mock_reg.delete_report.assert_called_once_with("prauthor", "owner", "repo")


class TestHandleReject:
    def test_reject_with_reason(self, handler_setup, tmp_path):
        handler, config, api, _ = handler_setup("/fossier reject SEO link spam")

        (tmp_path / "VOUCHED.td").write_text("# vouches\n")
        env_file = str(tmp_path / "github_env")
        with open(env_file, "w"):
            pass

        with patch.dict(os.environ, {"GITHUB_ENV": env_file}):
            result = handler.run()

        assert result == 0

        # Check VOUCHED.td was updated with denouncement
        content = (tmp_path / "VOUCHED.td").read_text()
        assert "- prauthor" in content
        assert "SEO link spam" in content

        api.close_pr.assert_called_once()
        api.add_labels.assert_called_once()

    def test_reject_without_reason_errors(self, handler_setup):
        handler, _, api, _ = handler_setup("/fossier reject")
        result = handler.run()
        # Should reply with error about missing reason
        # The -1 reaction comes from _reply_error, but the handler also
        # caught the error case before raising exception, so no +1
        api.post_comment.assert_called()
        body = api.post_comment.call_args[0][3]
        assert "reason is required" in body.lower()

    def test_reject_reports_to_registry(self, handler_setup, tmp_path):
        handler, config, api, _ = handler_setup("/fossier reject spam")
        config.registry_url = "https://registry.example.com"
        config.registry_api_key = "test-key"

        (tmp_path / "VOUCHED.td").write_text("# vouches\n")
        env_file = str(tmp_path / "github_env")
        with open(env_file, "w"):
            pass

        mock_reg = MagicMock()
        with (
            patch.dict(os.environ, {"GITHUB_ENV": env_file}),
            patch(
                "fossier.registry_client.RegistryClient", return_value=mock_reg
            ) as reg_cls,
        ):
            handler.run()

        reg_cls.assert_called_once_with("https://registry.example.com", "test-key")
        mock_reg.report_spam.assert_called_once()


class TestHandleCheck:
    def test_reruns_pipeline(self, handler_setup):
        handler, config, api, db = handler_setup("/fossier check")

        mock_decision = MagicMock()
        mock_decision.outcome = Outcome.ALLOW
        mock_decision.trust_tier = MagicMock(value="unknown")
        mock_decision.score_result = None
        mock_decision.pr_number = 42

        with (
            patch(
                "fossier.comment_commands.evaluate_contributor",
                return_value=mock_decision,
            ) as mock_eval,
            patch("fossier.comment_commands.execute_outcome") as mock_exec,
        ):
            result = handler.run()

        assert result == 0
        mock_eval.assert_called_once()
        mock_exec.assert_called_once()


class TestHandleScore:
    def test_posts_score_reply(self, handler_setup):
        handler, config, api, _ = handler_setup("/fossier score")

        mock_score = ScoreResult(
            total_score=65.0,
            confidence=0.9,
            signals=[SignalResult("account_age", 365, 0.8, 0.15)],
            outcome=Outcome.REVIEW,
        )

        with patch(
            "fossier.comment_commands.score_contributor", return_value=mock_score
        ):
            result = handler.run()

        assert result == 0
        # score posts a new comment, not an update
        api.post_comment.assert_called()
        body = api.post_comment.call_args[0][3]
        assert "Score Breakdown" in body


class TestHandleVouchAll:
    def test_vouches_all_contributors(self, handler_setup, tmp_path):
        handler, config, api, _ = handler_setup("/fossier vouch-all")
        api.get_contributors.return_value = ["alice", "bob", "charlie"]

        (tmp_path / "VOUCHED.td").write_text("# vouches\n+ alice\n")

        env_file = str(tmp_path / "github_env")
        with open(env_file, "w"):
            pass

        with patch.dict(os.environ, {"GITHUB_ENV": env_file}):
            result = handler.run()

        assert result == 0

        content = (tmp_path / "VOUCHED.td").read_text()
        assert "bob" in content
        assert "charlie" in content

        api.post_comment.assert_called()
        body = api.post_comment.call_args[0][3]
        assert "2 new" in body
        assert "1 already" in body

    def test_no_contributors_found(self, handler_setup):
        handler, _, api, _ = handler_setup("/fossier vouch-all")
        api.get_contributors.return_value = []
        result = handler.run()
        assert result == 0
        api.post_comment.assert_called()
        body = api.post_comment.call_args[0][3]
        assert "No contributors found" in body


# --- Action routing tests ---


class TestActionRouting:
    def test_issue_comment_routes_to_handler(self, tmp_path):
        from fossier.action import GithubAction

        event = _make_event("/fossier approve")
        event_path = tmp_path / "event.json"
        event_path.write_text(json.dumps(event))

        output_file = str(tmp_path / "github_output")
        with open(output_file, "w"):
            pass

        config = Config(
            repo_owner="owner",
            repo_name="repo",
            repo_root=tmp_path,
            db_path=str(tmp_path / "test.db"),
            github_token="test-token",
        )
        api = MagicMock()
        api.get_collaborator_permission.return_value = "write"
        api.get_pr.return_value = {
            "number": 42,
            "user": {"login": "prauthor"},
            "state": "open",
        }
        api.remove_label.return_value = True
        api.add_reaction.return_value = {}
        api.post_or_update_comment.return_value = {}

        env = {
            "GITHUB_EVENT_PATH": str(event_path),
            "GITHUB_EVENT_NAME": "issue_comment",
            "GITHUB_OUTPUT": output_file,
        }

        with (
            patch.dict(os.environ, env),
            patch("fossier.action.load_config", return_value=config),
            patch("fossier.action.GitHubAPI", return_value=api),
            patch("fossier.action.Database") as mock_db_cls,
        ):
            mock_db = MagicMock()
            mock_db_cls.return_value = mock_db

            action = GithubAction()
            result = action.run()

        assert result == 0
        api.add_reaction.assert_called()

    def test_issue_comment_on_issue_is_skipped(self, tmp_path):
        from fossier.action import GithubAction

        # No pull_request key in issue
        event = {
            "comment": {
                "body": "/fossier approve",
                "user": {"login": "user"},
                "id": 1,
            },
            "issue": {"number": 10},
        }
        event_path = tmp_path / "event.json"
        event_path.write_text(json.dumps(event))

        env = {
            "GITHUB_EVENT_PATH": str(event_path),
            "GITHUB_EVENT_NAME": "issue_comment",
        }
        with patch.dict(os.environ, env):
            action = GithubAction()
            result = action.run()

        assert result == 0

    def test_no_command_in_comment_is_skipped(self, tmp_path):
        from fossier.action import GithubAction

        event = _make_event("just a regular comment")
        event_path = tmp_path / "event.json"
        event_path.write_text(json.dumps(event))

        env = {
            "GITHUB_EVENT_PATH": str(event_path),
            "GITHUB_EVENT_NAME": "issue_comment",
        }
        with patch.dict(os.environ, env):
            action = GithubAction()
            result = action.run()

        assert result == 0

    def test_pr_event_still_works(self, tmp_path):
        from fossier.action import GithubAction

        event = {
            "pull_request": {
                "number": 42,
                "user": {"login": "testuser"},
            }
        }
        event_path = tmp_path / "event.json"
        event_path.write_text(json.dumps(event))

        output_file = str(tmp_path / "github_output")
        with open(output_file, "w"):
            pass

        config = Config(
            repo_owner="owner",
            repo_name="repo",
            repo_root=tmp_path,
            db_path=str(tmp_path / "test.db"),
            github_token="test-token",
            dry_run=True,
        )
        api = MagicMock()
        api.get_collaborators.return_value = []
        api.get_user.return_value = {
            "created_at": "2020-01-01T00:00:00Z",
            "public_repos": 10,
            "public_gists": 2,
            "followers": 5,
            "following": 10,
            "type": "User",
            "email": "user@example.com",
        }
        api.search_open_prs.return_value = 2
        api.search_closed_prs.return_value = 0
        api.search_prior_interaction.return_value = False
        api.get_pr_files.return_value = []
        api.get_user_orgs.return_value = []
        api.get_pr.return_value = None
        api.get_repo.return_value = {"stargazers_count": 50}
        api.get_pr_commits.return_value = []

        env = {
            "GITHUB_EVENT_PATH": str(event_path),
            "GITHUB_EVENT_NAME": "pull_request",
            "GITHUB_OUTPUT": output_file,
        }

        with (
            patch.dict(os.environ, env),
            patch("fossier.action.load_config", return_value=config),
            patch("fossier.action.GitHubAPI", return_value=api),
        ):
            action = GithubAction()
            result = action.run()

        assert result in (0, 1, 2)

    def test_pr_event_with_null_user_skips_without_action(self, tmp_path):
        # Ghost / suspended / deleted accounts surface as `user: null` in the
        # webhook payload. Fossier must not crash and must not auto-close
        # such PRs — they aren't the author's fault and there's nothing to
        # score. Returning 0 with no API calls ensures the PR is left alone.
        from fossier.action import GithubAction

        event = {
            "pull_request": {
                "number": 42,
                "user": None,
            }
        }
        event_path = tmp_path / "event.json"
        event_path.write_text(json.dumps(event))

        config = Config(
            repo_owner="owner",
            repo_name="repo",
            repo_root=tmp_path,
            db_path=str(tmp_path / "test.db"),
            github_token="test-token",
        )
        api = MagicMock()

        env = {
            "GITHUB_EVENT_PATH": str(event_path),
            "GITHUB_EVENT_NAME": "pull_request",
        }

        with (
            patch.dict(os.environ, env),
            patch("fossier.action.load_config", return_value=config),
            patch("fossier.action.GitHubAPI", return_value=api),
        ):
            action = GithubAction()
            result = action.run()

        assert result == 0
        api.close_pr.assert_not_called()
        api.add_labels.assert_not_called()
