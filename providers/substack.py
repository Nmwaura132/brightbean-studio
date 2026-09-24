"""Substack provider — built on Substack's UNOFFICIAL, undocumented API.

Substack's own 2026 Developer API (see README) only does creator profile
lookup; there is no first-party endpoint to publish a post. Everything below
talks to the same internal JSON API (``https://<publication>.substack.com/api/v1/*``)
the Substack web editor itself uses, authenticated with the operator's own
browser session cookies (``substack.sid`` and ``connect.sid``) instead of
OAuth — there is no OAuth app to register because Substack doesn't offer one.

This is real risk, not a formality: Substack can change or break these
endpoints without notice, and it's tied to a real person's actual account, not
an app-scoped token. Request shapes (both cookies, JSON-string ``draft_body``,
required ``draft_bylines``) follow jakub-k-slys/substack-gateway-oss, which is
e2e-tested against live Substack; the immediate ``/publish`` call is not
covered by that project, so treat the first real publish as its real test.

Auth type is SESSION, matching Bluesky/DEV.to: no ``get_auth_url`` /
``exchange_code`` — the operator pastes their session cookies directly. The
account's access_token holds both as JSON: ``{"substack_sid": ..., "connect_sid": ...}``.
"""

from __future__ import annotations

import json
import logging

from .base import SocialProvider
from .exceptions import OAuthError, PublishError
from .types import (
    AccountProfile,
    AuthType,
    MediaType,
    OAuthTokens,
    PostType,
    PublishContent,
    PublishResult,
)

logger = logging.getLogger(__name__)

# No platform-imposed length limit is documented for newsletter posts; this is
# just a sane upper bound, not a verified Substack figure.
MAX_BODY_LENGTH = 100_000


def _text_to_doc(text: str) -> dict:
    """Convert plain text to Substack's ProseMirror-style draft_body doc.

    One paragraph node per non-empty line. This is the minimal-viable shape
    cited across community reverse-engineering references — Substack's real
    editor produces much richer nodes (headings, links, images, embeds), none
    of which this builds.
    """
    paragraphs = [line for line in text.split("\n") if line.strip()] or [""]
    return {
        "type": "doc",
        "content": [{"type": "paragraph", "content": [{"type": "text", "text": line}]} for line in paragraphs],
    }


class SubstackProvider(SocialProvider):
    """Substack provider using a manually-supplied session cookie, not OAuth."""

    def __init__(self, credentials: dict | None = None):
        super().__init__(credentials)
        publication_url = self.credentials.get("publication_url", "")
        self.publication_url = publication_url.rstrip("/")

    # ------------------------------------------------------------------
    # Metadata
    # ------------------------------------------------------------------

    @property
    def platform_name(self) -> str:
        return "Substack"

    @property
    def auth_type(self) -> AuthType:
        return AuthType.SESSION

    @property
    def max_caption_length(self) -> int:
        return MAX_BODY_LENGTH

    @property
    def supported_post_types(self) -> list[PostType]:
        return [PostType.ARTICLE]

    @property
    def supported_media_types(self) -> list[MediaType]:
        # Empty on purpose — Substack's image upload is a separate,
        # undocumented endpoint not implemented here.
        return []

    @property
    def required_scopes(self) -> list[str]:
        return []  # session-based, no scopes

    # ------------------------------------------------------------------
    # OAuth stubs (not applicable for session auth)
    # ------------------------------------------------------------------

    def get_auth_url(self, redirect_uri: str, state: str, code_verifier: str | None = None) -> str:
        raise NotImplementedError("Substack uses a manually-supplied session cookie, not OAuth.")

    def exchange_code(self, code: str, redirect_uri: str, code_verifier: str | None = None) -> OAuthTokens:
        raise NotImplementedError("Substack uses a manually-supplied session cookie, not OAuth.")

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _headers(self, access_token: str) -> dict[str, str]:
        # Not a Bearer token — Substack authenticates the same way its own
        # browser editor does, via session cookies. Built manually rather
        # than passed as `access_token=` to `_request` so the base class
        # doesn't also attach an `Authorization: Bearer` header Substack
        # doesn't expect.
        cookies = json.loads(access_token)
        return {"Cookie": f"substack.sid={cookies['substack_sid']}; connect.sid={cookies['connect_sid']}"}

    def _get_own_user_id(self, headers: dict[str, str]) -> int:
        resp = self._request("GET", "https://substack.com/api/v1/user-settings", headers=headers)
        body = resp.json()
        settings = body.get("userSettings") or body.get("user_settings") or []
        if not settings:
            raise PublishError(
                "Substack returned no user settings — the session cookies may have expired.",
                platform=self.platform_name,
                raw_response=body,
            )
        return settings[0]["user_id"]

    def _require_publication_url(self) -> str:
        if not self.publication_url:
            raise PublishError(
                "Substack publication_url is not configured for this account.",
                platform=self.platform_name,
                retryable=False,
            )
        return self.publication_url

    # ------------------------------------------------------------------
    # Profile
    # ------------------------------------------------------------------

    def get_profile(self, access_token: str) -> AccountProfile:
        publication_url = self._require_publication_url()
        resp = self._request(
            "GET",
            f"{publication_url}/api/v1/subscription",
            headers=self._headers(access_token),
        )
        body = resp.json()
        publication = body.get("publication", {}) or {}
        if not publication:
            raise OAuthError(
                "Substack session cookie did not resolve to a publication — it may be "
                "expired, or this account isn't associated with the configured publication_url.",
                platform=self.platform_name,
                raw_response=body,
            )
        return AccountProfile(
            platform_id=str(publication.get("id", "")),
            name=publication.get("name", ""),
            handle=publication.get("subdomain"),
            avatar_url=publication.get("logo_url"),
            extra=publication,
        )

    # ------------------------------------------------------------------
    # Publishing
    # ------------------------------------------------------------------

    def publish_post(self, access_token: str, content: PublishContent) -> PublishResult:
        if content.post_type != PostType.ARTICLE:
            raise PublishError(
                f"Substack provider only supports ARTICLE posts (got {content.post_type})",
                platform=self.platform_name,
                retryable=False,
            )
        if content.media_urls or content.media_files:
            raise PublishError(
                "Substack media upload is not implemented yet — publish text "
                "only, or attach media through Substack directly.",
                platform=self.platform_name,
                retryable=False,
            )
        if not content.title:
            raise PublishError(
                "Substack posts require a title (content.title).",
                platform=self.platform_name,
                retryable=False,
            )

        publication_url = self._require_publication_url()
        headers = self._headers(access_token)
        user_id = self._get_own_user_id(headers)

        draft_payload = {
            "draft_title": content.title,
            "draft_subtitle": content.description or "",
            "draft_body": json.dumps(_text_to_doc(content.text[:MAX_BODY_LENGTH]), ensure_ascii=False),
            "draft_bylines": [{"id": user_id, "is_guest": False}],
            "type": "newsletter",
            "audience": "everyone",
        }
        draft_resp = self._request(
            "POST",
            f"{publication_url}/api/v1/drafts",
            headers=headers,
            json=draft_payload,
        )
        draft = draft_resp.json()
        draft_id = draft.get("id")
        if not draft_id:
            raise PublishError(
                f"Substack draft creation did not return an id: {draft}",
                platform=self.platform_name,
                raw_response=draft,
            )

        publish_resp = self._request(
            "POST",
            f"{publication_url}/api/v1/drafts/{draft_id}/publish",
            headers=headers,
            json={"send": True},
        )
        published = publish_resp.json()
        post_id = str(published.get("id", draft_id))
        return PublishResult(
            platform_post_id=post_id,
            url=published.get("canonical_url"),
            extra=published,
        )
