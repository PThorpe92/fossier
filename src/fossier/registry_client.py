"""Client for the global fossier spam registry."""

from __future__ import annotations

import logging
from dataclasses import dataclass

import httpx

logger = logging.getLogger(__name__)


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

    def check_username(self, username: str) -> RegistryCheckResult | None:
        """Check if a username is known spam in the global registry."""
        try:
            response = self._client.get(f"/check/{username}")
            if response.status_code == 200:
                data = response.json()
                return RegistryCheckResult(
                    known=data.get("known", False),
                    report_count=data.get("report_count", 0),
                )
            logger.debug("Registry check returned %d for %s", response.status_code, username)
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
            response = self._client.post("/report", json=payload)
            if response.status_code in (200, 201):
                return True
            logger.warning(
                "Registry report failed with %d: %s",
                response.status_code,
                response.text[:200],
            )
        except httpx.HTTPError as e:
            logger.warning("Registry report failed: %s", e)
        return False

    def close(self) -> None:
        self._client.close()
