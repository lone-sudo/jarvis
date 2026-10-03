"""
GeminiAPIProvider tests. Every HTTP call is mocked -- no real network
access, no real API key, per the review's explicit requirement to
verify error handling with mocked responses before any real call.
"""

import json
import urllib.error
from unittest.mock import patch, MagicMock

import pytest

from jarvis.providers.gemini_api import GeminiAPIProvider


@pytest.fixture(autouse=True)
def isolated_workspace(tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_WORKSPACE_ROOT", str(tmp_path / "workspace"))
    yield


def test_missing_api_key_returns_clear_error(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    provider = GeminiAPIProvider()
    result = provider.dispatch_prompt("hello")
    assert "GEMINI_API_KEY environment variable is not set" in result


def test_successful_response_parsed_correctly(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "fake-key-not-real")
    fake_response_body = json.dumps({
        "candidates": [{"content": {"parts": [{"text": "This is the model's answer."}]}}]
    }).encode("utf-8")

    mock_response = MagicMock()
    mock_response.read.return_value = fake_response_body
    mock_response.__enter__ = lambda self: mock_response
    mock_response.__exit__ = lambda *a: None

    with patch("jarvis.providers.gemini_api.urllib.request.urlopen", return_value=mock_response):
        provider = GeminiAPIProvider()
        result = provider.dispatch_prompt("hello")

    assert result == "This is the model's answer."


def test_api_key_never_appears_in_any_error_message(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "SUPER-SECRET-KEY-VALUE")
    with patch(
        "jarvis.providers.gemini_api.urllib.request.urlopen",
        side_effect=urllib.error.URLError("connection refused"),
    ):
        provider = GeminiAPIProvider()
        result = provider.dispatch_prompt("hello")

    assert "SUPER-SECRET-KEY-VALUE" not in result


def test_http_404_gives_actionable_hint(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "fake-key")
    error = urllib.error.HTTPError(
        url="x", code=404, msg="Not Found", hdrs=None,
        fp=MagicMock(read=lambda: b'{"error": "model not found"}'),
    )
    with patch("jarvis.providers.gemini_api.urllib.request.urlopen", side_effect=error):
        provider = GeminiAPIProvider()
        result = provider.dispatch_prompt("hello")

    assert "404" in result
    assert "verify model access" in result  # the actionable hint, not just a raw code


def test_http_429_gives_rate_limit_hint_and_no_auto_retry(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "fake-key")
    error = urllib.error.HTTPError(
        url="x", code=429, msg="Too Many Requests", hdrs=None,
        fp=MagicMock(read=lambda: b'{"error": "quota exceeded"}'),
    )
    call_count = {"n": 0}

    def counting_urlopen(*a, **kw):
        call_count["n"] += 1
        raise error

    with patch("jarvis.providers.gemini_api.urllib.request.urlopen", side_effect=counting_urlopen):
        provider = GeminiAPIProvider()
        result = provider.dispatch_prompt("hello")

    assert "429" in result
    assert "will not retry" in result
    assert call_count["n"] == 1  # confirms no automatic retry happened


def test_http_403_gives_permission_hint(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "fake-key")
    error = urllib.error.HTTPError(
        url="x", code=403, msg="Forbidden", hdrs=None,
        fp=MagicMock(read=lambda: b'{"error": "permission denied"}'),
    )
    with patch("jarvis.providers.gemini_api.urllib.request.urlopen", side_effect=error):
        provider = GeminiAPIProvider()
        result = provider.dispatch_prompt("hello")

    assert "403" in result
    assert "permission" in result.lower()


def test_malformed_json_response_handled_gracefully(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "fake-key")
    mock_response = MagicMock()
    mock_response.read.return_value = b"not valid json at all"
    mock_response.__enter__ = lambda self: mock_response
    mock_response.__exit__ = lambda *a: None

    with patch("jarvis.providers.gemini_api.urllib.request.urlopen", return_value=mock_response):
        provider = GeminiAPIProvider()
        result = provider.dispatch_prompt("hello")

    assert "ERROR" in result
    assert "could not parse" in result


def test_unexpected_response_shape_handled_gracefully(monkeypatch):
    """A 200 response that doesn't have the expected candidates/content/parts shape."""
    monkeypatch.setenv("GEMINI_API_KEY", "fake-key")
    mock_response = MagicMock()
    mock_response.read.return_value = json.dumps({"unexpected": "shape"}).encode("utf-8")
    mock_response.__enter__ = lambda self: mock_response
    mock_response.__exit__ = lambda *a: None

    with patch("jarvis.providers.gemini_api.urllib.request.urlopen", return_value=mock_response):
        provider = GeminiAPIProvider()
        result = provider.dispatch_prompt("hello")

    assert "ERROR" in result
    assert "could not parse" in result


def test_conforms_to_provider_protocol(monkeypatch):
    from jarvis.providers.base import Provider
    provider = GeminiAPIProvider()
    assert isinstance(provider, Provider)


def test_expects_cost_false_is_declared_not_verified(monkeypatch):
    """
    Documents the known limitation explicitly, rather than silently
    relying on it: this provider declares expects_cost=False to satisfy
    the $0 policy gate, but that declaration is not independently
    checked against real Gemini billing state -- it depends on the
    caller (Lone) having confirmed free-tier eligibility beforehand.
    """
    monkeypatch.setenv("GEMINI_API_KEY", "fake-key")
    with patch("jarvis.providers.gemini_api.PolicyValidator.authorize_provider") as mock_auth:
        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps(
            {"candidates": [{"content": {"parts": [{"text": "ok"}]}}]}
        ).encode("utf-8")
        mock_response.__enter__ = lambda self: mock_response
        mock_response.__exit__ = lambda *a: None
        with patch("jarvis.providers.gemini_api.urllib.request.urlopen", return_value=mock_response):
            GeminiAPIProvider().dispatch_prompt("hello")

    mock_auth.assert_called_once()
    _, kwargs = mock_auth.call_args
    assert kwargs.get("expects_cost") is False
