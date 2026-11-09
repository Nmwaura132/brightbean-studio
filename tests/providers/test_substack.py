"""Tests for SubstackProvider (session-cookie auth, publishing)."""

import json
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


TOKEN = "s%3Asub"


class TestTextToDoc:
    def test_one_paragraph_per_nonempty_line(self):
        doc = _text_to_doc("first line\n\nsecond line")

        paragraphs = doc["content"]
        assert len(paragraphs) == 2
        assert paragraphs[0]["content"][0]["text"] == "first line"
        assert paragraphs[1]["content"][0]["text"] == "second line"

    def test_a_hash_line_becomes_a_heading(self):
        doc = _text_to_doc("## 📊 Market today")

        assert doc["content"][0] == {
            "type": "heading",
            "attrs": {"level": 3},
            "content": [{"type": "text", "text": "📊 Market today"}],
        }

    def test_consecutive_bullet_lines_form_one_list(self):
        doc = _text_to_doc("• KCB pays tomorrow\n• KPLC books close 27 November")

        assert [len(node["content"]) for node in doc["content"]] == [2]

    def test_a_bare_domain_becomes_a_link_without_the_full_stop(self):
        doc = _text_to_doc("See your portfolio: heymara.co.ke.")

        assert doc["content"][0]["content"][1] == {
            "type": "text",
            "text": "heymara.co.ke",
            "marks": [{"type": "link", "attrs": {"href": "https://heymara.co.ke"}}],
        }

    def test_prices_are_not_links(self):
        doc = _text_to_doc("NCBA closed at 89.00, up 3.26%.")

        assert doc["content"][0]["content"] == [{"type": "text", "text": "NCBA closed at 89.00, up 3.26%."}]


class TestBodyWithoutSubtitle:
    def test_the_subtitle_line_is_not_repeated_in_the_body(self):
        from providers.substack import _body_without_subtitle

        assert _body_without_subtitle("A short dek.\nThe body.", "A short dek.") == "The body."

    def test_a_first_line_cut_short_as_subtitle_stays_in_the_body(self):
        from providers.substack import _body_without_subtitle

        text = "x" * 200 + "\nThe body."
        assert _body_without_subtitle(text, "x" * 150) == text


class TestOAuthStubs:
    def test_get_auth_url_not_implemented(self):
        with pytest.raises(NotImplementedError):
            _provider().get_auth_url("https://app.example/cb", "state")

    def test_exchange_code_not_implemented(self):
        with pytest.raises(NotImplementedError):
            _provider().exchange_code("code", "https://app.example/cb")


class TestHeaders:
    def test_sends_substack_sid_cookie_not_bearer(self):
        headers = _provider()._headers(TOKEN)

        assert headers == {"Cookie": "substack.sid=s%3Asub"}


class TestGetProfile:
    PUBLICATION = {"id": 42, "name": "My Newsletter", "subdomain": "example", "logo_url": "https://x/logo.png"}

    def _profile(self, mock_request, member_pub_id):
        mock_request.side_effect = [
            _make_response(self.PUBLICATION),
            _make_response({"id": 7, "publicationUsers": [{"publication": {"id": member_pub_id}, "role": "admin"}]}),
        ]
        return _provider().get_profile(TOKEN)

    @patch.object(SubstackProvider, "_request")
    def test_uses_publication_id_as_platform_id(self, mock_request):
        assert self._profile(mock_request, member_pub_id=42).platform_id == "42"

    @patch.object(SubstackProvider, "_request")
    def test_uses_publication_name(self, mock_request):
        assert self._profile(mock_request, member_pub_id=42).name == "My Newsletter"

    @patch.object(SubstackProvider, "_request")
    def test_raises_when_cookie_owner_is_not_a_member(self, mock_request):
        with pytest.raises(OAuthError):
            self._profile(mock_request, member_pub_id=99)

    def test_raises_when_publication_url_not_configured(self):
        provider = SubstackProvider({})

        with pytest.raises(PublishError, match="publication_url"):
            provider.get_profile(TOKEN)


class TestPublishPost:
    def _publish(self, mock_request):
        mock_request.side_effect = [
            _make_response({"id": 7, "handle": "me"}),
            _make_response({"id": 999}),
            _make_response({"id": 999, "canonical_url": "https://example.substack.com/p/my-post"}),
        ]
        content = PublishContent(title="My Post", text="body text", post_type=PostType.ARTICLE)
        return _provider().publish_post(TOKEN, content)

    @patch.object(SubstackProvider, "_request")
    def test_publishes_created_draft(self, mock_request):
        self._publish(mock_request)

        assert mock_request.call_args_list[2].args[1].endswith("/api/v1/drafts/999/publish")

    @patch.object(SubstackProvider, "_request")
    def test_returns_published_url(self, mock_request):
        result = self._publish(mock_request)

        assert result.url == "https://example.substack.com/p/my-post"

    @patch.object(SubstackProvider, "_request")
    def test_draft_bylines_use_own_user_id(self, mock_request):
        self._publish(mock_request)

        draft_payload = mock_request.call_args_list[1].kwargs["json"]
        assert draft_payload["draft_bylines"] == [{"id": 7, "is_guest": False}]

    @patch.object(SubstackProvider, "_request")
    def test_draft_body_is_a_json_string(self, mock_request):
        self._publish(mock_request)

        draft_payload = mock_request.call_args_list[1].kwargs["json"]
        assert json.loads(draft_payload["draft_body"])["type"] == "doc"

    @patch.object(SubstackProvider, "_request")
    def test_raises_when_profile_has_no_user_id(self, mock_request):
        mock_request.return_value = _make_response({})
        content = PublishContent(title="t", text="body", post_type=PostType.ARTICLE)

        with pytest.raises(PublishError, match="user id"):
            _provider().publish_post(TOKEN, content)

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


class TestSubtitle:
    def test_uses_only_the_first_line_of_a_long_caption(self):
        from providers.substack import _subtitle

        assert _subtitle("Headline here.\n\nA long body paragraph that must not become the subtitle.") == "Headline here."

    def test_is_capped_in_length(self):
        from providers.substack import MAX_SUBTITLE_LENGTH, _subtitle

        assert len(_subtitle("x" * 1000)) == MAX_SUBTITLE_LENGTH

    def test_empty_description_gives_empty_subtitle(self):
        from providers.substack import _subtitle

        assert _subtitle(None) == ""


def test_publisher_defaults_substack_posts_to_article():
    from apps.publisher.engine import PublishEngine

    assert PublishEngine._resolve_post_type("substack", {}, 0, None) == PostType.ARTICLE
