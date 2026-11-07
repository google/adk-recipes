"""Tests for the demo_meta table and demo anchor-date helpers."""

from datetime import UTC, datetime

from app.database.db import (
    get_db_cursor,
    get_demo_anchor_date,
    set_demo_anchor_date,
)


class TestDemoAnchor:
    def test_first_read_seeds_todays_utc_date(self, fresh_test_db):
        with get_db_cursor() as cursor:
            anchor = get_demo_anchor_date(cursor)
        assert anchor == datetime.now(UTC).date().isoformat()

    def test_anchor_is_stable_across_reads(self, fresh_test_db):
        with get_db_cursor() as cursor:
            first = get_demo_anchor_date(cursor)
        with get_db_cursor() as cursor:
            second = get_demo_anchor_date(cursor)
        assert first == second

    def test_set_overwrites(self, fresh_test_db):
        with get_db_cursor() as cursor:
            set_demo_anchor_date(cursor, "2026-01-15")
        with get_db_cursor() as cursor:
            assert get_demo_anchor_date(cursor) == "2026-01-15"
        with get_db_cursor() as cursor:
            set_demo_anchor_date(cursor, "2026-02-01")
        with get_db_cursor() as cursor:
            assert get_demo_anchor_date(cursor) == "2026-02-01"


class TestSeededWindow:
    def test_seeded_metrics_fill_the_anchor_window(self, fresh_test_db):
        """populate_mock_data() must fill exactly [anchor-29, anchor] for
        every seeded activated video — the single windowing rule."""
        from datetime import date, timedelta

        with get_db_cursor() as cursor:
            anchor = date.fromisoformat(get_demo_anchor_date(cursor))
            cursor.execute(
                """
                SELECT video_id, MIN(metric_date) AS lo, MAX(metric_date) AS hi,
                       COUNT(*) AS n
                FROM video_metrics GROUP BY video_id
                """
            )
            groups = cursor.fetchall()
        assert groups, "seeded demo DB must contain metrics"
        for g in groups:
            assert g["lo"] == (anchor - timedelta(days=29)).isoformat()
            assert g["hi"] == anchor.isoformat()
            assert g["n"] == 30

    def test_reseeding_is_deterministic(self, fresh_test_db):
        """Wipe metrics and repopulate: byte-identical rows come back."""
        from app.database.mock_data import populate_mock_data

        def snapshot():
            with get_db_cursor() as cursor:
                cursor.execute(
                    """
                    SELECT video_id, metric_date, impressions, dwell_time_seconds,
                           circulation, revenue
                    FROM video_metrics ORDER BY video_id, metric_date
                    """
                )
                return [tuple(r) for r in cursor.fetchall()]

        first = snapshot()
        with get_db_cursor() as cursor:
            cursor.execute("DELETE FROM video_metrics")
        populate_mock_data()
        assert snapshot() == first
