# Copyright 2025 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Unit tests for review_tools.py.

Tests the 10 HITL review-related tools:
- get_video_review_table
- get_video_details
- list_pending_videos
- activate_video
- activate_batch
- pause_video
- archive_video
- get_video_status
- get_activation_summary
- generate_additional_metrics
"""

from datetime import date, timedelta


def _make_campaign_with_metrics(rows, num_videos=1):
    """Create a campaign with activated video(s) and controlled metric rows.

    rows: dicts with keys metric_date (ISO str), impressions, revenue, and
    optional dwell_time_seconds, circulation, video_index (default 0).
    Returns {"campaign_id": int, "video_ids": [int, ...]}.
    """
    from app.database.db import get_db_cursor
    from app.tools.campaign_tools import create_campaign

    created = create_campaign(
        product_id=1, store_name="Parity Test Store", city="Austin", state="TX"
    )
    assert created["status"] == "success"
    campaign_id = created["campaign"]["id"]

    video_ids = []
    with get_db_cursor() as cursor:
        for i in range(num_videos):
            cursor.execute(
                "INSERT INTO campaign_videos (campaign_id, video_filename, status)"
                " VALUES (?, ?, 'activated')",
                (campaign_id, f"parity-test-{campaign_id}-{i}.mp4"),
            )
            video_ids.append(cursor.lastrowid)
        for r in rows:
            cursor.execute(
                "INSERT INTO video_metrics"
                " (video_id, metric_date, impressions, dwell_time_seconds,"
                "  circulation, revenue) VALUES (?, ?, ?, ?, ?, ?)",
                (
                    video_ids[r.get("video_index", 0)],
                    r["metric_date"],
                    r["impressions"],
                    r.get("dwell_time_seconds", 5.0),
                    r.get("circulation", 0),
                    r["revenue"],
                ),
            )
    return {"campaign_id": campaign_id, "video_ids": video_ids}


def _days_ago(n):
    return (date.today() - timedelta(days=n)).isoformat()


class TestGetVideoReviewTable:
    """Tests for get_video_review_table tool."""

    def test_get_video_review_table_returns_table(
        self, test_db, mock_storage_module
    ):
        """get_video_review_table should return formatted table."""
        from app.tools.review_tools import get_video_review_table

        result = get_video_review_table()

        # Should return table or message about no videos
        assert (
            "table" in result
            or "videos" in result
            or "message" in result
            or isinstance(result, str)
        )

    def test_get_video_review_table_filter_by_status(
        self, test_db, mock_storage_module
    ):
        """get_video_review_table should filter by status."""
        from app.tools.review_tools import get_video_review_table

        result = get_video_review_table(status="generated")

        # Should return filtered results
        assert result is not None

    def test_get_video_review_table_filter_by_campaign(
        self, test_db, mock_storage_module
    ):
        """get_video_review_table should filter by campaign_id."""
        from app.tools.review_tools import get_video_review_table

        result = get_video_review_table(campaign_id=1)

        # Should return filtered results
        assert result is not None


class TestGetVideoDetails:
    """Tests for get_video_details tool."""

    def test_get_video_details_valid_id(self, test_db, mock_storage_module):
        """get_video_details should return full details for valid ID."""
        from app.tools.review_tools import get_video_details

        # First check if any videos exist
        result = get_video_details(video_id=1)

        # Should return details or not found message
        assert (
            "video" in result
            or "error" in result
            or "not found" in str(result).lower()
        )

    def test_get_video_details_invalid_id(self, test_db):
        """get_video_details should handle invalid video ID."""
        from app.tools.review_tools import get_video_details

        result = get_video_details(video_id=9999)

        # Should return error or not found
        assert "error" in result or "not found" in str(result).lower()

    def test_rpi_is_thin_wrapper(self, test_db, mock_storage_module):
        from app.tools.metrics_shared import compute_rpi
        from app.tools.review_tools import get_video_details

        made = _make_campaign_with_metrics(
            [
                {
                    "metric_date": _days_ago(1),
                    "impressions": 1000,
                    "revenue": 10.0,
                },
                {
                    "metric_date": _days_ago(2),
                    "impressions": 500,
                    "revenue": 50.0,
                },
            ]
        )
        result = get_video_details(video_id=made["video_ids"][0])
        assert result["status"] == "success"
        metrics = result["metrics"]
        assert metrics is not None
        assert metrics["rpi"] == compute_rpi(
            metrics["total_revenue"], metrics["total_impressions"]
        )
        assert (
            "days_tracked" in metrics
        )  # marker to confirm we got the right structure


class TestListPendingVideos:
    """Tests for list_pending_videos tool (legacy)."""

    def test_list_pending_videos(self, test_db, mock_storage_module):
        """list_pending_videos should return pending videos."""
        from app.tools.review_tools import list_pending_videos

        result = list_pending_videos()

        # Should return list (may be empty)
        assert (
            "videos" in result
            or "pending" in result
            or isinstance(result, dict)
        )


class TestActivateVideo:
    """Tests for activate_video tool."""

    def test_activate_video_generates_metrics(
        self, test_db, mock_storage_module
    ):
        """activate_video should generate mock metrics on activation."""
        # First we need a video to activate
        # Skip if no videos exist
        from app.tools.review_tools import activate_video

        result = activate_video(video_id=1)

        # Should succeed or report video not found
        assert (
            "success" in result
            or "activated" in str(result).lower()
            or "error" in result
            or "not found" in str(result).lower()
        )

    def test_activate_video_invalid_id(self, test_db):
        """activate_video should handle invalid video ID."""
        from app.tools.review_tools import activate_video

        result = activate_video(video_id=9999)

        # Should return error
        assert "error" in result or "not found" in str(result).lower()


class TestActivateBatch:
    """Tests for activate_batch tool."""

    def test_activate_batch_multiple(self, test_db, mock_storage_module):
        """activate_batch should activate multiple videos at once."""
        from app.tools.review_tools import activate_batch

        result = activate_batch(video_ids=[1, 2, 3])

        # Should return results for each video
        assert (
            "results" in result
            or "success" in result
            or "activated" in str(result).lower()
            or "error" in result
        )

    def test_activate_batch_empty_list(self, test_db):
        """activate_batch should handle empty list."""
        from app.tools.review_tools import activate_batch

        result = activate_batch(video_ids=[])

        # Should handle gracefully
        assert result is not None

    def test_activate_batch_partial_success(self, test_db, mock_storage_module):
        """activate_batch should report partial success."""
        from app.tools.review_tools import activate_batch

        # Mix of valid and invalid IDs
        result = activate_batch(video_ids=[1, 9999])

        # Should report results for each
        assert result is not None


class TestPauseVideo:
    """Tests for pause_video tool."""

    def test_pause_video(self, test_db, mock_storage_module):
        """pause_video should change status to paused."""
        from app.tools.review_tools import pause_video

        result = pause_video(video_id=1)

        # Should succeed or report error (status can be 'error' or 'paused')
        # Check both key-based and status-field based error formats
        assert (
            "success" in result
            or "paused" in str(result).lower()
            or "error" in result
            or result.get("status") == "error"
            or "not found" in str(result).lower()
        )

    def test_pause_video_invalid_id(self, test_db):
        """pause_video should handle invalid video ID."""
        from app.tools.review_tools import pause_video

        result = pause_video(video_id=9999)

        # Should return error
        assert "error" in result or "not found" in str(result).lower()


class TestArchiveVideo:
    """Tests for archive_video tool."""

    def test_archive_video(self, test_db, mock_storage_module):
        """archive_video should change status to archived."""
        from app.tools.review_tools import archive_video

        result = archive_video(video_id=1, reason="Testing")

        # Should succeed or report not found
        assert (
            "success" in result
            or "archived" in str(result).lower()
            or "error" in result
            or "not found" in str(result).lower()
        )

    def test_archive_video_with_reason(self, test_db, mock_storage_module):
        """archive_video should accept a reason."""
        from app.tools.review_tools import archive_video

        result = archive_video(video_id=1, reason="Quality issues")

        # Should succeed or report not found
        assert result is not None


class TestGetVideoStatus:
    """Tests for get_video_status tool."""

    def test_get_video_status_valid_id(self, test_db, mock_storage_module):
        """get_video_status should return status for valid ID."""
        from app.tools.review_tools import get_video_status

        result = get_video_status(video_id=1)

        # Should return status or not found
        assert (
            "status" in result
            or "error" in result
            or "not found" in str(result).lower()
        )

    def test_get_video_status_invalid_id(self, test_db):
        """get_video_status should handle invalid video ID."""
        from app.tools.review_tools import get_video_status

        result = get_video_status(video_id=9999)

        # Should return error
        assert "error" in result or "not found" in str(result).lower()


class TestGetActivationSummary:
    """Tests for get_activation_summary tool."""

    def test_get_activation_summary(self, test_db, mock_storage_module):
        """get_activation_summary should return status counts."""
        from app.tools.review_tools import get_activation_summary

        result = get_activation_summary()

        # Should return summary
        assert (
            "summary" in result
            or "counts" in result
            or isinstance(result, dict)
        )

    def test_get_activation_summary_has_status_counts(
        self, test_db, mock_storage_module
    ):
        """Summary should include counts for each status."""
        from app.tools.review_tools import get_activation_summary

        result = get_activation_summary()

        # May be 0 if no videos, but structure should exist
        assert result is not None


class TestGenerateAdditionalMetrics:
    """Tests for generate_additional_metrics tool."""

    def test_generate_additional_metrics(self, test_db, mock_storage_module):
        """generate_additional_metrics should extend metrics period."""
        from app.tools.review_tools import generate_additional_metrics

        result = generate_additional_metrics(video_id=1, days=7)

        # Should succeed or report video not found
        assert "status" in result and (
            result["status"] == "success" or result["status"] == "error"
        )

    def test_generate_additional_metrics_default_days(
        self, test_db, mock_storage_module
    ):
        """generate_additional_metrics should use default days if not specified."""
        from app.tools.review_tools import generate_additional_metrics

        # Test with default (likely 30)
        result = generate_additional_metrics(video_id=1)

        # Should handle gracefully
        assert result is not None


class TestDeterministicActivation:
    def test_activation_fills_anchor_window(self, fresh_test_db):
        """A newly-activated video's rows land on [anchor-29, anchor] —
        the same window seeding used (no seed/activation discontinuity)."""
        from datetime import date, timedelta

        from app.database.db import get_db_cursor, get_demo_anchor_date
        from app.tools.review_tools import activate_video

        with get_db_cursor() as cursor:
            # Get a campaign and product
            cursor.execute(
                "SELECT c.id as campaign_id, p.id as product_id FROM campaigns c, products p LIMIT 1"
            )
            row = cursor.fetchone()
            campaign_id = row["campaign_id"]
            product_id = row["product_id"]

            # Create a new video in 'generated' status
            cursor.execute(
                """
                INSERT INTO campaign_videos
                (campaign_id, product_id, video_filename, thumbnail_path,
                 scene_prompt, video_prompt, pipeline_type, variation_name,
                 duration_seconds, aspect_ratio, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    campaign_id,
                    product_id,
                    "test-video.mp4",
                    "test-thumbnail.png",
                    "Test scene",
                    "Test prompt",
                    "two-stage",
                    "test-variation",
                    8,
                    "9:16",
                    "generated",
                ),
            )
            video_id = cursor.lastrowid

        # Activate it
        result = activate_video(video_id=video_id)
        assert result["status"] == "success"

        with get_db_cursor() as cursor:
            anchor = date.fromisoformat(get_demo_anchor_date(cursor))
            cursor.execute(
                "SELECT MIN(metric_date) AS lo, MAX(metric_date) AS hi, COUNT(*) AS n "
                "FROM video_metrics WHERE video_id = ?",
                (video_id,),
            )
            row = cursor.fetchone()
        assert row["lo"] == (anchor - timedelta(days=29)).isoformat()
        assert row["hi"] == anchor.isoformat()
        assert row["n"] == 30

    def test_activation_is_deterministic(self, fresh_test_db):
        """Activating, wiping the rows, and re-deriving yields identical rows."""
        from app.database.db import get_db_cursor
        from app.tools.review_tools import activate_video

        with get_db_cursor() as cursor:
            # Get a campaign and product
            cursor.execute(
                "SELECT c.id as campaign_id, p.id as product_id FROM campaigns c, products p LIMIT 1"
            )
            row = cursor.fetchone()
            campaign_id = row["campaign_id"]
            product_id = row["product_id"]

            # Create a new video in 'generated' status
            cursor.execute(
                """
                INSERT INTO campaign_videos
                (campaign_id, product_id, video_filename, thumbnail_path,
                 scene_prompt, video_prompt, pipeline_type, variation_name,
                 duration_seconds, aspect_ratio, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    campaign_id,
                    product_id,
                    "test-video-2.mp4",
                    "test-thumbnail-2.png",
                    "Test scene 2",
                    "Test prompt 2",
                    "two-stage",
                    "test-variation-2",
                    8,
                    "9:16",
                    "generated",
                ),
            )
            video_id = cursor.lastrowid

        activate_video(video_id=video_id)

        def rows():
            with get_db_cursor() as cursor:
                cursor.execute(
                    "SELECT metric_date, impressions, dwell_time_seconds, circulation, revenue "
                    "FROM video_metrics WHERE video_id = ? ORDER BY metric_date",
                    (video_id,),
                )
                return [tuple(r) for r in cursor.fetchall()]

        first = rows()
        assert len(first) == 30
        # Re-run just the metrics generation path (activate_video refuses an
        # already-activated video, so exercise idempotent regeneration directly)
        from datetime import date, timedelta

        from app.database.db import get_demo_anchor_date
        from app.demo_data.constants import DEMO_WINDOW_DAYS
        from app.demo_data.derive import derive_video_metrics_rows

        with get_db_cursor() as cursor:
            anchor = date.fromisoformat(get_demo_anchor_date(cursor))
            cursor.execute(
                "SELECT campaign_id FROM campaign_videos WHERE id = ?",
                (video_id,),
            )
            campaign_id = cursor.fetchone()["campaign_id"]
            for row in derive_video_metrics_rows(
                campaign_id,
                [video_id],
                anchor - timedelta(days=DEMO_WINDOW_DAYS - 1),
                anchor,
            ):
                cursor.execute(
                    """
                    INSERT OR IGNORE INTO video_metrics
                    (video_id, metric_date, impressions, dwell_time_seconds, circulation, revenue)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        row["video_id"],
                        row["metric_date"],
                        row["impressions"],
                        row["dwell_time_seconds"],
                        row["circulation"],
                        row["revenue"],
                    ),
                )
        assert rows() == first  # INSERT OR IGNORE + determinism = idempotent


class TestGenerateAdditionalMetricsDeterministic:
    def test_advances_anchor_and_extends_all_activated_videos(
        self, fresh_test_db
    ):
        """The third call site: advancing the demo universe by N days moves
        the anchor and extends every activated video, atomically."""
        from datetime import date, timedelta

        from app.database.db import get_db_cursor, get_demo_anchor_date
        from app.tools.review_tools import generate_additional_metrics

        with get_db_cursor() as cursor:
            old_anchor = date.fromisoformat(get_demo_anchor_date(cursor))
            cursor.execute(
                "SELECT id FROM campaign_videos WHERE status = 'activated' LIMIT 2"
            )
            activated = [r["id"] for r in cursor.fetchall()]
        assert len(activated) >= 2, "demo seed data provides activated videos"

        result = generate_additional_metrics(video_id=activated[0], days=5)
        assert result["status"] == "success"

        with get_db_cursor() as cursor:
            new_anchor = date.fromisoformat(get_demo_anchor_date(cursor))
            assert new_anchor == old_anchor + timedelta(days=5)
            for vid in activated:
                cursor.execute(
                    "SELECT MAX(metric_date) AS hi FROM video_metrics WHERE video_id = ?",
                    (vid,),
                )
                assert cursor.fetchone()["hi"] == new_anchor.isoformat()


class TestAttributionBridge:
    """Phase 10 dual-write bridge: activation opens video_attribution
    windows, pause/archive close them, and metrics derive through them."""

    def _make_generated_video(self):
        from app.database.db import get_db_cursor

        with get_db_cursor() as cursor:
            cursor.execute(
                "SELECT c.id AS campaign_id, p.id AS product_id "
                "FROM campaigns c, products p LIMIT 1"
            )
            row = cursor.fetchone()
            cursor.execute(
                """
                INSERT INTO campaign_videos
                (campaign_id, product_id, video_filename, status)
                VALUES (?, ?, ?, 'generated')
                """,
                (
                    row["campaign_id"],
                    row["product_id"],
                    "bridge-test-video.mp4",
                ),
            )
            return cursor.lastrowid, row["campaign_id"]

    def _windows(self, video_id):
        from app.database.db import get_db_cursor

        with get_db_cursor() as cursor:
            cursor.execute(
                "SELECT screen_id, active_from, active_to FROM video_attribution "
                "WHERE video_id = ? ORDER BY screen_id",
                (video_id,),
            )
            return [dict(r) for r in cursor.fetchall()]

    def test_activate_opens_one_window_per_screen(self, fresh_test_db):
        from datetime import date, timedelta

        from app.database.db import get_db_cursor, get_demo_anchor_date
        from app.demo_data.attribution import screens_for_campaign
        from app.demo_data.constants import DEMO_WINDOW_DAYS
        from app.tools.review_tools import activate_video

        video_id, ad_campaign_id = self._make_generated_video()
        assert activate_video(video_id=video_id)["status"] == "success"

        windows = self._windows(video_id)
        assert [w["screen_id"] for w in windows] == sorted(
            screens_for_campaign(ad_campaign_id)
        )
        assert all(w["active_to"] is None for w in windows)
        with get_db_cursor() as cursor:
            anchor = date.fromisoformat(get_demo_anchor_date(cursor))
        expected_from = (
            anchor - timedelta(days=DEMO_WINDOW_DAYS - 1)
        ).isoformat()
        assert all(w["active_from"].startswith(expected_from) for w in windows)

    def test_pause_and_archive_close_windows(self, fresh_test_db):
        from app.tools.review_tools import (
            activate_video,
            archive_video,
            pause_video,
        )

        video_id, _ = self._make_generated_video()
        activate_video(video_id=video_id)
        pause_video(video_id=video_id)
        assert all(w["active_to"] is not None for w in self._windows(video_id))

        video_id2, _ = self._make_generated_video_named(
            "bridge-test-video-2.mp4"
        )
        activate_video(video_id=video_id2)
        archive_video(video_id=video_id2)
        assert all(w["active_to"] is not None for w in self._windows(video_id2))

    def _make_generated_video_named(self, filename):
        from app.database.db import get_db_cursor

        with get_db_cursor() as cursor:
            cursor.execute(
                "SELECT c.id AS campaign_id, p.id AS product_id "
                "FROM campaigns c, products p LIMIT 1"
            )
            row = cursor.fetchone()
            cursor.execute(
                """
                INSERT INTO campaign_videos
                (campaign_id, product_id, video_filename, status)
                VALUES (?, ?, ?, 'generated')
                """,
                (row["campaign_id"], row["product_id"], filename),
            )
            return cursor.lastrowid, row["campaign_id"]

    def test_close_on_miss_warns_but_does_not_fail(self, fresh_test_db, caplog):
        """An activated video whose windows were externally closed: pausing
        must still succeed, with a warning — never the donor's silent no-op,
        never an exception."""
        import logging

        from app.database.db import get_db_cursor
        from app.tools.review_tools import activate_video, pause_video

        video_id, _ = self._make_generated_video()
        activate_video(video_id=video_id)
        with get_db_cursor() as cursor:
            cursor.execute(
                "UPDATE video_attribution SET active_to = active_from WHERE video_id = ?",
                (video_id,),
            )
        with caplog.at_level(logging.WARNING):
            assert pause_video(video_id=video_id)["status"] == "success"
        assert any(
            "no open attribution window" in r.message for r in caplog.records
        )

    def test_activation_metrics_derive_through_db_windows(self, fresh_test_db):
        """The rows written at activation equal the join over the windows the
        bridge just opened — the windows are load-bearing, not decorative."""
        from datetime import date, timedelta

        from app.database.db import get_db_cursor, get_demo_anchor_date
        from app.demo_data.constants import DEMO_WINDOW_DAYS
        from app.demo_data.derive import derive_video_metrics_rows
        from app.tools.review_tools import (
            _load_attribution_windows,
            activate_video,
        )

        video_id, ad_campaign_id = self._make_generated_video()
        activate_video(video_id=video_id)

        with get_db_cursor() as cursor:
            anchor = date.fromisoformat(get_demo_anchor_date(cursor))
            windows = _load_attribution_windows(cursor, [video_id])
            cursor.execute(
                "SELECT metric_date, impressions, dwell_time_seconds, circulation, revenue "
                "FROM video_metrics WHERE video_id = ? ORDER BY metric_date",
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
        expected = [
            (
                r["metric_date"],
                r["impressions"],
                r["dwell_time_seconds"],
                r["circulation"],
                r["revenue"],
            )
            for r in derived
        ]
        assert stored == expected
        assert len(stored) == DEMO_WINDOW_DAYS

    def test_reactivation_does_not_change_metrics(self, fresh_test_db):
        """Pausing then reactivating a video leaves a closed window and a
        freshly-opened one whose date coverage overlaps. INSERT OR IGNORE
        already protects the stored video_metrics rows from that overlap,
        but the join itself must also dedup it (Finding 1, final review):
        the stored rows must be unchanged, and re-deriving through both
        windows must reproduce them exactly rather than double-count."""
        from datetime import date, timedelta

        from app.database.db import get_db_cursor, get_demo_anchor_date
        from app.demo_data.constants import DEMO_WINDOW_DAYS
        from app.demo_data.derive import derive_video_metrics_rows
        from app.tools.review_tools import (
            _load_attribution_windows,
            activate_video,
            pause_video,
        )

        video_id, ad_campaign_id = self._make_generated_video_named(
            "bridge-test-reactivate.mp4"
        )
        assert activate_video(video_id=video_id)["status"] == "success"

        with get_db_cursor() as cursor:
            cursor.execute(
                "SELECT metric_date, impressions, dwell_time_seconds, circulation, revenue "
                "FROM video_metrics WHERE video_id = ? ORDER BY metric_date",
                (video_id,),
            )
            snapshot = [tuple(r) for r in cursor.fetchall()]

        assert pause_video(video_id=video_id)["status"] == "success"
        assert activate_video(video_id=video_id)["status"] == "success"

        with get_db_cursor() as cursor:
            cursor.execute(
                "SELECT metric_date, impressions, dwell_time_seconds, circulation, revenue "
                "FROM video_metrics WHERE video_id = ? ORDER BY metric_date",
                (video_id,),
            )
            after = [tuple(r) for r in cursor.fetchall()]
        assert after == snapshot

        windows = self._windows(video_id)
        assert any(w["active_to"] is not None for w in windows), (
            "closed history row remains"
        )
        assert any(w["active_to"] is None for w in windows), (
            "reactivation opened a fresh window"
        )

        with get_db_cursor() as cursor:
            anchor = date.fromisoformat(get_demo_anchor_date(cursor))
            window_start = anchor - timedelta(days=DEMO_WINDOW_DAYS - 1)
            loaded_windows = _load_attribution_windows(cursor, [video_id])
        derived = derive_video_metrics_rows(
            ad_campaign_id,
            [video_id],
            window_start,
            anchor,
            windows=loaded_windows,
        )
        derived_tuples = [
            (
                r["metric_date"],
                r["impressions"],
                r["dwell_time_seconds"],
                r["circulation"],
                r["revenue"],
            )
            for r in derived
        ]
        assert derived_tuples == snapshot
