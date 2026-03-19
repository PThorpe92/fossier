"""Trust tier resolution cascade: Blocked → Trusted → Known → Unknown."""

from __future__ import annotations

import logging
from pathlib import Path

from fossier.codeowners import parse_codeowners
from fossier.config import Config
from fossier.db import Database
from fossier.github_api import GitHubAPI
from fossier.models import TrustTier
from fossier.trustdown import parse_vouched

logger = logging.getLogger(__name__)


def resolve_tier(
    username: str,
    config: Config,
    db: Database,
    api: GitHubAPI,
) -> tuple[TrustTier, str]:
    """Resolve a contributor's trust tier. Returns (tier, reason)."""
    username_lower = username.lower()
    repo_root = config.repo_root

    # 1. BLOCKED - checked first so denounced users can't be elevated
    tier, reason = _check_blocked(username_lower, config, repo_root)
    if tier:
        return tier, reason

    # 2. TRUSTED
    tier, reason = _check_trusted(username_lower, config, repo_root, api)
    if tier:
        return tier, reason

    # 3. KNOWN — previous contributors in DB
    tier, reason = _check_known(username_lower, config, db)
    if tier:
        return tier, reason

    # 4. UNKNOWN
    return TrustTier.UNKNOWN, "No prior trust relationship found"


def _check_blocked(
    username: str, config: Config, repo_root: Path
) -> tuple[TrustTier | None, str]:
    # Config blocked list
    if username in config.blocked_users:
        return TrustTier.BLOCKED, "Listed in config blocked_users"

    # VOUCHED.td denouncements
    td = parse_vouched(repo_root)
    if username in td.denounced:
        reason = td.denounced[username]
        return TrustTier.BLOCKED, f"Denounced in VOUCHED.td: {reason}"

    return None, ""


def _check_trusted(
    username: str, config: Config, repo_root: Path, api: GitHubAPI
) -> tuple[TrustTier | None, str]:
    # Config trusted list
    if username in config.trusted_users:
        return TrustTier.TRUSTED, "Listed in config trusted_users"

    # VOUCHED.td vouched
    td = parse_vouched(repo_root)
    if username in td.vouched:
        return TrustTier.TRUSTED, "Vouched in VOUCHED.td"

    # CODEOWNERS
    codeowners = parse_codeowners(repo_root)
    if username in codeowners:
        return TrustTier.TRUSTED, "Listed in CODEOWNERS"

    # GitHub collaborators API
    if config.repo_owner and config.repo_name:
        try:
            collabs = api.get_collaborators(config.repo_owner, config.repo_name)
            if username in collabs:
                return TrustTier.TRUSTED, "GitHub repository collaborator"
        except Exception as e:
            logger.warning("Failed to check collaborators: %s", e)

    return None, ""


def _check_known(
    username: str, config: Config, db: Database
) -> tuple[TrustTier | None, str]:
    contributor = db.get_contributor(config.repo_owner, config.repo_name, username)
    if contributor and contributor.trust_tier in (TrustTier.TRUSTED, TrustTier.KNOWN):
        return TrustTier.KNOWN, "Previously recorded in database"

    return None, ""
