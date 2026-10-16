"""Schema + open/close semantics for the video_attribution windows table."""

from app.database.db import get_db_cursor


class TestVideoAttributionTable:
    def test_schema(self, fresh_test_db):
        with get_db_cursor() as cursor:
            cursor.execute("PRAGMA table_info(video_attribution)")
            cols = {row[1] for row in cursor.fetchall()}
        assert cols == {
            "id",
            "video_id",
            "ad_campaign_id",
            "screen_id",
            "active_from",
            "active_to",
        }

    def test_open_close_roundtrip(self, fresh_test_db):
        with get_db_cursor() as cursor:
            cursor.execute("SELECT id FROM campaign_videos LIMIT 1")
            video_id = cursor.fetchone()["id"]
            # Phase 10 bulk seed (mock_data.populate_mock_data) pre-opens
            # windows for every activated video; clear them so this test's
            # roundtrip is isolated to the single window it manages.
            cursor.execute(
                "DELETE FROM video_attribution WHERE video_id = ?", (video_id,)
            )
            cursor.execute(
                """
                INSERT INTO video_attribution
                (video_id, ad_campaign_id, screen_id, active_from, active_to)
                VALUES (?, ?, ?, ?, NULL)
                """,
                (video_id, 1, 101, "2026-06-01T00:00:00"),
            )
            cursor.execute(
                "SELECT COUNT(*) AS n FROM video_attribution "
                "WHERE video_id = ? AND active_to IS NULL",
                (video_id,),
            )
            assert cursor.fetchone()["n"] == 1
            cursor.execute(
                "UPDATE video_attribution SET active_to = ? "
                "WHERE video_id = ? AND active_to IS NULL",
                ("2026-06-15T00:00:00", video_id),
            )
            cursor.execute(
                "SELECT COUNT(*) AS n FROM video_attribution "
                "WHERE video_id = ? AND active_to IS NULL",
                (video_id,),
            )
            assert cursor.fetchone()["n"] == 0
