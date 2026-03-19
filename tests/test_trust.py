"""Tests for trust tier resolution."""

from unittest.mock import MagicMock

from fossier.config import Config
from fossier.db import Database
from fossier.models import Contributor, TrustTier
from fossier.trust import resolve_tier


def test_blocked_from_config(tmp_path):
    config = Config(
        repo_owner="owner",
        repo_name="repo",
        repo_root=tmp_path,
        blocked_users={"spammer"},
    )
    db = MagicMock(spec=Database)
    api = MagicMock()

    tier, reason = resolve_tier("spammer", config, db, api)
    assert tier == TrustTier.BLOCKED
    assert "config" in reason.lower()


def test_blocked_from_vouched_td(tmp_path):
    (tmp_path / "VOUCHED.td").write_text("- baduser  Known spammer\n")
    config = Config(repo_owner="owner", repo_name="repo", repo_root=tmp_path)
    db = MagicMock(spec=Database)
    api = MagicMock()

    tier, reason = resolve_tier("baduser", config, db, api)
    assert tier == TrustTier.BLOCKED
    assert "denounced" in reason.lower()


def test_trusted_from_config(tmp_path):
    config = Config(
        repo_owner="owner",
        repo_name="repo",
        repo_root=tmp_path,
        trusted_users={"maintainer"},
    )
    db = MagicMock(spec=Database)
    api = MagicMock()

    tier, reason = resolve_tier("maintainer", config, db, api)
    assert tier == TrustTier.TRUSTED


def test_trusted_from_vouched_td(tmp_path):
    (tmp_path / "VOUCHED.td").write_text("+ trusteduser\n")
    config = Config(repo_owner="owner", repo_name="repo", repo_root=tmp_path)
    db = MagicMock(spec=Database)
    api = MagicMock()

    tier, reason = resolve_tier("trusteduser", config, db, api)
    assert tier == TrustTier.TRUSTED


def test_trusted_from_codeowners(tmp_path):
    (tmp_path / "CODEOWNERS").write_text("* @codeowner\n")
    config = Config(repo_owner="owner", repo_name="repo", repo_root=tmp_path)
    db = MagicMock(spec=Database)
    api = MagicMock()

    tier, reason = resolve_tier("codeowner", config, db, api)
    assert tier == TrustTier.TRUSTED


def test_trusted_from_collaborators_api(tmp_path):
    config = Config(repo_owner="owner", repo_name="repo", repo_root=tmp_path)
    db = MagicMock(spec=Database)
    api = MagicMock()
    api.get_collaborators.return_value = ["collab-user"]

    tier, reason = resolve_tier("collab-user", config, db, api)
    assert tier == TrustTier.TRUSTED
    assert "collaborator" in reason.lower()


def test_known_from_db(tmp_path):
    config = Config(repo_owner="owner", repo_name="repo", repo_root=tmp_path)
    db = MagicMock(spec=Database)
    db.get_contributor.return_value = Contributor(
        username="knownuser",
        repo_owner="owner",
        repo_name="repo",
        trust_tier=TrustTier.KNOWN,
    )
    api = MagicMock()
    api.get_collaborators.return_value = []

    tier, reason = resolve_tier("knownuser", config, db, api)
    assert tier == TrustTier.KNOWN


def test_unknown_fallthrough(tmp_path):
    config = Config(repo_owner="owner", repo_name="repo", repo_root=tmp_path)
    db = MagicMock(spec=Database)
    db.get_contributor.return_value = None
    api = MagicMock()
    api.get_collaborators.return_value = []

    tier, reason = resolve_tier("newuser", config, db, api)
    assert tier == TrustTier.UNKNOWN


def test_blocked_wins_over_trusted(tmp_path):
    """A denounced user cannot be elevated by other trust sources."""
    (tmp_path / "VOUCHED.td").write_text("- compromised  Account hacked\n")
    (tmp_path / "CODEOWNERS").write_text("* @compromised\n")
    config = Config(
        repo_owner="owner",
        repo_name="repo",
        repo_root=tmp_path,
        trusted_users={"compromised"},
    )
    db = MagicMock(spec=Database)
    api = MagicMock()

    tier, reason = resolve_tier("compromised", config, db, api)
    assert tier == TrustTier.BLOCKED
