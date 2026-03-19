"""VOUCHED.td (TrustDown) parser.

Format:
    Lines starting with `+` vouch for a user.
    Lines starting with `-` denounce a user.
    Optional reason after the username (rest of line).
    Comments start with `#`. Blank lines are ignored.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger(__name__)

_VOUCHED_PATHS = [
    "VOUCHED.td",
    ".github/VOUCHED.td",
]


@dataclass
class TrustDown:
    vouched: set[str] = field(default_factory=set)
    denounced: dict[str, str] = field(default_factory=dict)  # username -> reason


def parse_vouched(repo_root: Path) -> TrustDown:
    """Parse VOUCHED.td file and return vouched/denounced sets."""
    for rel_path in _VOUCHED_PATHS:
        path = repo_root / rel_path
        if path.is_file():
            logger.debug("Found VOUCHED.td at %s", path)
            return _parse_file(path)

    logger.debug("No VOUCHED.td file found")
    return TrustDown()


def _parse_file(path: Path) -> TrustDown:
    result = TrustDown()
    for line_num, line in enumerate(path.read_text().splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue

        if line.startswith("+"):
            parts = line[1:].strip().split(None, 1)
            if parts:
                result.vouched.add(parts[0].lower())
        elif line.startswith("-"):
            parts = line[1:].strip().split(None, 1)
            if parts:
                username = parts[0].lower()
                reason = parts[1] if len(parts) > 1 else "Denounced in VOUCHED.td"
                result.denounced[username] = reason
        else:
            logger.warning("VOUCHED.td:%d: unrecognized line: %s", line_num, line)

    return result


def add_vouch(repo_root: Path, username: str) -> Path:
    """Add a vouch entry to VOUCHED.td, creating it if needed. Returns path used."""
    path = _get_or_create_path(repo_root)
    content = path.read_text() if path.exists() else ""
    entry = f"+ {username.lower()}\n"
    if entry.strip() not in content:
        with open(path, "a") as f:
            if content and not content.endswith("\n"):
                f.write("\n")
            f.write(entry)
    return path


def add_denounce(repo_root: Path, username: str, reason: str) -> Path:
    """Add a denounce entry to VOUCHED.td, creating it if needed. Returns path used."""
    path = _get_or_create_path(repo_root)
    content = path.read_text() if path.exists() else ""
    entry = f"- {username.lower()}  {reason}\n"
    with open(path, "a") as f:
        if content and not content.endswith("\n"):
            f.write("\n")
        f.write(entry)
    return path


def _get_or_create_path(repo_root: Path) -> Path:
    for rel_path in _VOUCHED_PATHS:
        path = repo_root / rel_path
        if path.is_file():
            return path
    return repo_root / _VOUCHED_PATHS[0]
