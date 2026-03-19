"""Shared test fixtures."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from fossier.config import Config
from fossier.db import Database
from fossier.github_api import GitHubAPI


@pytest.fixture
def tmp_dir(tmp_path):
    """Temporary directory for test files."""
    return tmp_path


@pytest.fixture
def db(tmp_path):
    """Fresh in-memory-like database for each test."""
    db_path = str(tmp_path / "test.db")
    database = Database(db_path)
    database.connect()
    yield database
    database.close()


@pytest.fixture
def config(tmp_path):
    """Default test config."""
    return Config(
        repo_owner="testowner",
        repo_name="testrepo",
        repo_root=tmp_path,
        db_path=str(tmp_path / "test.db"),
        github_token="test-token",
        dry_run=True,
    )


@pytest.fixture
def mock_api(config, db):
    """GitHubAPI with mocked HTTP client."""
    api = GitHubAPI(config, db)
    api._client = MagicMock()
    return api
