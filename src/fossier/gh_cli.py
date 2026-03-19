"""GitHub CLI (gh) integration: fallback data source when REST API fails.

Uses `gh` CLI commands which have the user's full auth context,
handle private repos, and avoid search API permission issues.
"""

from __future__ import annotations

import json
import logging
import shutil
import subprocess

logger = logging.getLogger(__name__)


def is_available() -> bool:
    """Check if `gh` CLI is installed and authenticated."""
    return shutil.which("gh") is not None


def get_auth_token() -> str | None:
    """Get the token from `gh auth token` for use with REST API calls."""
    try:
        result = subprocess.run(
            ["gh", "auth", "token"],
            capture_output=True,
            text=True,
            timeout=5,
        )
        if result.returncode == 0:
            return result.stdout.strip()
    except (FileNotFoundError, subprocess.TimeoutExpired):
        pass
    return None


def api_get(path: str, params: dict | None = None) -> dict | list | None:
    """Call `gh api` for a raw REST endpoint. Returns parsed JSON or None."""
    cmd = ["gh", "api", path, "--header", "Accept: application/vnd.github+json"]
    for key, value in (params or {}).items():
        cmd.extend(["-f", f"{key}={value}"])
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            logger.debug("gh api %s failed: %s", path, result.stderr.strip()[:200])
            return None
        return json.loads(result.stdout)
    except (FileNotFoundError, subprocess.TimeoutExpired, json.JSONDecodeError) as e:
        logger.debug("gh api %s error: %s", path, e)
        return None


def search_open_prs(username: str) -> int:
    """Count open PRs by user using `gh search prs`."""
    try:
        result = subprocess.run(
            [
                "gh",
                "search",
                "prs",
                "--author",
                username,
                "--state",
                "open",
                "--json",
                "number",
                "--limit",
                "100",
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode != 0:
            return -1
        data = json.loads(result.stdout)
        return len(data) if isinstance(data, list) else -1
    except (FileNotFoundError, subprocess.TimeoutExpired, json.JSONDecodeError):
        return -1


def search_prior_interaction(owner: str, repo: str, username: str) -> bool:
    """Check if user has prior issues/PRs/comments on this repo using `gh`."""
    # Check for PRs by the user
    try:
        result = subprocess.run(
            [
                "gh",
                "search",
                "prs",
                "--repo",
                f"{owner}/{repo}",
                "--author",
                username,
                "--json",
                "number",
                "--limit",
                "1",
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode == 0:
            data = json.loads(result.stdout)
            if isinstance(data, list) and len(data) > 0:
                return True
    except (FileNotFoundError, subprocess.TimeoutExpired, json.JSONDecodeError):
        pass

    # Check for issues by the user
    try:
        result = subprocess.run(
            [
                "gh",
                "search",
                "issues",
                "--repo",
                f"{owner}/{repo}",
                "--author",
                username,
                "--json",
                "number",
                "--limit",
                "1",
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )
        if result.returncode == 0:
            data = json.loads(result.stdout)
            if isinstance(data, list) and len(data) > 0:
                return True
    except (FileNotFoundError, subprocess.TimeoutExpired, json.JSONDecodeError):
        pass

    return False


def get_collaborators(owner: str, repo: str) -> list[str] | None:
    """Get repo collaborators via `gh api`. Returns None if gh unavailable."""
    data = api_get(f"/repos/{owner}/{repo}/collaborators", {"per_page": "100"})
    if not data or not isinstance(data, list):
        return None
    return [c["login"].lower() for c in data if "login" in c]


def get_repo(owner: str, repo: str) -> dict | None:
    """Get repo info via `gh api`."""
    return api_get(f"/repos/{owner}/{repo}")
