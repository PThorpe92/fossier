"""Tests for trust tier resolution."""

from unittest.mock import MagicMock

from fossier.config import Config
from fossier.db import Database
from fossier.models import Contributor, TrustTier
from fossier.trust import TrustResolver


def test_blocked_from_config(tmp_path):
    config = Config(
        repo_owner="owner",
        repo_name="repo",
        repo_root=tmp_path,
        blocked_users={"spammer"},
    )
    db = MagicMock(spec=Database)
    api = MagicMock()
    resolver = TrustResolver(config, db, api)

    tier, reason = resolver.resolve_tier("spammer")
    assert tier == TrustTier.BLOCKED
    assert "config" in reason.lower()


def test_blocked_from_vouched_td(tmp_path):
    (tmp_path / "VOUCHED.td").write_text("- baduser  Known spammer\n")
    config = Config(repo_owner="owner", repo_name="repo", repo_root=tmp_path)
    db = MagicMock(spec=Database)
    api = MagicMock()
    resolver = TrustResolver(config, db, api)

    tier, reason = resolver.resolve_tier("baduser")
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
    resolver = TrustResolver(config, db, api)

    tier, reason = resolver.resolve_tier("maintainer")
    assert tier == TrustTier.TRUSTED


def test_trusted_from_vouched_td(tmp_path):
    (tmp_path / "VOUCHED.td").write_text("+ trusteduser\n")
    config = Config(repo_owner="owner", repo_name="repo", repo_root=tmp_path)
    db = MagicMock(spec=Database)
    api = MagicMock()
    resolver = TrustResolver(config, db, api)

    tier, reason = resolver.resolve_tier("trusteduser")
    assert tier == TrustTier.TRUSTED


def test_trusted_from_codeowners(tmp_path):
    (tmp_path / "CODEOWNERS").write_text("* @codeowner\n")
    config = Config(repo_owner="owner", repo_name="repo", repo_root=tmp_path)
    db = MagicMock(spec=Database)
    api = MagicMock()
    resolver = TrustResolver(config, db, api)

    tier, reason = resolver.resolve_tier("codeowner")
    assert tier == TrustTier.TRUSTED


def test_trusted_from_collaborators_api(tmp_path):
    config = Config(repo_owner="owner", repo_name="repo", repo_root=tmp_path)
    db = MagicMock(spec=Database)
    api = MagicMock()
    api.get_collaborators.return_value = ["collab-user"]
    resolver = TrustResolver(config, db, api)

    tier, reason = resolver.resolve_tier("collab-user")
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
    resolver = TrustResolver(config, db, api)

    tier, reason = resolver.resolve_tier("knownuser")
    assert tier == TrustTier.KNOWN


def test_unknown_fallthrough(tmp_path):
    config = Config(repo_owner="owner", repo_name="repo", repo_root=tmp_path)
    db = MagicMock(spec=Database)
    db.get_contributor.return_value = None
    api = MagicMock()
    resolver = TrustResolver(config, db, api)
    api.get_collaborators.return_value = []

    tier, reason = resolver.resolve_tier("newuser")
    assert tier == TrustTier.UNKNOWN


def test_trusted_from_org_membership(tmp_path):
    """User in a trusted org should be TRUSTED."""
    config = Config(
        repo_owner="owner",
        repo_name="repo",
        repo_root=tmp_path,
        trusted_orgs={"my-company"},
    )
    db = MagicMock(spec=Database)
    db.get_contributor.return_value = None
    api = MagicMock()
    api.get_collaborators.return_value = []
    api.get_user_orgs.return_value = ["my-company", "some-other-org"]
    resolver = TrustResolver(config, db, api)

    tier, reason = resolver.resolve_tier("orgmember")
    assert tier == TrustTier.TRUSTED
    assert "trusted org" in reason.lower()
    assert "my-company" in reason


def test_trusted_orgs_not_member(tmp_path):
    """User not in any trusted org should fall through."""
    config = Config(
        repo_owner="owner",
        repo_name="repo",
        repo_root=tmp_path,
        trusted_orgs={"my-company"},
    )
    db = MagicMock(spec=Database)
    db.get_contributor.return_value = None
    api = MagicMock()
    api.get_collaborators.return_value = []
    api.get_user_orgs.return_value = ["unrelated-org"]
    resolver = TrustResolver(config, db, api)

    tier, reason = resolver.resolve_tier("outsider")
    assert tier == TrustTier.UNKNOWN


def test_trusted_orgs_empty_config(tmp_path):
    """No trusted_orgs configured should skip the check entirely."""
    config = Config(repo_owner="owner", repo_name="repo", repo_root=tmp_path)
    db = MagicMock(spec=Database)
    db.get_contributor.return_value = None
    api = MagicMock()
    api.get_collaborators.return_value = []
    resolver = TrustResolver(config, db, api)

    tier, reason = resolver.resolve_tier("newuser")
    assert tier == TrustTier.UNKNOWN
    api.get_user_orgs.assert_not_called()


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
    resolver = TrustResolver(config, db, api)

    tier, reason = resolver.resolve_tier("compromised")
    assert tier == TrustTier.BLOCKED
