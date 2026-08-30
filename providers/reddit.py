"""Reddit API provider.

Self (text) and link posts only — image/video posts go through Reddit's media
asset lease + upload flow, which (like X's media upload) is different enough
from anything else in this codebase that it isn't implemented here rather than
guessed at. See ``supported_media_types``.

Two Reddit-specific quirks baked in below:
* Every request needs a descriptive ``User-Agent`` header — Reddit aggressively
  rate-limits generic/default ones. See ``_headers``.
* The authorize URL needs ``duration=permanent`` or Reddit never issues a
  refresh token — the access token just expires in ~1 hour with no way to
  renew short of reconnecting (Reddit's analogue of X's ``offline.access``).
"""

from __future__ import annotations

import base64
import html
import logging
import os
from urllib.parse import urlencode

from .base import SocialProvider
from .exceptions import OAuthError, PublishError
from .types import (
    AccountProfile,
    AuthType,
    CommentResult,
    MediaType,
    OAuthTokens,
    PostMetrics,
    PostType,
    PublishContent,
    PublishResult,
)

logger = logging.getLogger(__name__)

AUTH_URL = "https://www.reddit.com/api/v1/authorize"
TOKEN_URL = "https://www.reddit.com/api/v1/access_token"
REVOKE_URL = "https://www.reddit.com/api/v1/revoke_token"
API_BASE = os.environ.get("REDDIT_API_BASE", "https://oauth.reddit.com")

MAX_TITLE_LENGTH = 300
MAX_SELFTEXT_LENGTH = 40000

# Reddit's own default per Developer Terms: identify the app, not a generic
# HTTP client string. Self-hosters can override via the `user_agent` credential
# key if Reddit asks them to make it more specific to their instance.
DEFAULT_USER_AGENT = "web:brightbean-studio:v1.0 (by /u/brightbean_studio)"


class RedditProvider(SocialProvider):
    """Reddit API provider using OAuth 2.0 (confidential "web app" client type)."""

    # ------------------------------------------------------------------
    # Metadata
    # ------------------------------------------------------------------

    @property
    def platform_name(self) -> str:
        return "Reddit"

    @property
    def auth_type(self) -> AuthType:
        return AuthType.OAUTH2

    @property
    def max_caption_length(self) -> int:
        return MAX_SELFTEXT_LENGTH

    @property
    def supported_post_types(self) -> list[PostType]:
        return [PostType.TEXT, PostType.LINK]

    @property
    def supported_media_types(self) -> list[MediaType]:
        # Empty on purpose — see module docstring. publish_post rejects media
        # explicitly rather than silently dropping it.
        return []

    @property
    def required_scopes(self) -> list[str]:
        return ["identity", "submit", "read"]

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _user_agent(self) -> str:
        return self.credentials.get("user_agent") or DEFAULT_USER_AGENT

    def _headers(self, *, with_basic_auth: bool = False) -> dict[str, str]:
        headers = {"User-Agent": self._user_agent()}
        if with_basic_auth:
            client_id = self.credentials["client_id"]
            client_secret = self.credentials["client_secret"]
            encoded = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
            headers["Authorization"] = f"Basic {encoded}"
        return headers

    @staticmethod
    def _raise_on_thing_errors(body: dict, action: str, platform_name: str) -> dict:
        """Reddit's "thing" API returns 200 with errors in json.errors, not an HTTP error.

        Each entry is ``[code, message, field]``. Returns ``json.data`` on success.
        """
        payload = body.get("json", body)
        errors = payload.get("errors") or []
        if errors:
            raise PublishError(
                f"Reddit {action} failed: {errors}",
                platform=platform_name,
                raw_response=body,
            )
        return payload.get("data", {})

    # ------------------------------------------------------------------
    # OAuth
    # ------------------------------------------------------------------

    def get_auth_url(self, redirect_uri: str, state: str, code_verifier: str | None = None) -> str:
        params = {
            "client_id": self.credentials["client_id"],
            "redirect_uri": redirect_uri,
            "state": state,
            "scope": " ".join(self.required_scopes),
            "response_type": "code",
            # Without this Reddit issues an hour-long token and no refresh
            # token at all — see module docstring.
            "duration": "permanent",
        }
        return f"{AUTH_URL}?{urlencode(params)}"

    def exchange_code(self, code: str, redirect_uri: str, code_verifier: str | None = None) -> OAuthTokens:
        resp = self._request(
            "POST",
            TOKEN_URL,
            headers=self._headers(with_basic_auth=True),
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": redirect_uri,
            },
        )
        body = resp.json()
        if "access_token" not in body:
            raise OAuthError(
                f"Reddit token exchange failed: {body}",
                platform=self.platform_name,
                raw_response=body,
            )
        return OAuthTokens(
            access_token=body["access_token"],
            refresh_token=body.get("refresh_token"),
            expires_in=body.get("expires_in"),
            scope=body.get("scope"),
            raw_response=body,
        )

    def refresh_token(self, refresh_token: str) -> OAuthTokens:
        resp = self._request(
            "POST",
            TOKEN_URL,
            headers=self._headers(with_basic_auth=True),
            data={
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
            },
        )
        body = resp.json()
        if "access_token" not in body:
            raise OAuthError(
                f"Reddit token refresh failed: {body}",
                platform=self.platform_name,
                raw_response=body,
            )
        return OAuthTokens(
            access_token=body["access_token"],
            # Reddit's refresh response omits refresh_token (it doesn't rotate
            # it) — keep the one the caller already has.
            refresh_token=body.get("refresh_token", refresh_token),
            expires_in=body.get("expires_in"),
            scope=body.get("scope"),
            raw_response=body,
        )

    # ------------------------------------------------------------------
    # Profile
    # ------------------------------------------------------------------

    def get_profile(self, access_token: str) -> AccountProfile:
        resp = self._request(
            "GET",
            f"{API_BASE}/api/v1/me",
            access_token=access_token,
            headers=self._headers(),
        )
        data = resp.json()
        # Reddit HTML-escapes icon_img query-string ampersands.
        avatar = data.get("icon_img")
        return AccountProfile(
            platform_id=data.get("id", ""),
            name=data.get("name", ""),
            handle=data.get("name"),
            avatar_url=html.unescape(avatar) if avatar else None,
            extra={"total_karma": data.get("total_karma", 0)},
        )

    # ------------------------------------------------------------------
    # Publishing
    # ------------------------------------------------------------------

    def publish_post(self, access_token: str, content: PublishContent) -> PublishResult:
        if content.post_type not in (PostType.TEXT, PostType.LINK):
            raise PublishError(
                f"Reddit provider only supports TEXT and LINK posts (got {content.post_type})",
                platform=self.platform_name,
                retryable=False,
            )
        if content.media_urls or content.media_files:
            raise PublishError(
                "Reddit media upload is not implemented yet — post text or a "
                "link without media, or attach media through Reddit directly.",
                platform=self.platform_name,
                retryable=False,
            )
        subreddit = content.extra.get("subreddit")
        if not subreddit:
            raise PublishError(
                "subreddit is required in content.extra for Reddit posts",
                platform=self.platform_name,
                retryable=False,
            )

        title = (content.title or content.text or "")[:MAX_TITLE_LENGTH]
        if not title:
            raise PublishError(
                "Reddit posts require a title (content.title, or content.text as a fallback)",
                platform=self.platform_name,
                retryable=False,
            )

        payload = {
            "api_type": "json",
            "sr": subreddit,
            "title": title,
        }
        if content.post_type == PostType.LINK:
            payload["kind"] = "link"
            payload["url"] = content.link_url
            payload["resubmit"] = "true"
        else:
            payload["kind"] = "self"
            payload["text"] = content.text[:MAX_SELFTEXT_LENGTH]

        resp = self._request(
            "POST",
            f"{API_BASE}/api/submit",
            access_token=access_token,
            headers=self._headers(),
            data=payload,
        )
        data = self._raise_on_thing_errors(resp.json(), "submit", self.platform_name)
        post_id = data.get("id", "")
        return PublishResult(
            platform_post_id=post_id,
            url=data.get("url"),
            extra=data,
        )

    def publish_comment(self, access_token: str, post_id: str, text: str) -> CommentResult:
        """Post a top-level comment on ``post_id`` — Reddit's "first comment" analogue."""
        resp = self._request(
            "POST",
            f"{API_BASE}/api/comment",
            access_token=access_token,
            headers=self._headers(),
            data={
                "api_type": "json",
                "thing_id": f"t3_{post_id}",
                "text": text,
            },
        )
        data = self._raise_on_thing_errors(resp.json(), "comment", self.platform_name)
        things = data.get("things") or []
        comment_id = things[0].get("data", {}).get("id", "") if things else ""
        return CommentResult(platform_comment_id=comment_id)

    # ------------------------------------------------------------------
    # Analytics
    # ------------------------------------------------------------------

    def get_post_metrics(self, access_token: str, post_id: str) -> PostMetrics:
        resp = self._request(
            "GET",
            f"{API_BASE}/api/info",
            access_token=access_token,
            headers=self._headers(),
            params={"id": f"t3_{post_id}"},
        )
        children = resp.json().get("data", {}).get("children") or []
        if not children:
            return PostMetrics()
        post = children[0].get("data", {})
        return PostMetrics(
            likes=post.get("score", 0),
            comments=post.get("num_comments", 0),
            extra={"upvote_ratio": post.get("upvote_ratio")},
        )

    # ------------------------------------------------------------------
    # Token management
    # ------------------------------------------------------------------

    def revoke_token(self, access_token: str) -> bool:
        try:
            self._request(
                "POST",
                REVOKE_URL,
                headers=self._headers(with_basic_auth=True),
                data={"token": access_token, "token_type_hint": "access_token"},
            )
            return True
        except Exception:
            logger.exception("Failed to revoke Reddit token")
            return False
