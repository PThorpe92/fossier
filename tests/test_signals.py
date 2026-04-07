"""Tests for individual signal implementations."""

from unittest.mock import MagicMock

from fossier.signals import (
    _signal_account_age,
    _signal_activity_velocity,
    _signal_bot,
    _signal_closed_prs,
    _signal_commit_verification,
    _signal_contributor_stars,
    _signal_follower_ratio,
    _signal_merged_prs,
    _signal_open_prs,
    _signal_pr_content,
    _signal_pr_description,
    _signal_prior_interaction,
    _signal_public_repos,
    collect_signals,
)


def _mock_api(
    user_data=None,
    pr_files=None,
    search_prs=-1,
    closed_prs=0,
    merged_prs=0,
    prior=0,
    recent_items=0,
):
    api = MagicMock()
    api.get_user.return_value = user_data
    api.get_pr_files.return_value = pr_files or []
    api.search_open_prs.return_value = search_prs
    api.search_closed_prs.return_value = closed_prs
    api.search_merged_prs.return_value = merged_prs
    api.search_prior_interaction.return_value = prior
    api.count_recent_items.return_value = recent_items
    return api


def test_account_age_old_account():
    user = {
        "created_at": "2020-01-01T00:00:00Z",
        "public_repos": 10,
        "followers": 5,
        "public_gists": 0,
    }
    api = _mock_api(user_data=user)
    result = _signal_account_age(api, "user", "o", "r", None, user_profile=user)
    assert result.success
    assert result.normalized > 0.5


def test_account_age_new_account():
    from datetime import datetime, timedelta, timezone

    recent = (datetime.now(timezone.utc) - timedelta(days=7)).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )
    user = {"created_at": recent, "public_repos": 1, "followers": 0, "public_gists": 0}
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
    assert result.normalized == 0.5


def test_open_prs_few():
    api = _mock_api(search_prs=2)
    result = _signal_open_prs(api, "user", "o", "r", None)
    assert result.normalized > 0.7


def test_open_prs_many():
    api = _mock_api(search_prs=20)
    result = _signal_open_prs(api, "user", "o", "r", None)
    assert result.normalized == 0.0


def test_open_prs_search_failed():
    api = _mock_api(search_prs=-1)
    result = _signal_open_prs(api, "user", "o", "r", None)
    assert not result.success
    assert result.normalized == 0.5  # neutral, not penalizing


def test_prior_interaction_many():
    api = _mock_api(prior=5)
    result = _signal_prior_interaction(api, "user", "o", "r", None)
    assert result.normalized == 1.0


def test_prior_interaction_one():
    api = _mock_api(prior=1)
    result = _signal_prior_interaction(api, "user", "o", "r", None)
    assert 0.3 <= result.normalized <= 0.4


def test_prior_interaction_none():
    api = _mock_api(prior=0)
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
        merged_prs=5,
        prior=3,
        recent_items=1,
    )
    api.get_user_orgs.return_value = ["some-org"]
    api.get_pr.return_value = {"title": "Add feature", "body": "Some description"}
    api.get_repo.return_value = {"stargazers_count": 100}
    api.get_pr_commits.return_value = [
        {"commit": {"verification": {"verified": True}}},
    ]
    api.get_user_repos.return_value = [{"stargazers_count": 10}]
    results = collect_signals(api, "user", "o", "r")
    assert len(results) == 17
    names = {r.name for r in results}
    assert "account_age" in names
    assert "bot_signals" in names
    assert "closed_prs_elsewhere" in names
    assert "merged_prs_elsewhere" in names
    assert "activity_velocity" in names
    assert "commit_email" in names
    assert "pr_description" in names
    assert "repo_stars" in names
    assert "org_membership" in names
    assert "commit_verification" in names
    assert "contributor_stars" in names


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
        merged_prs=5,
        prior=3,
        recent_items=1,
    )
    api.get_user_orgs.return_value = ["some-org"]
    api.get_pr.return_value = {"title": "Add feature", "body": "Some description"}
    api.get_repo.return_value = {"stargazers_count": 100}
    api.get_pr_commits.return_value = [
        {"commit": {"verification": {"verified": True}}},
    ]
    api.get_user_repos.return_value = [{"stargazers_count": 10}]
    collect_signals(api, "user", "o", "r")
    assert api.get_user.call_count == 1


def test_pr_description_em_dash_penalty():
    """Em dashes in PR description should reduce score (AI-slop indicator)."""
    api = _mock_api()
    api.get_pr.return_value = {
        "title": "Refactor authentication module",
        "body": "This PR refactors the auth module \u2014 improving clarity and performance.",
    }
    result = _signal_pr_description(api, "user", "o", "r", 1)
    assert result.success
    # Should be penalized vs a clean description
    api2 = _mock_api()
    api2.get_pr.return_value = {
        "title": "Refactor authentication module",
        "body": "This PR refactors the auth module, improving clarity and performance.",
    }
    clean_result = _signal_pr_description(api2, "user", "o", "r", 1)
    assert result.normalized < clean_result.normalized


def test_pr_description_emoji_penalty():
    """Excessive emojis in PR description should reduce score."""
    api = _mock_api()
    api.get_pr.return_value = {
        "title": "Fix bug \U0001f41b",
        "body": "Fixed the issue \u2728\U0001f680\U0001f389\U0001f4af great improvement!",
    }
    result = _signal_pr_description(api, "user", "o", "r", 1)
    assert result.success
    # Few emojis (<=3) should not be penalized
    api2 = _mock_api()
    api2.get_pr.return_value = {
        "title": "Fix bug",
        "body": "Fixed the issue, this is a great improvement for users!",
    }
    clean_result = _signal_pr_description(api2, "user", "o", "r", 1)
    assert result.normalized < clean_result.normalized


def test_closed_prs_none():
    api = _mock_api(closed_prs=0)
    result = _signal_closed_prs(api, "user", "o", "r", None)
    assert result.success
    assert result.normalized == 1.0


def test_closed_prs_few():
    api = _mock_api(closed_prs=3)
    result = _signal_closed_prs(api, "user", "o", "r", None)
    assert result.success
    assert result.normalized == 0.7


def test_closed_prs_many():
    api = _mock_api(closed_prs=15)
    result = _signal_closed_prs(api, "user", "o", "r", None)
    assert result.success
    assert result.normalized == 0.0


def test_closed_prs_search_failed():
    api = _mock_api(closed_prs=-1)
    result = _signal_closed_prs(api, "user", "o", "r", None)
    assert not result.success
    assert result.normalized == 0.5


def test_contributor_stars_many():
    api = _mock_api()
    api.get_user_repos.return_value = [
        {"stargazers_count": 30},
        {"stargazers_count": 25},
    ]
    result = _signal_contributor_stars(api, "user", "o", "r", None)
    assert result.success
    assert result.normalized == 1.0


def test_contributor_stars_few():
    api = _mock_api()
    api.get_user_repos.return_value = [
        {"stargazers_count": 5},
    ]
    result = _signal_contributor_stars(api, "user", "o", "r", None)
    assert result.success
    assert result.normalized == 0.1


def test_contributor_stars_none():
    api = _mock_api()
    api.get_user_repos.return_value = []
    result = _signal_contributor_stars(api, "user", "o", "r", None)
    assert result.success
    assert result.normalized == 0.2


def test_commit_verification_signed_but_unverified():
    """Commits signed with an unknown key should get partial credit, not zero."""
    api = _mock_api()
    api.get_pr_commits.return_value = [
        {"commit": {"verification": {"verified": False, "reason": "unknown_key"}}},
    ]
    result = _signal_commit_verification(api, "user", "o", "r", 1)
    assert result.success
    # Should get partial credit (0.5 effective), not treated as unsigned
    assert result.normalized > 0.3  # > floor for unsigned
    assert result.normalized < 1.0  # < fully verified


def test_commit_verification_fully_verified():
    api = _mock_api()
    api.get_pr_commits.return_value = [
        {"commit": {"verification": {"verified": True, "reason": "valid"}}},
    ]
    result = _signal_commit_verification(api, "user", "o", "r", 1)
    assert result.success
    assert result.normalized == 1.0


def test_commit_verification_unsigned():
    api = _mock_api()
    api.get_pr_commits.return_value = [
        {"commit": {"verification": {"verified": False, "reason": "unsigned"}}},
    ]
    result = _signal_commit_verification(api, "user", "o", "r", 1)
    assert result.success
    assert result.normalized == 0.3  # floor for no signing


# --- New signal tests ---


def test_merged_prs_many():
    api = _mock_api(merged_prs=10)
    result = _signal_merged_prs(api, "user", "o", "r", None)
    assert result.success
    assert result.normalized == 1.0


def test_merged_prs_few():
    api = _mock_api(merged_prs=2)
    result = _signal_merged_prs(api, "user", "o", "r", None)
    assert result.success
    assert 0.4 < result.normalized < 0.6  # 0.2 + 2*0.16 = 0.52


def test_merged_prs_none():
    api = _mock_api(merged_prs=0)
    result = _signal_merged_prs(api, "user", "o", "r", None)
    assert result.success
    assert result.normalized == 0.2


def test_merged_prs_search_failed():
    api = _mock_api(merged_prs=-1)
    result = _signal_merged_prs(api, "user", "o", "r", None)
    assert not result.success
    assert result.normalized == 0.5


def test_activity_velocity_low():
    api = _mock_api(recent_items=1)
    result = _signal_activity_velocity(api, "user", "o", "r", None)
    assert result.success
    assert result.normalized == 1.0


def test_activity_velocity_moderate():
    api = _mock_api(recent_items=3)
    result = _signal_activity_velocity(api, "user", "o", "r", None)
    assert result.success
    assert result.normalized == 0.5


def test_activity_velocity_high():
    api = _mock_api(recent_items=8)
    result = _signal_activity_velocity(api, "user", "o", "r", None)
    assert result.success
    assert result.normalized == 0.0


def test_account_age_old_empty():
    """Old account with zero activity should be penalized."""
    user = {
        "created_at": "2020-01-01T00:00:00Z",
        "public_repos": 0,
        "followers": 0,
        "public_gists": 0,
    }
    api = _mock_api(user_data=user)
    result = _signal_account_age(api, "user", "o", "r", None, user_profile=user)
    assert result.success
    # activity_factor = 0.3, so normalized = base * 0.3
    assert result.normalized < 0.4


def test_account_age_old_active():
    """Old account with real activity should get full credit."""
    user = {
        "created_at": "2020-01-01T00:00:00Z",
        "public_repos": 20,
        "followers": 10,
        "public_gists": 0,
    }
    api = _mock_api(user_data=user)
    result = _signal_account_age(api, "user", "o", "r", None, user_profile=user)
    assert result.success
    assert result.normalized > 0.9


def test_account_age_old_sparse():
    """Old account with very sparse activity (2 repos, 0 followers)."""
    user = {
        "created_at": "2025-05-01T00:00:00Z",
        "public_repos": 2,
        "followers": 0,
        "public_gists": 0,
    }
    api = _mock_api(user_data=user)
    result = _signal_account_age(api, "user", "o", "r", None, user_profile=user)
    assert result.success
    # ~349 days, activity=2, factor=0.6 -> base*0.6 ~ 0.57
    assert result.normalized < 0.7
