"""Seed-time bulk path: pre-activated demo videos get attribution windows
(no activation moment exists on this path — the windows ARE the fiction)."""

from datetime import date, timedelta

from app.database.db import get_db_cursor, get_demo_anchor_date
from app.demo_data.attribution import screens_for_campaign
from app.demo_data.constants import DEMO_WINDOW_DAYS


class TestBulkSeedWindows:
    def test_every_activated_video_has_open_windows_per_screen(
        self, fresh_test_db
    ):
        with get_db_cursor() as cursor:
            anchor = date.fromisoformat(get_demo_anchor_date(cursor))
            expected_from = (
                anchor - timedelta(days=DEMO_WINDOW_DAYS - 1)
            ).isoformat()
            cursor.execute(
                "SELECT id, campaign_id FROM campaign_videos WHERE status = 'activated'"
            )
            activated = [(r["id"], r["campaign_id"]) for r in cursor.fetchall()]
            assert activated, "demo seed data provides activated videos"

            for video_id, ad_campaign_id in activated:
                cursor.execute(
                    "SELECT screen_id, active_from, active_to FROM video_attribution "
                    "WHERE video_id = ? ORDER BY screen_id",
                    (video_id,),
                )
                windows = cursor.fetchall()
                assert [w["screen_id"] for w in windows] == sorted(
                    screens_for_campaign(ad_campaign_id)
                )
                assert all(w["active_to"] is None for w in windows)
                assert all(
                    w["active_from"].startswith(expected_from) for w in windows
                )

    def test_bulk_metrics_match_join_over_stored_windows(self, fresh_test_db):
        from app.demo_data.derive import derive_video_metrics_rows
        from app.tools.review_tools import _load_attribution_windows

        with get_db_cursor() as cursor:
            anchor = date.fromisoformat(get_demo_anchor_date(cursor))
            cursor.execute(
                "SELECT id, campaign_id FROM campaign_videos "
                "WHERE status = 'activated' LIMIT 1"
            )
            row = cursor.fetchone()
            video_id, ad_campaign_id = row["id"], row["campaign_id"]
            windows = _load_attribution_windows(cursor, [video_id])
            cursor.execute(
                "SELECT metric_date, impressions, revenue FROM video_metrics "
                "WHERE video_id = ? ORDER BY metric_date",
                (video_id,),
            )
            stored = [tuple(r) for r in cursor.fetchall()]

        derived = derive_video_metrics_rows(
            ad_campaign_id,
            [video_id],
            anchor - timedelta(days=DEMO_WINDOW_DAYS - 1),
            anchor,
            windows=windows,
        )
        assert stored == [
            (r["metric_date"], r["impressions"], r["revenue"]) for r in derived
        ]
