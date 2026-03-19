"""GitHub API client with rate limiting and caching."""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timedelta, timezone

import httpx

from fossier.config import Config
from fossier.db import Database

logger = logging.getLogger(__name__)

API_BASE = "https://api.github.com"


class RateLimitError(Exception):
    def __init__(self, reset_at: float):
        self.reset_at = reset_at
        super().__init__(f"Rate limited until {reset_at}")


class GitHubAPI:
    def __init__(self, config: Config, db: Database):
        self.config = config
        self.db = db
        self._client = httpx.Client(
            base_url=API_BASE,
            headers=self._build_headers(),
            timeout=30.0,
        )
        self._rate_remaining: dict[str, int] = {"core": 5000, "search": 30}
        self._rate_reset: dict[str, float] = {"core": 0, "search": 0}

    def _build_headers(self) -> dict[str, str]:
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }
        if self.config.github_token:
            headers["Authorization"] = f"Bearer {self.config.github_token}"
        return headers

    def close(self) -> None:
        self._client.close()

    def _cache_ttl_for(self, path: str) -> int:
        """Return cache TTL in hours based on endpoint type."""
        if path.startswith("/search/"):
            return self.config.cache_ttl.search_hours
        if "/collaborators" in path:
            return self.config.cache_ttl.collaborators_hours
        return self.config.cache_ttl.user_profile_hours

    def _update_rate_limits(self, response: httpx.Response, pool: str) -> None:
        remaining = response.headers.get("x-ratelimit-remaining")
        reset = response.headers.get("x-ratelimit-reset")
        if remaining is not None:
            self._rate_remaining[pool] = int(remaining)
        if reset is not None:
            self._rate_reset[pool] = float(reset)

    def _check_rate_limit(self, pool: str) -> None:
        if self._rate_remaining.get(pool, 1) <= 0:
            reset = self._rate_reset.get(pool, 0)
            wait = reset - time.time()
            if wait > 0:
                raise RateLimitError(reset)

    def get(
        self, path: str, params: dict | None = None, pool: str = "core"
    ) -> dict | list | None:
        """GET request with caching, ETag support, and rate limiting."""
        cache_key = f"GET:{path}:{json.dumps(params or {}, sort_keys=True)}"

        # Check cache
        cached = self.db.get_cached(cache_key)
        etag = None
        if cached:
            logger.debug("Cache hit for %s", path)
            return cached["data"]
        if cached is None:
            # Check for expired cache to get etag for conditional request
            pass

        # Check rate limits
        self._check_rate_limit(pool)

        # Make request
        headers = {}
        if etag:
            headers["If-None-Match"] = etag

        try:
            response = self._client.get(path, params=params, headers=headers)
        except httpx.HTTPError as e:
            logger.error("HTTP error for %s: %s", path, e)
            return None

        self._update_rate_limits(response, pool)

        if response.status_code == 304 and cached:
            return cached["data"]

        if response.status_code == 403:
            reset = self._rate_reset.get(pool, 0)
            wait = reset - time.time()
            if wait > 0 and wait < 120:
                logger.warning("Rate limited, waiting %.0fs", wait)
                time.sleep(wait + 1)
                return self.get(path, params, pool)
            logger.error("Rate limited on %s, cannot retry", path)
            return None

        if response.status_code == 404:
            logger.debug("404 for %s", path)
            return None

        if response.status_code >= 400:
            logger.error(
                "API error %d for %s: %s",
                response.status_code,
                path,
                response.text[:200],
            )
            return None

        data = response.json()
        resp_etag = response.headers.get("etag")

        # Cache the response
        ttl_hours = self._cache_ttl_for(path)
        expires = datetime.now(timezone.utc) + timedelta(hours=ttl_hours)
        self.db.set_cached(
            cache_key=cache_key,
            response_json=json.dumps(data),
            expires_at=expires.strftime("%Y-%m-%d %H:%M:%S"),
            etag=resp_etag,
        )

        return data

    def post(self, path: str, json_data: dict) -> dict | None:
        """POST request (no caching)."""
        self._check_rate_limit("core")
        try:
            response = self._client.post(path, json=json_data)
        except httpx.HTTPError as e:
            logger.error("HTTP error for POST %s: %s", path, e)
            return None
        self._update_rate_limits(response, "core")
        if response.status_code >= 400:
            logger.error("API error %d for POST %s", response.status_code, path)
            return None
        return response.json()

    def patch(self, path: str, json_data: dict) -> dict | None:
        """PATCH request (no caching)."""
        self._check_rate_limit("core")
        try:
            response = self._client.patch(path, json=json_data)
        except httpx.HTTPError as e:
            logger.error("HTTP error for PATCH %s: %s", path, e)
            return None
        self._update_rate_limits(response, "core")
        if response.status_code >= 400:
            logger.error("API error %d for PATCH %s", response.status_code, path)
            return None
        return response.json()

    def get_user(self, username: str) -> dict | None:
        return self.get(f"/users/{username}")

    def get_collaborators(self, owner: str, repo: str) -> list[str]:
        data = self.get(f"/repos/{owner}/{repo}/collaborators")
        if not data or not isinstance(data, list):
            return []
        return [c["login"].lower() for c in data if "login" in c]

    def get_pr_files(self, owner: str, repo: str, pr_number: int) -> list[dict]:
        data = self.get(f"/repos/{owner}/{repo}/pulls/{pr_number}/files")
        if not data or not isinstance(data, list):
            return []
        return data

    def search_open_prs(self, username: str) -> int:
        """Count of open PRs by user across all repos."""
        data = self.get(
            "/search/issues",
            params={"q": f"author:{username} is:pr is:open", "per_page": "1"},
            pool="search",
        )
        if not data or not isinstance(data, dict):
            return -1
        return data.get("total_count", -1)

    def search_prior_interaction(self, owner: str, repo: str, username: str) -> bool:
        """Check if user has any prior issues/comments on this repo."""
        data = self.get(
            "/search/issues",
            params={"q": f"repo:{owner}/{repo} author:{username}", "per_page": "1"},
            pool="search",
        )
        if not data or not isinstance(data, dict):
            return False
        return data.get("total_count", 0) > 0

    def post_comment(
        self, owner: str, repo: str, pr_number: int, body: str
    ) -> dict | None:
        return self.post(
            f"/repos/{owner}/{repo}/issues/{pr_number}/comments",
            {"body": body},
        )

    def add_labels(
        self, owner: str, repo: str, pr_number: int, labels: list[str]
    ) -> dict | None:
        return self.post(
            f"/repos/{owner}/{repo}/issues/{pr_number}/labels",
            {"labels": labels},
        )

    def close_pr(self, owner: str, repo: str, pr_number: int) -> dict | None:
        return self.patch(
            f"/repos/{owner}/{repo}/pulls/{pr_number}",
            {"state": "closed"},
        )
