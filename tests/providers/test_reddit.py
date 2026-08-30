"""Tests for RedditProvider (OAuth, publishing, metrics)."""

import base64
from unittest.mock import MagicMock, patch
from urllib.parse import parse_qs, urlsplit

import pytest

from providers.exceptions import OAuthError, PublishError
from providers.reddit import DEFAULT_USER_AGENT
from providers.types import PostType, PublishContent
from providers.reddit import RedditProvider


def _make_response(payload: dict) -> MagicMock:
    resp = MagicMock()
    resp.json = MagicMock(return_value=payload)
    return resp


def _provider(**extra) -> RedditProvider:
    return RedditProvider({"client_id": "cid", "client_secret": "csecret", **extra})


class TestGetAuthUrl:
    def test_requests_permanent_duration_for_refresh_token(self):
        url = _provider().get_auth_url("https://app.example/cb", "state-123")

        query = parse_qs(urlsplit(url).query)
        assert query["duration"] == ["permanent"]

    def test_scopes_are_identity_submit_read(self):
        url = _provider().get_auth_url("https://app.example/cb", "state-123")

        query = parse_qs(urlsplit(url).query)
        assert query["scope"] == ["identity submit read"]


class TestUserAgent:
    def test_falls_back_to_default_when_unset(self):
        assert _provider()._user_agent() == DEFAULT_USER_AGENT

    def test_uses_configured_value_when_set(self):
        provider = _provider(user_agent="web:custom:v1 (by /u/someone)")
        assert provider._user_agent() == "web:custom:v1 (by /u/someone)"


class TestExchangeCode:
    @patch.object(RedditProvider, "_request")
    def test_sends_basic_auth_and_user_agent(self, mock_request):
        mock_request.return_value = _make_response({"access_token": "tok", "refresh_token": "ref"})

        _provider().exchange_code("auth-code", "https://app.example/cb")

        _, kwargs = mock_request.call_args
        expected_auth = base64.b64encode(b"cid:csecret").decode()
        assert kwargs["headers"]["Authorization"] == f"Basic {expected_auth}"
        assert kwargs["headers"]["User-Agent"] == DEFAULT_USER_AGENT

    @patch.object(RedditProvider, "_request")
    def test_raises_oauth_error_when_no_access_token_returned(self, mock_request):
        mock_request.return_value = _make_response({"error": "invalid_grant"})

        with pytest.raises(OAuthError):
            _provider().exchange_code("bad-code", "https://app.example/cb")


class TestRefreshToken:
    @patch.object(RedditProvider, "_request")
    def test_keeps_existing_refresh_token_when_response_omits_it(self, mock_request):
        mock_request.return_value = _make_response({"access_token": "new-tok"})

        tokens = _provider().refresh_token("old-refresh")

        assert tokens.refresh_token == "old-refresh"


class TestGetProfile:
    @patch.object(RedditProvider, "_request")
    def test_unescapes_html_entities_in_avatar_url(self, mock_request):
        mock_request.return_value = _make_response(
            {"id": "abc123", "name": "someuser", "icon_img": "https://example.com/a.png?x=1&amp;y=2", "total_karma": 500}
        )

        profile = _provider().get_profile("token")

        assert profile.avatar_url == "https://example.com/a.png?x=1&y=2"
        assert profile.handle == "someuser"


class TestPublishPost:
    @patch.object(RedditProvider, "_request")
    def test_publishes_self_post(self, mock_request):
        mock_request.return_value = _make_response({"json": {"errors": [], "data": {"id": "xyz789", "url": "https://reddit.com/r/test/xyz789"}}})

        content = PublishContent(text="body text", title="My title", post_type=PostType.TEXT, extra={"subreddit": "test"})
        result = _provider().publish_post("token", content)

        _, kwargs = mock_request.call_args
        assert kwargs["data"]["kind"] == "self"
        assert kwargs["data"]["sr"] == "test"
        assert result.platform_post_id == "xyz789"

    @patch.object(RedditProvider, "_request")
    def test_publishes_link_post(self, mock_request):
        mock_request.return_value = _make_response({"json": {"errors": [], "data": {"id": "xyz789"}}})

        content = PublishContent(
            title="My title", post_type=PostType.LINK, link_url="https://example.com", extra={"subreddit": "test"}
        )
        _provider().publish_post("token", content)

        _, kwargs = mock_request.call_args
        assert kwargs["data"]["kind"] == "link"
        assert kwargs["data"]["url"] == "https://example.com"

    def test_requires_subreddit(self):
        content = PublishContent(text="body", title="t", post_type=PostType.TEXT)

        with pytest.raises(PublishError, match="subreddit"):
            _provider().publish_post("token", content)

    def test_requires_title(self):
        content = PublishContent(text="", title="", post_type=PostType.TEXT, extra={"subreddit": "test"})

        with pytest.raises(PublishError, match="title"):
            _provider().publish_post("token", content)

    def test_rejects_media(self):
        content = PublishContent(
            text="body", title="t", post_type=PostType.TEXT, media_urls=["https://x/y.jpg"], extra={"subreddit": "test"}
        )

        with pytest.raises(PublishError):
            _provider().publish_post("token", content)

    @patch.object(RedditProvider, "_request")
    def test_raises_on_reddit_thing_errors(self, mock_request):
        mock_request.return_value = _make_response(
            {"json": {"errors": [["SUBREDDIT_NOEXIST", "that subreddit doesn't exist", "sr"]], "data": {}}}
        )

        content = PublishContent(text="body", title="t", post_type=PostType.TEXT, extra={"subreddit": "nope"})
        with pytest.raises(PublishError, match="SUBREDDIT_NOEXIST"):
            _provider().publish_post("token", content)


class TestGetPostMetrics:
    @patch.object(RedditProvider, "_request")
    def test_maps_score_and_num_comments(self, mock_request):
        mock_request.return_value = _make_response(
            {"data": {"children": [{"data": {"score": 42, "num_comments": 7, "upvote_ratio": 0.9}}]}}
        )

        metrics = _provider().get_post_metrics("token", "xyz789")

        assert metrics.likes == 42
        assert metrics.comments == 7
