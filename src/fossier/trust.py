from __future__ import annotations

import logging
from pathlib import Path

from fossier.codeowners import parse_codeowners
from fossier.config import Config
from fossier.db import Database
from fossier.github_api import GitHubAPI
from fossier.models import TrustTier
from fossier.trustdown import TrustDown, parse_vouched

logger = logging.getLogger(__name__)


class TrustResolver:
    """Resolves a contributor's trust tier based on multiple sources:
    - Config blocked_users and trusted_users
    - VOUCHED.td denounced and vouched lists
    - CODEOWNERS file
    - GitHub collaborators API
    - Database of previous contributors
    """

    def __init__(self, config: Config, db: Database, api: GitHubAPI):
        self.config = config
        self.db = db
        self.api = api
        self.repo_root = config.repo_root
        self.td = parse_vouched(self.repo_root)

    def resolve_tier(self, username: str) -> tuple[TrustTier, str]:
        """Resolve a contributor's trust tier. Returns (tier, reason)."""
        username_lower = username.lower()
        repo_root = self.config.repo_root

        # Parse VOUCHED.td once for both blocked and trusted checks
        td = parse_vouched(repo_root)

        # 1. BLOCKED - checked first so denounced users can't be elevated
        tier, reason = self._check_blocked(username_lower, td)
        if tier:
            return tier, reason

        # 2. TRUSTED
        tier, reason = self._check_trusted(username_lower, repo_root, td)
        if tier:
            return tier, reason

        # 3. KNOWN - previous contributors in DB
        tier, reason = self._check_known(username_lower)
        if tier:
            return tier, reason

        # 4. UNKNOWN
        return TrustTier.UNKNOWN, "No prior trust relationship found"

    def _check_blocked(
        self, username: str, td: TrustDown
    ) -> tuple[TrustTier | None, str]:
        # Config blocked list
        if username in self.config.blocked_users:
            return TrustTier.BLOCKED, "Listed in config blocked_users"

        # VOUCHED.td denouncements
        if username in td.denounced:
            reason = td.denounced[username]
            return TrustTier.BLOCKED, f"Denounced in VOUCHED.td: {reason}"

        return None, ""

    def _check_trusted(
        self, username: str, repo_root: Path, td: TrustDown
    ) -> tuple[TrustTier | None, str]:
        # Config trusted list
        if username in self.config.trusted_users:
            return TrustTier.TRUSTED, "Listed in config trusted_users"

        # VOUCHED.td vouched
        if username in td.vouched:
            return TrustTier.TRUSTED, "Vouched in VOUCHED.td"

        # CODEOWNERS
        codeowners = parse_codeowners(repo_root)
        if username in codeowners:
            return TrustTier.TRUSTED, "Listed in CODEOWNERS"

        # GitHub collaborators API
        if self.config.repo_owner and self.config.repo_name:
            try:
                collabs = self.api.get_collaborators(
                    self.config.repo_owner, self.config.repo_name
                )
                if username in collabs:
                    return TrustTier.TRUSTED, "GitHub repository collaborator"
            except Exception as e:
                logger.warning("Failed to check collaborators: %s", e)

        return None, ""

    def _check_known(self, username: str) -> tuple[TrustTier | None, str]:
        contributor = self.db.get_contributor(
            self.config.repo_owner, self.config.repo_name, username
        )
        if contributor and contributor.trust_tier in (
            TrustTier.TRUSTED,
            TrustTier.KNOWN,
        ):
            return TrustTier.KNOWN, "Previously recorded in database"

        return None, ""
