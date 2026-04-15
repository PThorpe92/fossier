"""Client for the global fossier spam registry."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass

import httpx

logger = logging.getLogger(__name__)

MAX_RETRIES = 3
BACKOFF_BASE_SECONDS = 0.5
RETRYABLE_STATUS = {429, 500, 502, 503, 504}


@dataclass
class RegistryCheckResult:
    known: bool
    report_count: int


class RegistryClient:
    """HTTP client for the fossier global spam registry."""

    def __init__(self, base_url: str, api_key: str = ""):
        self.base_url = base_url.rstrip("/")
        self._client = httpx.Client(
            base_url=self.base_url,
            headers=self._build_headers(api_key),
            timeout=10.0,
        )

    def _build_headers(self, api_key: str) -> dict[str, str]:
        headers = {"Accept": "application/json"}
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        return headers

    def _request_with_retry(
        self, method: str, url: str, **kwargs
    ) -> httpx.Response | None:
        """Execute an HTTP request with exponential backoff on transient failures."""
        last_exc: Exception | None = None
        for attempt in range(MAX_RETRIES):
            try:
                response = self._client.request(method, url, **kwargs)
                if response.status_code not in RETRYABLE_STATUS:
                    return response
                # Retryable server error — honor Retry-After if present
                wait = self._get_wait(response, attempt)
                logger.debug(
                    "Registry returned %d, retrying in %.1fs (attempt %d/%d)",
                    response.status_code,
                    wait,
                    attempt + 1,
                    MAX_RETRIES,
                )
                time.sleep(wait)
            except httpx.HTTPError as e:
                last_exc = e
                wait = BACKOFF_BASE_SECONDS * (2**attempt)
                logger.debug(
                    "Registry request failed (%s), retrying in %.1fs (attempt %d/%d)",
                    e,
                    wait,
                    attempt + 1,
                    MAX_RETRIES,
                )
                time.sleep(wait)

        if last_exc:
            raise last_exc
        return response  # type: ignore[possibly-undefined]

    @staticmethod
    def _get_wait(response: httpx.Response, attempt: int) -> float:
        retry_after = response.headers.get("Retry-After")
        if retry_after:
            try:
                return min(float(retry_after), 30.0)
            except ValueError:
                pass
        return BACKOFF_BASE_SECONDS * (2**attempt)

    def check_username(self, username: str) -> RegistryCheckResult | None:
        """Check if a username is known spam in the global registry."""
        try:
            response = self._request_with_retry("GET", f"/api/check/{username}")
            if response and response.status_code == 200:
                data = response.json()
                return RegistryCheckResult(
                    known=data.get("known", False),
                    report_count=data.get("report_count", 0),
                )
            if response:
                logger.debug(
                    "Registry check returned %d for %s",
                    response.status_code,
                    username,
                )
        except httpx.HTTPError as e:
            logger.warning("Registry check failed for %s: %s", username, e)
        return None

    def report_spam(
        self,
        username: str,
        repo_owner: str,
        repo_name: str,
        score: float,
        reason: str,
        pr_number: int | None = None,
        signals: dict | None = None,
    ) -> bool:
        """Report a spam contributor to the global registry. Returns True on success."""
        payload = {
            "username": username,
            "repo_owner": repo_owner,
            "repo_name": repo_name,
            "score": score,
            "reason": reason,
        }
        if pr_number is not None:
            payload["pr_number"] = pr_number
        if signals:
            payload["signals"] = signals

        try:
            response = self._request_with_retry("POST", "/api/report", json=payload)
            if response and response.status_code in (200, 201):
                return True
            if response:
                logger.warning(
                    "Registry report failed with %d: %s",
                    response.status_code,
                    response.text[:200],
                )
        except httpx.HTTPError as e:
            logger.warning("Registry report failed: %s", e)
        return False

    def delete_report(
        self,
        username: str,
        repo_owner: str,
        repo_name: str,
    ) -> bool:
        """Remove this repo's spam report for a user.

        The registry authorizes the call with the API key, so only reports
        filed by the authenticated repo are deleted — reports from other
        repos are untouched. Intended for use when a maintainer overrides
        an auto-deny via /fossier approve or /fossier vouch.

        Returns True if a report was removed, False if there was nothing to
        delete or the call failed. Failures are logged and swallowed —
        callers should treat this as best-effort.
        """
        path = f"/api/report/{username}/{repo_owner}/{repo_name}"
        try:
            response = self._request_with_retry("DELETE", path)
            if response and response.status_code == 200:
                try:
                    data = response.json()
                except ValueError:
                    data = {}
                return bool(data.get("deleted", False))
            if response:
                logger.warning(
                    "Registry delete returned %d for %s",
                    response.status_code,
                    username,
                )
        except httpx.HTTPError as e:
            logger.warning("Registry delete failed for %s: %s", username, e)
        return False

    def close(self) -> None:
        self._client.close()
