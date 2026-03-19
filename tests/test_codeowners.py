"""Tests for CODEOWNERS parser."""

from pathlib import Path

from fossier.codeowners import parse_codeowners


def test_no_codeowners(tmp_path):
    assert parse_codeowners(tmp_path) == set()


def test_basic_codeowners(tmp_path):
    (tmp_path / "CODEOWNERS").write_text(
        "* @alice @bob\n"
        "docs/ @carol\n"
    )
    owners = parse_codeowners(tmp_path)
    assert owners == {"alice", "bob", "carol"}


def test_github_dir_location(tmp_path):
    (tmp_path / ".github").mkdir()
    (tmp_path / ".github" / "CODEOWNERS").write_text("* @maintainer\n")
    owners = parse_codeowners(tmp_path)
    assert owners == {"maintainer"}


def test_comments_and_blanks(tmp_path):
    (tmp_path / "CODEOWNERS").write_text(
        "# This is a comment\n"
        "\n"
        "*.js @frontend-lead\n"
        "# Another comment\n"
        "*.py @backend-lead\n"
    )
    owners = parse_codeowners(tmp_path)
    assert owners == {"frontend-lead", "backend-lead"}


def test_team_references(tmp_path):
    (tmp_path / "CODEOWNERS").write_text("* @org/core-team @alice\n")
    owners = parse_codeowners(tmp_path)
    assert "org/core-team" in owners
    assert "alice" in owners


def test_case_normalization(tmp_path):
    (tmp_path / "CODEOWNERS").write_text("* @Alice @BOB\n")
    owners = parse_codeowners(tmp_path)
    assert owners == {"alice", "bob"}
