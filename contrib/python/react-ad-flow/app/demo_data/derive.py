"""Signature-stable facade over the Phase 10 ad-play join.

Kept so both historical call sites (review_tools._insert_derived_metrics,
mock_data seed-time bulk generation) and any older DB keep working: callers
that pass real attribution windows get the honest join; callers that don't
get synthesized always-open windows across the campaign's screens — the
same rows the join produces for a video live all range on every screen.

The per-video-per-day INVENTIONS that used to live here are gone:
video_fraction is replaced by the deterministic play schedule
(attribution.slot_budget + expand_ad_plays), and the 1:1 campaign-as-screen
proxy by attribution.screens_for_campaign. campaign_uplift and video_rpi
moved to attribution.py (re-exported here unchanged).
"""

from datetime import date, datetime

from .attribution import (
    campaign_uplift,  # noqa: F401  (re-export: historical import site)
    derive_rows_from_windows,
    screens_for_campaign,
    video_rpi,  # noqa: F401  (re-export: historical import site)
)


def derive_video_metrics_rows(
    ad_campaign_id: int,
    video_ids: list,
    date_from: date,
    date_to: date,
    windows: list[dict] | None = None,
) -> list[dict]:
    """One video_metrics row per (video, day covered by a window), via the
    ad-play join. Videos with no window in `windows` get synthesized
    always-open windows from date_from on all campaign screens."""
    windows = list(windows or [])
    have = {w["video_id"] for w in windows}
    screens = screens_for_campaign(ad_campaign_id)
    for video_id in video_ids:
        if video_id not in have:
            for screen_id in screens:
                windows.append(
                    {
                        "video_id": video_id,
                        "screen_id": screen_id,
                        "active_from": datetime.combine(
                            date_from, datetime.min.time()
                        ),
                        "active_to": None,
                    }
                )
    return derive_rows_from_windows(
        ad_campaign_id, video_ids, windows, date_from, date_to
    )
