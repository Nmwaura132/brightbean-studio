"""X (formerly Twitter) API v2 provider.

Text-only publishing. X's v2 media upload (INIT/APPEND/FINALIZE, with async
STATUS polling for video/GIF) is a materially different, harder-to-verify flow
than the image endpoints other providers here use — rather than ship a guess at
it, ``publish_post`` rejects media explicitly until it's built for real against
a live app. See ``supported_media_types``.

Auth is OAuth 2.0 Authorization Code with PKCE (RFC 7636, standard base64url
S256 — unlike TikTok, X does not deviate to hex). ``offline.access`` is
required in the scope list to receive a refresh token at all; without it the
access token expires in ~2 hours with no way to renew except a full reconnect.
"""

from __future__ import annotations

import base64
import hashlib
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

AUTH_URL = "https://x.com/i/oauth2/authorize"
API_BASE = os.environ.get("X_API_BASE", "https://api.x.com/2")
TOKEN_URL = f"{API_BASE}/oauth2/token"
REVOKE_URL = f"{API_BASE}/oauth2/revoke"


def _pkce_code_challenge(code_verifier: str) -> str:
    """Standard RFC 7636 S256 challenge: base64url(SHA256(verifier)), no padding."""
    digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


class XProvider(SocialProvider):
    """X API v2 provider using OAuth 2.0 with PKCE."""

    uses_pkce = True

    # ------------------------------------------------------------------
    # Metadata
    # ------------------------------------------------------------------

    @property
    def platform_name(self) -> str:
        return "X"

    @property
    def auth_type(self) -> AuthType:
        return AuthType.OAUTH2

    @property
    def max_caption_length(self) -> int:
        return 280

    @property
    def supported_post_types(self) -> list[PostType]:
        return [PostType.TEXT]

    @property
    def supported_media_types(self) -> list[MediaType]:
        # Empty on purpose — see module docstring. publish_post rejects media
        # explicitly rather than silently dropping it.
        return []

    @property
    def required_scopes(self) -> list[str]:
        return ["tweet.read", "tweet.write", "users.read", "offline.access"]

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _basic_auth_header(self) -> dict[str, str]:
        """HTTP Basic auth for the token endpoint, required for confidential clients.

        X's token endpoint also wants client_id in the body regardless of
        client type, so callers send both.
        """
        client_id = self.credentials["client_id"]
        client_secret = self.credentials["client_secret"]
        encoded = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
        return {"Authorization": f"Basic {encoded}"}

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
        }
        if code_verifier:
            params["code_challenge"] = _pkce_code_challenge(code_verifier)
            params["code_challenge_method"] = "S256"
        return f"{AUTH_URL}?{urlencode(params)}"

    def exchange_code(self, code: str, redirect_uri: str, code_verifier: str | None = None) -> OAuthTokens:
        data = {
            "client_id": self.credentials["client_id"],
            "code": code,
            "grant_type": "authorization_code",
            "redirect_uri": redirect_uri,
        }
        if code_verifier:
            data["code_verifier"] = code_verifier
        resp = self._request("POST", TOKEN_URL, headers=self._basic_auth_header(), data=data)
        body = resp.json()
        if "access_token" not in body:
            raise OAuthError(
                f"X token exchange failed: {body}",
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
            headers=self._basic_auth_header(),
            data={
                "client_id": self.credentials["client_id"],
                "refresh_token": refresh_token,
                "grant_type": "refresh_token",
            },
        )
        body = resp.json()
        if "access_token" not in body:
            raise OAuthError(
                f"X token refresh failed: {body}",
                platform=self.platform_name,
                raw_response=body,
            )
        return OAuthTokens(
            access_token=body["access_token"],
            # X rotates refresh tokens on every use; falling back to the old one
            # would silently break the next refresh if the response omits it.
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
            f"{API_BASE}/users/me",
            access_token=access_token,
            params={"user.fields": "profile_image_url,public_metrics"},
        )
        data = resp.json().get("data", {})
        metrics = data.get("public_metrics", {})
        return AccountProfile(
            platform_id=data.get("id", ""),
            name=data.get("name", ""),
            handle=data.get("username"),
            avatar_url=data.get("profile_image_url"),
            follower_count=metrics.get("followers_count", 0),
            extra=data,
        )

    # ------------------------------------------------------------------
    # Publishing
    # ------------------------------------------------------------------

    def publish_post(self, access_token: str, content: PublishContent) -> PublishResult:
        if content.post_type != PostType.TEXT:
            raise PublishError(
                f"X provider only supports TEXT posts (got {content.post_type})",
                platform=self.platform_name,
                retryable=False,
            )
        if content.media_urls or content.media_files:
            raise PublishError(
                "X media upload is not implemented yet — post text without media, "
                "or attach media through X directly.",
                platform=self.platform_name,
                retryable=False,
            )

        # X has no separate "link" field like Facebook/Pinterest's payload["link"]
        # — a link only ever reaches X inline in the tweet text (which X then
        # auto-unfurls into a card), so unlike those providers there is nothing
        # to attach here beyond the caption itself.
        text = content.text[: self.max_caption_length]

        resp = self._request(
            "POST",
            f"{API_BASE}/tweets",
            access_token=access_token,
            json={"text": text},
        )
        body = resp.json()
        post_id = body.get("data", {}).get("id", "")
        return PublishResult(
            platform_post_id=post_id,
            url=f"https://x.com/i/web/status/{post_id}" if post_id else None,
            extra=body.get("data", {}),
        )

    def publish_comment(self, access_token: str, post_id: str, text: str) -> CommentResult:
        """Post a reply tweet under ``post_id`` — X has no separate comment object."""
        resp = self._request(
            "POST",
            f"{API_BASE}/tweets",
            access_token=access_token,
            json={
                "text": text[: self.max_caption_length],
                "reply": {"in_reply_to_tweet_id": post_id},
            },
        )
        reply_id = resp.json().get("data", {}).get("id", "")
        return CommentResult(platform_comment_id=reply_id)

    # ------------------------------------------------------------------
    # Analytics
    # ------------------------------------------------------------------

    def get_post_metrics(self, access_token: str, post_id: str) -> PostMetrics:
        resp = self._request(
            "GET",
            f"{API_BASE}/tweets/{post_id}",
            access_token=access_token,
            params={"tweet.fields": "public_metrics"},
        )
        metrics = resp.json().get("data", {}).get("public_metrics", {})
        return PostMetrics(
            impressions=metrics.get("impression_count", 0),
            likes=metrics.get("like_count", 0),
            comments=metrics.get("reply_count", 0),
            shares=metrics.get("retweet_count", 0),
            extra={
                "quote_count": metrics.get("quote_count", 0),
                "bookmark_count": metrics.get("bookmark_count", 0),
            },
        )

    # ------------------------------------------------------------------
    # Token management
    # ------------------------------------------------------------------

    def revoke_token(self, access_token: str) -> bool:
        try:
            self._request(
                "POST",
                REVOKE_URL,
                headers=self._basic_auth_header(),
                data={
                    "token": access_token,
                    "client_id": self.credentials["client_id"],
                    "token_type_hint": "access_token",
                },
            )
            return True
        except Exception:
            logger.exception("Failed to revoke X token")
            return False
