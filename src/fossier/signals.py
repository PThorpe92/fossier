"""Individual signal implementations for spam scoring.

Each signal normalizes to 0.0-1.0, where 1.0 = trustworthy.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timezone

from fossier.github_api import GitHubAPI
from fossier.models import SignalResult

logger = logging.getLogger(__name__)

# Code file extensions
_CODE_EXTS = {
    ".py", ".rs", ".go", ".js", ".ts", ".jsx", ".tsx", ".c", ".cpp", ".h",
    ".java", ".kt", ".rb", ".php", ".swift", ".cs", ".scala", ".zig", ".hs",
    ".ex", ".exs", ".clj", ".lua", ".sh", ".bash", ".zsh", ".fish", ".pl",
    ".r", ".jl", ".nim", ".v", ".d", ".ml", ".mli", ".fs", ".fsi",
}
_TEST_PATTERNS = re.compile(r"(test_|_test\.|\.test\.|tests/|spec/|__tests__/)")
_DOC_PATTERNS = re.compile(
    r"(readme|changelog|contributing|license|\.md$|\.rst$|\.txt$|docs/)",
    re.IGNORECASE,
)
_BOT_USERNAME_PATTERNS = re.compile(
    r"(\[bot\]$|bot$|-bot$|^dependabot|^renovate|^greenkeeper|^snyk-)",
    re.IGNORECASE,
)


def collect_signals(
    api: GitHubAPI,
    username: str,
    repo_owner: str,
    repo_name: str,
    pr_number: int | None = None,
    weights: dict[str, float] | None = None,
) -> list[SignalResult]:
    """Collect all signals for a user. Returns list of SignalResult."""
    weights = weights or {}

    collectors = [
        ("account_age", _signal_account_age),
        ("public_repos", _signal_public_repos),
        ("contribution_history", _signal_contribution_history),
        ("follower_ratio", _signal_follower_ratio),
        ("bot_signals", _signal_bot),
        ("open_prs_elsewhere", _signal_open_prs),
        ("prior_interaction", _signal_prior_interaction),
        ("pr_content", _signal_pr_content),
    ]

    results = []
    for name, collector in collectors:
        weight = weights.get(name, 0.1)
        try:
            result = collector(api, username, repo_owner, repo_name, pr_number)
            result.weight = weight
            results.append(result)
        except Exception as e:
            logger.warning("Signal %s failed: %s", name, e)
            results.append(SignalResult(
                name=name,
                raw_value=0,
                normalized=0.0,
                weight=weight,
                success=False,
                error=str(e),
            ))

    return results


def _signal_account_age(
    api: GitHubAPI, username: str, owner: str, repo: str, pr: int | None
) -> SignalResult:
    user = api.get_user(username)
    if not user or "created_at" not in user:
        return SignalResult("account_age", 0, 0.0, 0, success=False, error="User not found")

    created = datetime.fromisoformat(user["created_at"].replace("Z", "+00:00"))
    days = (datetime.now(timezone.utc) - created).days
    normalized = min(days / 365, 1.0)
    return SignalResult("account_age", days, normalized, 0)


def _signal_public_repos(
    api: GitHubAPI, username: str, owner: str, repo: str, pr: int | None
) -> SignalResult:
    user = api.get_user(username)
    if not user:
        return SignalResult("public_repos", 0, 0.0, 0, success=False, error="User not found")

    count = user.get("public_repos", 0)
    normalized = min(count / 20, 1.0)
    return SignalResult("public_repos", count, normalized, 0)


def _signal_contribution_history(
    api: GitHubAPI, username: str, owner: str, repo: str, pr: int | None
) -> SignalResult:
    # GitHub doesn't expose contribution count via REST API directly.
    # Use public_repos + followers as a proxy, or just mark as unavailable.
    user = api.get_user(username)
    if not user:
        return SignalResult("contribution_history", 0, 0.0, 0, success=False, error="User not found")

    # Proxy: public_repos + public_gists as rough contribution indicator
    count = user.get("public_repos", 0) + user.get("public_gists", 0)
    normalized = min(count / 200, 1.0)
    return SignalResult("contribution_history", count, normalized, 0)


def _signal_follower_ratio(
    api: GitHubAPI, username: str, owner: str, repo: str, pr: int | None
) -> SignalResult:
    user = api.get_user(username)
    if not user:
        return SignalResult("follower_ratio", 0, 0.0, 0, success=False, error="User not found")

    followers = user.get("followers", 0)
    following = max(user.get("following", 0), 1)
    ratio = followers / following
    normalized = min(ratio / 2.0, 1.0)
    return SignalResult("follower_ratio", round(ratio, 2), normalized, 0)


def _signal_bot(
    api: GitHubAPI, username: str, owner: str, repo: str, pr: int | None
) -> SignalResult:
    user = api.get_user(username)
    if not user:
        return SignalResult("bot_signals", 0, 0.5, 0, success=False, error="User not found")

    is_bot = False
    if user.get("type", "").lower() == "bot":
        is_bot = True
    if _BOT_USERNAME_PATTERNS.search(username):
        is_bot = True

    normalized = 0.0 if is_bot else 1.0
    return SignalResult("bot_signals", is_bot, normalized, 0)


def _signal_open_prs(
    api: GitHubAPI, username: str, owner: str, repo: str, pr: int | None
) -> SignalResult:
    count = api.search_open_prs(username)
    if count < 0:
        return SignalResult("open_prs_elsewhere", 0, 0.0, 0, success=False, error="Search failed")

    normalized = max(1.0 - count / 15, 0.0)
    return SignalResult("open_prs_elsewhere", count, normalized, 0)


def _signal_prior_interaction(
    api: GitHubAPI, username: str, owner: str, repo: str, pr: int | None
) -> SignalResult:
    has_interaction = api.search_prior_interaction(owner, repo, username)
    normalized = 1.0 if has_interaction else 0.0
    return SignalResult("prior_interaction", has_interaction, normalized, 0)


def _signal_pr_content(
    api: GitHubAPI, username: str, owner: str, repo: str, pr: int | None
) -> SignalResult:
    if pr is None:
        return SignalResult("pr_content", "no_pr", 0.5, 0, success=False, error="No PR number")

    files = api.get_pr_files(owner, repo, pr)
    if not files:
        return SignalResult("pr_content", "no_files", 0.5, 0, success=False, error="No files found")

    filenames = [f.get("filename", "") for f in files]
    total_changes = sum(f.get("additions", 0) + f.get("deletions", 0) for f in files)

    score = 1.0

    # Check if only docs/README files
    all_docs = all(_DOC_PATTERNS.search(fn) for fn in filenames)
    if all_docs:
        score -= 0.6

    if total_changes < 5:
        score -= 0.2

    if len(filenames) == 1:
        score -= 0.1

    # Positive signals
    has_code = any(
        any(fn.endswith(ext) for ext in _CODE_EXTS) for fn in filenames
    )
    if has_code:
        score += 0.15

    has_tests = any(_TEST_PATTERNS.search(fn) for fn in filenames)
    if has_tests:
        score += 0.1

    normalized = max(0.0, min(1.0, score))
    raw = {
        "files": len(filenames),
        "total_changes": total_changes,
        "all_docs": all_docs,
        "has_code": has_code,
        "has_tests": has_tests,
    }
    return SignalResult("pr_content", str(raw), normalized, 0)
