"""Cross-module constants for the analytics app."""

from __future__ import annotations

# Platforms whose APIs don't expose aggregate analytics. The analytics
# page renders a per-platform "not available" variant instead of zeroed-
# out KPI cards and charts.
#
# The cron skips these because ``sync_all_account_analytics`` filters on
# ``services.analytics_availability``. Each entry MUST *also* have
# ``BACKFILL_DAYS_PER_PLATFORM[<platform>] = 0`` in ``apps/analytics/tasks.py``
# — that guard alone is not sufficient (it only skips the per-post loop, after
# the account-level fetch has already run), but it keeps the per-post window
# honest for any caller that reads the table directly.
NO_ANALYTICS_PLATFORMS: dict[str, str] = {
    "linkedin_personal": ("LinkedIn doesn't expose personal-profile analytics. Only Company Pages have analytics."),
    "bluesky": ("Bluesky's AT Protocol doesn't surface aggregate post analytics."),
    "mastodon": ("The Mastodon API doesn't expose aggregate post analytics."),
    # DevtoProvider implements no metrics methods, so the sync would call into
    # SocialProvider's NotImplementedError. Listed here rather than left to
    # AnalyticsPlatformConfig: a missing config row now reads as *enabled*, and
    # this is a capability gap, not an admin decision.
    "devto": ("Publishing to DEV.to is supported, but its analytics aren't wired up yet."),
    # XProvider/RedditProvider implement per-post metrics but not account-level
    # metrics (no account-analytics endpoint requested/available); SubstackProvider
    # implements neither. All three would hit SocialProvider's NotImplementedError.
    "x": ("X account-level analytics aren't implemented yet. Per-post metrics still work."),
    "reddit": ("Reddit has no account-level analytics for a connected user. Per-post metrics still work."),
    "substack": ("Publishing to Substack is supported, but its analytics aren't wired up yet."),
}
