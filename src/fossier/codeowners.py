from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)

# Standard locations for CODEOWNERS
_CODEOWNERS_PATHS = [
    "CODEOWNERS",
    ".github/CODEOWNERS",
    "docs/CODEOWNERS",
]


def parse_codeowners(repo_root: Path) -> set[str]:
    """Parse CODEOWNERS file and return a set of lowercase GitHub usernames.
    Team references (@org/team) are included as-is for later resolution.
    """
    for rel_path in _CODEOWNERS_PATHS:
        path = repo_root / rel_path
        if path.is_file():
            logger.debug("Found CODEOWNERS at %s", path)
            return _parse_file(path)

    logger.debug("No CODEOWNERS file found")
    return set()


def _parse_file(path: Path) -> set[str]:
    owners: set[str] = set()
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        # Format: <pattern> <owner1> <owner2> ...
        parts = line.split()
        for part in parts[1:]:  # skip the file pattern
            if part.startswith("@"):
                # Could be @username or @org/team
                owners.add(part[1:].lower())
    return owners
