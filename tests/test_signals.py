"""Tests for individual signal implementations."""

from unittest.mock import MagicMock

from fossier.signals import (
    _signal_account_age,
    _signal_bot,
    _signal_follower_ratio,
    _signal_open_prs,
    _signal_pr_content,
    _signal_prior_interaction,
    _signal_public_repos,
    collect_signals,
)


def _mock_api(user_data=None, pr_files=None, search_prs=-1, prior=False):
    api = MagicMock()
    api.get_user.return_value = user_data
    api.get_pr_files.return_value = pr_files or []
    api.search_open_prs.return_value = search_prs
    api.search_prior_interaction.return_value = prior
    return api


def test_account_age_old_account():
    user = {"created_at": "2020-01-01T00:00:00Z"}
    api = _mock_api(user_data=user)
    result = _signal_account_age(api, "user", "o", "r", None, user_profile=user)
    assert result.success
    assert result.normalized > 0.5


def test_account_age_new_account():
    user = {"created_at": "2026-03-01T00:00:00Z"}
    api = _mock_api(user_data=user)
    result = _signal_account_age(api, "user", "o", "r", None, user_profile=user)
    assert result.success
    assert result.normalized < 0.1


def test_account_age_user_not_found():
    api = _mock_api(user_data=None)
    result = _signal_account_age(api, "ghost", "o", "r", None, user_profile=None)
    assert not result.success


def test_public_repos_many():
    user = {"public_repos": 30}
    api = _mock_api(user_data=user)
    result = _signal_public_repos(api, "user", "o", "r", None, user_profile=user)
    assert result.normalized == 1.0


def test_public_repos_few():
    user = {"public_repos": 2}
    api = _mock_api(user_data=user)
    result = _signal_public_repos(api, "user", "o", "r", None, user_profile=user)
    assert result.normalized == 0.1


def test_follower_ratio_high():
    user = {"followers": 100, "following": 10}
    api = _mock_api(user_data=user)
    result = _signal_follower_ratio(api, "user", "o", "r", None, user_profile=user)
    assert result.normalized == 1.0


def test_follower_ratio_low():
    user = {"followers": 0, "following": 50}
    api = _mock_api(user_data=user)
    result = _signal_follower_ratio(api, "user", "o", "r", None, user_profile=user)
    assert result.normalized == 0.0


def test_bot_detection_bot_type():
    user = {"type": "Bot"}
    api = _mock_api(user_data=user)
    result = _signal_bot(api, "dependabot[bot]", "o", "r", None, user_profile=user)
    assert result.normalized == 0.0


def test_bot_detection_human():
    user = {"type": "User"}
    api = _mock_api(user_data=user)
    result = _signal_bot(api, "alice", "o", "r", None, user_profile=user)
    assert result.normalized == 1.0


def test_open_prs_few():
    api = _mock_api(search_prs=2)
    result = _signal_open_prs(api, "user", "o", "r", None)
    assert result.normalized > 0.8


def test_open_prs_many():
    api = _mock_api(search_prs=20)
    result = _signal_open_prs(api, "user", "o", "r", None)
    assert result.normalized == 0.0


def test_open_prs_search_failed():
    api = _mock_api(search_prs=-1)
    result = _signal_open_prs(api, "user", "o", "r", None)
    assert not result.success
    assert result.normalized == 0.5  # neutral, not penalizing


def test_prior_interaction_yes():
    api = _mock_api(prior=True)
    result = _signal_prior_interaction(api, "user", "o", "r", None)
    assert result.normalized == 1.0


def test_prior_interaction_no():
    api = _mock_api(prior=False)
    result = _signal_prior_interaction(api, "user", "o", "r", None)
    assert result.normalized == 0.0


def test_pr_content_code_changes():
    files = [
        {"filename": "src/main.py", "additions": 20, "deletions": 5},
        {"filename": "tests/test_main.py", "additions": 15, "deletions": 0},
    ]
    api = _mock_api(pr_files=files)
    result = _signal_pr_content(api, "user", "o", "r", 1)
    assert result.success
    assert result.normalized > 0.8  # code + tests = high score


def test_pr_content_docs_only():
    files = [
        {"filename": "README.md", "additions": 2, "deletions": 1},
    ]
    api = _mock_api(pr_files=files)
    result = _signal_pr_content(api, "user", "o", "r", 1)
    assert result.success
    assert result.normalized < 0.3  # docs only + small change + single file


def test_pr_content_no_pr():
    api = _mock_api()
    result = _signal_pr_content(api, "user", "o", "r", None)
    assert not result.success


def test_collect_signals_returns_all():
    user_data = {
        "created_at": "2020-01-01T00:00:00Z",
        "public_repos": 10,
        "public_gists": 2,
        "followers": 5,
        "following": 10,
        "type": "User",
        "email": "user@example.com",
    }
    api = _mock_api(
        user_data=user_data,
        search_prs=3,
        prior=True,
    )
    api.get_user_orgs.return_value = ["some-org"]
    api.get_pr.return_value = {"title": "Add feature", "body": "Some description"}
    api.get_repo.return_value = {"stargazers_count": 100}
    api.get_pr_commits.return_value = [
        {"commit": {"verification": {"verified": True}}},
    ]
    results = collect_signals(api, "user", "o", "r")
    assert len(results) == 13
    names = {r.name for r in results}
    assert "account_age" in names
    assert "bot_signals" in names
    assert "commit_email" in names
    assert "pr_description" in names
    assert "repo_stars" in names
    assert "org_membership" in names
    assert "commit_verification" in names


def test_collect_signals_fetches_user_once():
    """User profile should be fetched exactly once, not per-signal."""
    user_data = {
        "created_at": "2020-01-01T00:00:00Z",
        "public_repos": 10,
        "public_gists": 2,
        "followers": 5,
        "following": 10,
        "type": "User",
        "email": "user@example.com",
    }
    api = _mock_api(
        user_data=user_data,
        search_prs=3,
        prior=True,
    )
    api.get_user_orgs.return_value = ["some-org"]
    api.get_pr.return_value = {"title": "Add feature", "body": "Some description"}
    api.get_repo.return_value = {"stargazers_count": 100}
    api.get_pr_commits.return_value = [
        {"commit": {"verification": {"verified": True}}},
    ]
    collect_signals(api, "user", "o", "r")
    assert api.get_user.call_count == 1
