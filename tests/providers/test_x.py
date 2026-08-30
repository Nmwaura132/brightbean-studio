"""Tests for XProvider (OAuth, publishing, metrics)."""

import base64
import hashlib
from unittest.mock import MagicMock, patch
from urllib.parse import parse_qs, urlsplit

import pytest

from providers.exceptions import OAuthError, PublishError
from providers.types import PostType, PublishContent
from providers.x import XProvider


def _make_response(payload: dict) -> MagicMock:
    resp = MagicMock()
    resp.json = MagicMock(return_value=payload)
    return resp


def _provider() -> XProvider:
    return XProvider({"client_id": "cid", "client_secret": "csecret"})


class TestGetAuthUrl:
    def test_provider_declares_pkce(self):
        assert _provider().uses_pkce is True

    def test_scopes_include_offline_access_for_refresh_tokens(self):
        url = _provider().get_auth_url("https://app.example/cb", "state-123")

        query = parse_qs(urlsplit(url).query)
        assert query["scope"] == ["tweet.read tweet.write users.read offline.access"]

    def test_includes_standard_base64url_pkce_challenge(self):
        url = _provider().get_auth_url("https://app.example/cb", "state-123", code_verifier="verifier-xyz")

        query = parse_qs(urlsplit(url).query)
        expected = base64.urlsafe_b64encode(hashlib.sha256(b"verifier-xyz").digest()).rstrip(b"=").decode()
        assert query["code_challenge"] == [expected]
        # Guards against accidentally copying TikTok's hex-digest deviation.
        assert len(expected) < 64
        assert query["code_challenge_method"] == ["S256"]

    def test_omits_pkce_when_no_verifier(self):
        url = _provider().get_auth_url("https://app.example/cb", "state-123")

        assert "code_challenge" not in url
        assert "code_challenge_method" not in url


class TestExchangeCode:
    @patch.object(XProvider, "_request")
    def test_sends_basic_auth_and_code_verifier(self, mock_request):
        mock_request.return_value = _make_response({"access_token": "tok", "refresh_token": "ref", "expires_in": 7200})

        _provider().exchange_code("auth-code", "https://app.example/cb", code_verifier="verifier-xyz")

        _, kwargs = mock_request.call_args
        expected_auth = base64.b64encode(b"cid:csecret").decode()
        assert kwargs["headers"]["Authorization"] == f"Basic {expected_auth}"
        assert kwargs["data"]["code_verifier"] == "verifier-xyz"
        assert kwargs["data"]["client_id"] == "cid"

    @patch.object(XProvider, "_request")
    def test_raises_oauth_error_when_no_access_token_returned(self, mock_request):
        mock_request.return_value = _make_response({"error": "invalid_grant"})

        with pytest.raises(OAuthError):
            _provider().exchange_code("bad-code", "https://app.example/cb")


class TestRefreshToken:
    @patch.object(XProvider, "_request")
    def test_falls_back_to_old_refresh_token_when_response_omits_it(self, mock_request):
        mock_request.return_value = _make_response({"access_token": "new-tok", "expires_in": 7200})

        tokens = _provider().refresh_token("old-refresh")

        assert tokens.refresh_token == "old-refresh"


class TestPublishPost:
    @patch.object(XProvider, "_request")
    def test_publishes_text_post(self, mock_request):
        mock_request.return_value = _make_response({"data": {"id": "12345"}})

        result = _provider().publish_post("token", PublishContent(text="hello world", post_type=PostType.TEXT))

        assert result.platform_post_id == "12345"
        assert result.url == "https://x.com/i/web/status/12345"

    def test_rejects_media_urls(self):
        content = PublishContent(text="hi", post_type=PostType.TEXT, media_urls=["https://example.com/pic.jpg"])

        with pytest.raises(PublishError):
            _provider().publish_post("token", content)

    def test_rejects_non_text_post_type(self):
        content = PublishContent(text="hi", post_type=PostType.IMAGE)

        with pytest.raises(PublishError):
            _provider().publish_post("token", content)


class TestGetPostMetrics:
    @patch.object(XProvider, "_request")
    def test_maps_public_metrics_fields(self, mock_request):
        mock_request.return_value = _make_response(
            {"data": {"public_metrics": {"like_count": 5, "reply_count": 2, "retweet_count": 1, "impression_count": 100}}}
        )

        metrics = _provider().get_post_metrics("token", "12345")

        assert metrics.likes == 5
        assert metrics.comments == 2
        assert metrics.shares == 1
        assert metrics.impressions == 100
