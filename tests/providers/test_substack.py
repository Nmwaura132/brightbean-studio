"""Tests for SubstackProvider (session-cookie auth, publishing)."""

from unittest.mock import MagicMock, patch

import pytest

from providers.exceptions import OAuthError, PublishError
from providers.substack import SubstackProvider, _text_to_doc
from providers.types import PostType, PublishContent


def _make_response(payload: dict) -> MagicMock:
    resp = MagicMock()
    resp.json = MagicMock(return_value=payload)
    return resp


def _provider(publication_url="https://example.substack.com") -> SubstackProvider:
    return SubstackProvider({"publication_url": publication_url})


class TestTextToDoc:
    def test_one_paragraph_per_nonempty_line(self):
        doc = _text_to_doc("first line\n\nsecond line")

        paragraphs = doc["content"]
        assert len(paragraphs) == 2
        assert paragraphs[0]["content"][0]["text"] == "first line"
        assert paragraphs[1]["content"][0]["text"] == "second line"


class TestOAuthStubs:
    def test_get_auth_url_not_implemented(self):
        with pytest.raises(NotImplementedError):
            _provider().get_auth_url("https://app.example/cb", "state")

    def test_exchange_code_not_implemented(self):
        with pytest.raises(NotImplementedError):
            _provider().exchange_code("code", "https://app.example/cb")


class TestHeaders:
    def test_builds_cookie_header_not_bearer(self):
        headers = _provider()._headers("my-session-cookie")

        assert headers == {"Cookie": "connect.sid=my-session-cookie"}


class TestGetProfile:
    @patch.object(SubstackProvider, "_request")
    def test_parses_publication_info(self, mock_request):
        mock_request.return_value = _make_response(
            {"publication": {"id": 42, "name": "My Newsletter", "subdomain": "example", "logo_url": "https://x/logo.png"}}
        )

        profile = _provider().get_profile("cookie-value")

        assert profile.platform_id == "42"
        assert profile.name == "My Newsletter"
        assert profile.handle == "example"

    @patch.object(SubstackProvider, "_request")
    def test_raises_when_no_publication_in_response(self, mock_request):
        mock_request.return_value = _make_response({})

        with pytest.raises(OAuthError):
            _provider().get_profile("cookie-value")

    def test_raises_when_publication_url_not_configured(self):
        provider = SubstackProvider({})

        with pytest.raises(PublishError, match="publication_url"):
            provider.get_profile("cookie-value")


class TestPublishPost:
    @patch.object(SubstackProvider, "_request")
    def test_creates_draft_then_publishes_it(self, mock_request):
        mock_request.side_effect = [
            _make_response({"id": 999}),
            _make_response({"id": 999, "canonical_url": "https://example.substack.com/p/my-post"}),
        ]

        content = PublishContent(title="My Post", text="body text", post_type=PostType.ARTICLE)
        result = _provider().publish_post("cookie-value", content)

        assert mock_request.call_count == 2
        first_call_url = mock_request.call_args_list[0].args[1]
        second_call_url = mock_request.call_args_list[1].args[1]
        assert first_call_url.endswith("/api/v1/drafts")
        assert second_call_url.endswith("/api/v1/drafts/999/publish")
        assert result.platform_post_id == "999"
        assert result.url == "https://example.substack.com/p/my-post"

    def test_requires_title(self):
        content = PublishContent(title="", text="body", post_type=PostType.ARTICLE)

        with pytest.raises(PublishError, match="title"):
            _provider().publish_post("cookie-value", content)

    def test_rejects_non_article_post_type(self):
        content = PublishContent(title="t", text="body", post_type=PostType.TEXT)

        with pytest.raises(PublishError):
            _provider().publish_post("cookie-value", content)

    def test_rejects_media(self):
        content = PublishContent(title="t", text="body", post_type=PostType.ARTICLE, media_urls=["https://x/y.jpg"])

        with pytest.raises(PublishError):
            _provider().publish_post("cookie-value", content)
