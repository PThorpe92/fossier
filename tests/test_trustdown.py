"""Tests for VOUCHED.td parser."""

from pathlib import Path

from fossier.trustdown import TrustDown, add_denounce, add_vouch, parse_vouched


def test_parse_empty_dir(tmp_path):
    result = parse_vouched(tmp_path)
    assert result.vouched == set()
    assert result.denounced == {}


def test_parse_vouch_entries(tmp_path):
    (tmp_path / "VOUCHED.td").write_text(
        "# Maintainers\n"
        "+ octocat\n"
        "+ mona  Longtime contributor\n"
    )
    result = parse_vouched(tmp_path)
    assert result.vouched == {"octocat", "mona"}
    assert result.denounced == {}


def test_parse_denounce_entries(tmp_path):
    (tmp_path / "VOUCHED.td").write_text(
        "- spammer  SEO link spam\n"
        "- badactor\n"
    )
    result = parse_vouched(tmp_path)
    assert result.vouched == set()
    assert "spammer" in result.denounced
    assert result.denounced["spammer"] == "SEO link spam"
    assert result.denounced["badactor"] == "Denounced in VOUCHED.td"


def test_parse_mixed(tmp_path):
    (tmp_path / "VOUCHED.td").write_text(
        "# Trust list\n"
        "+ alice\n"
        "- bob  Spam PRs\n"
        "\n"
        "+ charlie\n"
    )
    result = parse_vouched(tmp_path)
    assert result.vouched == {"alice", "charlie"}
    assert result.denounced == {"bob": "Spam PRs"}


def test_case_normalization(tmp_path):
    (tmp_path / "VOUCHED.td").write_text("+ OctoCat\n- SpamUser  reason\n")
    result = parse_vouched(tmp_path)
    assert "octocat" in result.vouched
    assert "spamuser" in result.denounced


def test_github_dir_location(tmp_path):
    (tmp_path / ".github").mkdir()
    (tmp_path / ".github" / "VOUCHED.td").write_text("+ fromgithubdir\n")
    result = parse_vouched(tmp_path)
    assert "fromgithubdir" in result.vouched


def test_add_vouch(tmp_path):
    add_vouch(tmp_path, "newuser")
    result = parse_vouched(tmp_path)
    assert "newuser" in result.vouched


def test_add_denounce(tmp_path):
    add_denounce(tmp_path, "baduser", "spam")
    result = parse_vouched(tmp_path)
    assert "baduser" in result.denounced


def test_add_vouch_idempotent(tmp_path):
    add_vouch(tmp_path, "alice")
    add_vouch(tmp_path, "alice")
    content = (tmp_path / "VOUCHED.td").read_text()
    assert content.count("alice") == 1
