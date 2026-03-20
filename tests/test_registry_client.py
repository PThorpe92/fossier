"""Tests for the global fossier registry client."""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import httpx
import pytest

from fossier.registry_client import RegistryClient


@pytest.fixture
def client():
    c = RegistryClient("https://registry.example.com", api_key="test-key")
    c._client = MagicMock()
    yield c
    # no need to close since client is mocked


def _mock_response(status=200, json_data=None):
    resp = MagicMock(spec=httpx.Response)
    resp.status_code = status
    resp.json.return_value = json_data or {}
    resp.text = json.dumps(json_data or {})
    return resp


class TestCheckUsername:
    def test_known_spam(self, client):
        client._client.get.return_value = _mock_response(
            json_data={"known": True, "report_count": 5}
        )
        result = client.check_username("spammer")
        assert result is not None
        assert result.known is True
        assert result.report_count == 5

    def test_unknown_user(self, client):
        client._client.get.return_value = _mock_response(
            json_data={"known": False, "report_count": 0}
        )
        result = client.check_username("gooduser")
        assert result is not None
        assert result.known is False
        assert result.report_count == 0

    def test_server_error(self, client):
        client._client.get.return_value = _mock_response(status=500)
        result = client.check_username("user")
        assert result is None

    def test_network_error(self, client):
        client._client.get.side_effect = httpx.ConnectError("Connection refused")
        result = client.check_username("user")
        assert result is None


class TestReportSpam:
    def test_successful_report(self, client):
        client._client.post.return_value = _mock_response(
            status=201, json_data={"id": 1}
        )
        result = client.report_spam(
            username="spammer",
            repo_owner="owner",
            repo_name="repo",
            score=25.0,
            reason="Score: 25.0",
        )
        assert result is True
        call_args = client._client.post.call_args
        payload = call_args.kwargs.get("json") or call_args[1].get("json")
        assert payload["username"] == "spammer"
        assert payload["score"] == 25.0

    def test_report_with_signals(self, client):
        client._client.post.return_value = _mock_response(status=201)
        signals = {"account_age": {"normalized": 0.1, "weight": 0.15}}
        result = client.report_spam(
            username="spammer",
            repo_owner="owner",
            repo_name="repo",
            score=20.0,
            reason="Low score",
            pr_number=42,
            signals=signals,
        )
        assert result is True

    def test_report_auth_failure(self, client):
        client._client.post.return_value = _mock_response(status=401)
        result = client.report_spam(
            username="user", repo_owner="o", repo_name="r",
            score=30.0, reason="test",
        )
        assert result is False

    def test_report_network_error(self, client):
        client._client.post.side_effect = httpx.ConnectError("Connection refused")
        result = client.report_spam(
            username="user", repo_owner="o", repo_name="r",
            score=30.0, reason="test",
        )
        assert result is False


class TestClientHeaders:
    def test_auth_header_set(self):
        client = RegistryClient("https://example.com", api_key="my-key")
        headers = client._build_headers("my-key")
        assert headers["Authorization"] == "Bearer my-key"
        client.close()

    def test_no_auth_without_key(self):
        client = RegistryClient("https://example.com")
        headers = client._build_headers("")
        assert "Authorization" not in headers
        client.close()
