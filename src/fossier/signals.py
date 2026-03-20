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


def is_bot_username(username: str) -> bool:
    """Check if a username matches known bot patterns."""
    return bool(_BOT_USERNAME_PATTERNS.search(username))

# Emoji Unicode ranges (common blocks)
_EMOJI_RE = re.compile(
    "["
    "\U0001f300-\U0001f9ff"  # Misc Symbols, Emoticons, Symbols & Pictographs, etc.
    "\U00002702-\U000027b0"  # Dingbats
    "\U0000fe00-\U0000fe0f"  # Variation Selectors
    "\U0001fa00-\U0001faff"  # Symbols and Pictographs Extended-A
    "\U00002600-\U000026ff"  # Misc Symbols
    "]"
)


def _is_emoji(ch: str) -> bool:
    return bool(_EMOJI_RE.match(ch))


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

    # Fetch user profile once — used by multiple signals
    user_profile = api.get_user(username)

    collectors = [
        ("account_age", _signal_account_age),
        ("public_repos", _signal_public_repos),
        ("contribution_history", _signal_contribution_history),
        ("follower_ratio", _signal_follower_ratio),
        ("bot_signals", _signal_bot),
        ("open_prs_elsewhere", _signal_open_prs),
        ("prior_interaction", _signal_prior_interaction),
        ("pr_content", _signal_pr_content),
        ("commit_email", _signal_commit_email),
        ("pr_description", _signal_pr_description),
        ("repo_stars", _signal_repo_stars),
        ("org_membership", _signal_org_membership),
        ("commit_verification", _signal_commit_verification),
    ]

    results = []
    for name, collector in collectors:
        weight = weights.get(name, 0.1)
        try:
            result = collector(
                api, username, repo_owner, repo_name, pr_number,
                user_profile=user_profile,
            )
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
    api: GitHubAPI, username: str, owner: str, repo: str, pr: int | None,
    *, user_profile: dict | None = None,
) -> SignalResult:
    user = user_profile
    if not user or "created_at" not in user:
        return SignalResult("account_age", 0, 0.0, 0, success=False, error="User not found")

    created = datetime.fromisoformat(user["created_at"].replace("Z", "+00:00"))
    days = (datetime.now(timezone.utc) - created).days
    normalized = min(days / 365, 1.0)
    return SignalResult("account_age", days, normalized, 0)


def _signal_public_repos(
    api: GitHubAPI, username: str, owner: str, repo: str, pr: int | None,
    *, user_profile: dict | None = None,
) -> SignalResult:
    user = user_profile
    if not user:
        return SignalResult("public_repos", 0, 0.0, 0, success=False, error="User not found")

    count = user.get("public_repos", 0)
    normalized = min(count / 20, 1.0)
    return SignalResult("public_repos", count, normalized, 0)


def _signal_contribution_history(
    api: GitHubAPI, username: str, owner: str, repo: str, pr: int | None,
    *, user_profile: dict | None = None,
) -> SignalResult:
    # GitHub doesn't expose contribution count via REST API directly.
    # Use public_repos + followers as a proxy, or just mark as unavailable.
    user = user_profile
    if not user:
        return SignalResult("contribution_history", 0, 0.0, 0, success=False, error="User not found")

    # Proxy: public_repos + public_gists as rough contribution indicator
    count = user.get("public_repos", 0) + user.get("public_gists", 0)
    normalized = min(count / 200, 1.0)
    return SignalResult("contribution_history", count, normalized, 0)


def _signal_follower_ratio(
    api: GitHubAPI, username: str, owner: str, repo: str, pr: int | None,
    *, user_profile: dict | None = None,
) -> SignalResult:
    user = user_profile
    if not user:
        return SignalResult("follower_ratio", 0, 0.0, 0, success=False, error="User not found")

    followers = user.get("followers", 0)
    following = max(user.get("following", 0), 1)
    ratio = followers / following
    normalized = min(ratio / 2.0, 1.0)
    return SignalResult("follower_ratio", round(ratio, 2), normalized, 0)


def _signal_bot(
    api: GitHubAPI, username: str, owner: str, repo: str, pr: int | None,
    *, user_profile: dict | None = None,
) -> SignalResult:
    user = user_profile
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
    api: GitHubAPI, username: str, owner: str, repo: str, pr: int | None,
    *, user_profile: dict | None = None,
) -> SignalResult:
    count = api.search_open_prs(username)
    if count < 0:
        return SignalResult("open_prs_elsewhere", 0, 0.5, 0, success=False, error="Search failed")

    normalized = max(1.0 - count / 15, 0.0)
    return SignalResult("open_prs_elsewhere", count, normalized, 0)


def _signal_prior_interaction(
    api: GitHubAPI, username: str, owner: str, repo: str, pr: int | None,
    *, user_profile: dict | None = None,
) -> SignalResult:
    has_interaction = api.search_prior_interaction(owner, repo, username)
    normalized = 1.0 if has_interaction else 0.0
    return SignalResult("prior_interaction", has_interaction, normalized, 0)


def _signal_pr_content(
    api: GitHubAPI, username: str, owner: str, repo: str, pr: int | None,
    *, user_profile: dict | None = None,
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


# Disposable email domain patterns
_DISPOSABLE_EMAIL_DOMAINS = {
    "mailinator.com", "guerrillamail.com", "tempmail.com", "throwaway.email",
    "10minutemail.com", "yopmail.com", "trashmail.com", "sharklasers.com",
    "guerrillamailblock.com", "grr.la", "dispostable.com",
}


def _signal_commit_email(
    api: GitHubAPI, username: str, owner: str, repo: str, pr: int | None,
    *, user_profile: dict | None = None,
) -> SignalResult:
    """Check if commit email matches GitHub profile. Disposable domains are suspicious."""
    user = user_profile
    if not user:
        return SignalResult("commit_email", 0, 0.0, 0, success=False, error="User not found")

    email = (user.get("email") or "").lower()
    if not email:
        # No public email - neutral signal
        return SignalResult("commit_email", "no_email", 0.5, 0)

    # Check for disposable email domains
    domain = email.split("@")[-1] if "@" in email else ""
    if domain in _DISPOSABLE_EMAIL_DOMAINS:
        return SignalResult("commit_email", email, 0.1, 0)

    # Has a real email set - positive signal
    return SignalResult("commit_email", email, 0.8, 0)


def _signal_pr_description(
    api: GitHubAPI, username: str, owner: str, repo: str, pr: int | None,
    *, user_profile: dict | None = None,
) -> SignalResult:
    """Analyze PR title/body. Empty descriptions and keyword stuffing are spam signals."""
    if pr is None:
        return SignalResult("pr_description", "no_pr", 0.5, 0, success=False, error="No PR number")

    pr_data = api.get_pr(owner, repo, pr)
    if not pr_data:
        return SignalResult("pr_description", "not_found", 0.5, 0, success=False, error="PR not found")

    title = pr_data.get("title", "")
    body = pr_data.get("body") or ""

    score = 0.5  # neutral baseline

    # Empty body is suspicious
    if not body.strip():
        score -= 0.3

    # Very short title
    if len(title) < 10:
        score -= 0.1

    # Substantive body
    if len(body) > 50:
        score += 0.2

    # Links in body (could be spam or could be legitimate references)
    link_count = body.lower().count("http")
    if link_count > 5:
        score -= 0.2  # excessive links
    elif link_count > 0:
        score += 0.1  # some references is good

    # Em dashes and emojis in PR text are AI-slop indicators
    text = title + " " + body
    has_em_dash = "\u2014" in text or "\u2013" in text
    emoji_count = sum(1 for ch in text if _is_emoji(ch))
    if has_em_dash:
        score -= 0.15
    if emoji_count > 3:
        score -= 0.15

    normalized = max(0.0, min(1.0, score))
    raw = {
        "title_len": len(title),
        "body_len": len(body),
        "link_count": link_count,
        "has_em_dash": has_em_dash,
        "emoji_count": emoji_count,
    }
    return SignalResult("pr_description", str(raw), normalized, 0)


def _signal_repo_stars(
    api: GitHubAPI, username: str, owner: str, repo: str, pr: int | None,
    *, user_profile: dict | None = None,
) -> SignalResult:
    """High-star repos attract more spam. Returns higher scrutiny for popular repos."""
    repo_data = api.get_repo(owner, repo)
    if not repo_data:
        return SignalResult("repo_stars", 0, 0.5, 0, success=False, error="Repo not found")

    stars = repo_data.get("stargazers_count", 0)
    # Higher star repos should increase scrutiny (lower normalized = more suspicious context)
    # 0 stars -> 1.0, 1000+ stars -> 0.3 (floor)
    normalized = max(0.3, 1.0 - (stars / 1500))
    return SignalResult("repo_stars", stars, round(normalized, 3), 0)


def _signal_org_membership(
    api: GitHubAPI, username: str, owner: str, repo: str, pr: int | None,
    *, user_profile: dict | None = None,
) -> SignalResult:
    """Users belonging to GitHub orgs are less likely to be spam."""
    orgs = api.get_user_orgs(username)
    # -1 would indicate an error in the list case, but empty list is valid
    count = len(orgs)
    if count > 0:
        normalized = min(count / 3, 1.0)  # 3+ orgs = max trust
    else:
        normalized = 0.2  # no orgs = slight negative but not strongly
    return SignalResult("org_membership", count, normalized, 0)


def _signal_commit_verification(
    api: GitHubAPI, username: str, owner: str, repo: str, pr: int | None,
    *, user_profile: dict | None = None,
) -> SignalResult:
    """Check if PR commits are GPG/SSH signed. Signed commits indicate higher trust."""
    if pr is None:
        return SignalResult("commit_verification", "no_pr", 0.5, 0, success=False, error="No PR number")

    commits = api.get_pr_commits(owner, repo, pr)
    if not commits:
        return SignalResult("commit_verification", "no_commits", 0.5, 0, success=False, error="No commits found")

    total = len(commits)
    verified = sum(
        1 for c in commits
        if c.get("commit", {}).get("verification", {}).get("verified", False)
    )

    ratio = verified / total
    # All signed = 1.0, none signed = 0.3 (not signing isn't strongly negative)
    normalized = 0.3 + (ratio * 0.7)
    raw = {"total_commits": total, "verified": verified, "ratio": round(ratio, 2)}
    return SignalResult("commit_verification", str(raw), round(normalized, 3), 0)
