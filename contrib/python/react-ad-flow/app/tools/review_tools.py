# Copyright 2026 Google LLC
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

"""HITL (Human-in-the-Loop) Review and Activation Tools.

This module implements the video activation workflow where:
1. Videos are generated with status='generated'
2. Users review pending videos (thumbnails visible)
3. Selected videos are activated to go live
4. Mock metrics are only generated after activation

Video Lifecycle:
    generated → (HITL Review) → activated → (metrics start)
        ↓                           ↓
      archived                    paused
"""

import json
import logging
from datetime import date, datetime, timedelta
from typing import Any

from ..database.db import (
    get_db_cursor,
    get_demo_anchor_date,
    set_demo_anchor_date,
)
from ..demo_data.attribution import screens_for_campaign
from ..demo_data.constants import DEMO_WINDOW_DAYS
from ..demo_data.derive import derive_video_metrics_rows

# Shared with mock_data (ws10 carried item 2); the underscore alias keeps
# the historical import site (tests import it from here) working.
from ..demo_data.windows import (
    load_attribution_windows as _load_attribution_windows,
)
from .metrics_shared import compute_rpi

logger = logging.getLogger(__name__)


def list_pending_videos(
    campaign_id: int | None = None, limit: int = 10
) -> dict:
    """List videos awaiting activation (status='generated').

    Shows videos that have been generated but not yet pushed live.
    Includes thumbnail paths for visual review.

    Args:
        campaign_id: Optional campaign filter
        limit: Maximum number of videos to return

    Returns:
        Dictionary with list of pending videos
    """
    with get_db_cursor() as cursor:
        if campaign_id:
            cursor.execute(
                """
                SELECT cv.*, c.name as campaign_name, p.name as product_name
                FROM campaign_videos cv
                JOIN campaigns c ON cv.campaign_id = c.id
                LEFT JOIN products p ON cv.product_id = p.id
                WHERE cv.status = 'generated' AND cv.campaign_id = ?
                ORDER BY cv.created_at DESC
                LIMIT ?
            """,
                (campaign_id, limit),
            )
        else:
            cursor.execute(
                """
                SELECT cv.*, c.name as campaign_name, p.name as product_name
                FROM campaign_videos cv
                JOIN campaigns c ON cv.campaign_id = c.id
                LEFT JOIN products p ON cv.product_id = p.id
                WHERE cv.status = 'generated'
                ORDER BY cv.created_at DESC
                LIMIT ?
            """,
                (limit,),
            )

        rows = cursor.fetchall()

        videos = []
        for row in rows:
            variation_params = None
            if row["variation_params"]:
                try:
                    variation_params = json.loads(row["variation_params"])
                except json.JSONDecodeError:
                    pass

            videos.append(
                {
                    "id": row["id"],
                    "campaign_id": row["campaign_id"],
                    "campaign_name": row["campaign_name"],
                    "product_id": row["product_id"],
                    "product_name": row["product_name"],
                    "video_filename": row["video_filename"],
                    "thumbnail_path": row["thumbnail_path"],
                    "variation_name": row["variation_name"],
                    "variation_params": variation_params,
                    "duration_seconds": row["duration_seconds"],
                    "created_at": row["created_at"],
                    "generation_time_seconds": row["generation_time_seconds"],
                }
            )

        return {
            "status": "success",
            "pending_count": len(videos),
            "videos": videos,
            "message": f"Found {len(videos)} videos awaiting activation"
            + (f" for campaign {campaign_id}" if campaign_id else ""),
        }


def _open_attribution_windows(
    cursor, video_id, ad_campaign_id, active_from
) -> int:
    """Open one window per campaign screen (skip screens already open).

    Reactivation after a pause intentionally opens fresh windows alongside
    the closed history row left by the pause, even though their date
    coverage can overlap — the join (attribution.derive_rows_from_windows)
    is responsible for deduping that overlap, not this bridge."""
    opened = 0
    for screen_id in screens_for_campaign(ad_campaign_id):
        cursor.execute(
            "SELECT 1 FROM video_attribution "
            "WHERE video_id = ? AND screen_id = ? AND active_to IS NULL",
            (video_id, screen_id),
        )
        if cursor.fetchone():
            continue
        cursor.execute(
            "INSERT INTO video_attribution "
            "(video_id, ad_campaign_id, screen_id, active_from, active_to) "
            "VALUES (?, ?, ?, ?, NULL)",
            (video_id, ad_campaign_id, screen_id, active_from.isoformat()),
        )
        opened += 1
    return opened


def _close_attribution_windows(
    cursor, video_id, active_to, expect_open: bool
) -> int:
    """Close all open windows. Unlike the donor bridge's silent no-op, a miss
    where windows were expected logs a warning."""
    cursor.execute(
        "UPDATE video_attribution SET active_to = ? "
        "WHERE video_id = ? AND active_to IS NULL",
        (active_to.isoformat(), video_id),
    )
    closed = cursor.rowcount
    if closed == 0 and expect_open:
        logger.warning(
            "no open attribution window to close for video %s", video_id
        )
    return closed


def _insert_derived_metrics(
    cursor, ad_campaign_id, video_ids, date_from, date_to
) -> int:
    """Insert join-derived deterministic rows; INSERT OR IGNORE keeps
    overlapping regeneration idempotent (UNIQUE(video_id, metric_date)).
    Videos with no stored windows (pre-bridge rows) fall back to the
    facade's synthesized always-open windows."""
    windows = _load_attribution_windows(cursor, video_ids)
    inserted = 0
    for row in derive_video_metrics_rows(
        ad_campaign_id, video_ids, date_from, date_to, windows=windows
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
        inserted += cursor.rowcount if cursor.rowcount > 0 else 0
    return inserted


def activate_video(video_id: int, activated_by: str = "user") -> dict:
    """Activate a video to push it live.

    This:
    1. Changes status from 'generated' to 'activated'
    2. Records activation timestamp and user
    3. Generates mock metrics starting from today

    Args:
        video_id: The video ID to activate
        activated_by: Who activated (for audit trail)

    Returns:
        Dictionary with activation result and generated metrics count
    """
    with get_db_cursor() as cursor:
        # Check if video exists and is in 'generated' status
        cursor.execute(
            """
            SELECT cv.*, c.name as campaign_name, p.name as product_name
            FROM campaign_videos cv
            JOIN campaigns c ON cv.campaign_id = c.id
            LEFT JOIN products p ON cv.product_id = p.id
            WHERE cv.id = ?
        """,
            (video_id,),
        )

        video = cursor.fetchone()
        if not video:
            return {"status": "error", "message": f"Video {video_id} not found"}

        if video["status"] == "activated":
            return {
                "status": "error",
                "message": f"Video {video_id} is already activated",
            }

        if video["status"] not in ["generated", "paused"]:
            return {
                "status": "error",
                "message": f"Video {video_id} cannot be activated (status: {video['status']})",
            }

        # Update status to activated
        now = datetime.now().isoformat()
        cursor.execute(
            """
            UPDATE campaign_videos
            SET status = 'activated', activated_at = ?, activated_by = ?
            WHERE id = ?
        """,
            (now, activated_by, video_id),
        )

        # Open attribution windows across the campaign's screens for the
        # whole demo history window (the 30-day backfill fiction), THEN
        # derive metrics through them — the join only accrues where a
        # window covers.
        anchor = date.fromisoformat(get_demo_anchor_date(cursor))
        window_start = anchor - timedelta(days=DEMO_WINDOW_DAYS - 1)
        _open_attribution_windows(
            cursor,
            video_id,
            video["campaign_id"],
            datetime.combine(window_start, datetime.min.time()),
        )
        metrics_generated = _insert_derived_metrics(
            cursor,
            video["campaign_id"],
            [video_id],
            window_start,
            anchor,
        )

        return {
            "status": "success",
            "message": "Video activated successfully and is now live",
            "video": {
                "id": video_id,
                "video_filename": video["video_filename"],
                "campaign_name": video["campaign_name"],
                "product_name": video["product_name"],
                "variation_name": video["variation_name"],
                "activated_at": now,
                "activated_by": activated_by,
                "metrics_generated": metrics_generated,
            },
        }


def activate_batch(video_ids: list[int], activated_by: str = "user") -> dict:
    """Activate multiple videos at once.

    Args:
        video_ids: List of video IDs to activate
        activated_by: Who activated (for audit trail)

    Returns:
        Dictionary with batch activation results
    """
    results = []
    success_count = 0
    error_count = 0

    for video_id in video_ids:
        result = activate_video(video_id, activated_by)
        results.append(
            {
                "video_id": video_id,
                "status": result["status"],
                "message": result.get("message", ""),
            }
        )

        if result["status"] == "success":
            success_count += 1
        else:
            error_count += 1

    return {
        "status": "success" if error_count == 0 else "partial",
        "message": f"Activated {success_count} videos"
        + (f", {error_count} failed" if error_count > 0 else ""),
        "success_count": success_count,
        "error_count": error_count,
        "results": results,
    }


def pause_video(video_id: int) -> dict:
    """Pause an activated video.

    Stops new metrics from being generated but preserves existing data.

    Args:
        video_id: The video ID to pause

    Returns:
        Dictionary with pause result
    """
    with get_db_cursor() as cursor:
        cursor.execute(
            """
            SELECT cv.*, c.name as campaign_name
            FROM campaign_videos cv
            JOIN campaigns c ON cv.campaign_id = c.id
            WHERE cv.id = ?
        """,
            (video_id,),
        )

        video = cursor.fetchone()
        if not video:
            return {"status": "error", "message": f"Video {video_id} not found"}

        if video["status"] != "activated":
            return {
                "status": "error",
                "message": f"Video {video_id} is not activated (status: {video['status']})",
            }

        cursor.execute(
            """
            UPDATE campaign_videos
            SET status = 'paused'
            WHERE id = ?
        """,
            (video_id,),
        )

        _close_attribution_windows(
            cursor, video_id, datetime.now(), expect_open=True
        )

        return {
            "status": "success",
            "message": "Video paused successfully",
            "video": {
                "id": video_id,
                "video_filename": video["video_filename"],
                "campaign_name": video["campaign_name"],
                "new_status": "paused",
            },
        }


def archive_video(video_id: int, reason: str | None = None) -> dict:
    """Archive a video (reject/remove from consideration).

    Archived videos are not shown in pending lists and cannot be activated.

    Args:
        video_id: The video ID to archive
        reason: Optional reason for archiving

    Returns:
        Dictionary with archive result
    """
    with get_db_cursor() as cursor:
        cursor.execute(
            """
            SELECT cv.*, c.name as campaign_name
            FROM campaign_videos cv
            JOIN campaigns c ON cv.campaign_id = c.id
            WHERE cv.id = ?
        """,
            (video_id,),
        )

        video = cursor.fetchone()
        if not video:
            return {"status": "error", "message": f"Video {video_id} not found"}

        if video["status"] == "archived":
            return {
                "status": "error",
                "message": f"Video {video_id} is already archived",
            }

        cursor.execute(
            """
            UPDATE campaign_videos
            SET status = 'archived'
            WHERE id = ?
        """,
            (video_id,),
        )

        # Only an activated video is expected to have open windows; archiving
        # a never-activated or paused video closes nothing, silently.
        _close_attribution_windows(
            cursor,
            video_id,
            datetime.now(),
            expect_open=(video["status"] == "activated"),
        )

        return {
            "status": "success",
            "message": "Video archived" + (f": {reason}" if reason else ""),
            "video": {
                "id": video_id,
                "video_filename": video["video_filename"],
                "campaign_name": video["campaign_name"],
                "previous_status": video["status"],
                "new_status": "archived",
                "reason": reason,
            },
        }


def get_video_status(video_id: int) -> dict:
    """Get the current status and details of a video.

    Args:
        video_id: The video ID to check

    Returns:
        Dictionary with video status and details
    """
    with get_db_cursor() as cursor:
        cursor.execute(
            """
            SELECT cv.*, c.name as campaign_name, p.name as product_name
            FROM campaign_videos cv
            JOIN campaigns c ON cv.campaign_id = c.id
            LEFT JOIN products p ON cv.product_id = p.id
            WHERE cv.id = ?
        """,
            (video_id,),
        )

        video = cursor.fetchone()
        if not video:
            return {"status": "error", "message": f"Video {video_id} not found"}

        # Get metrics count if activated
        metrics_count = 0
        if video["status"] == "activated":
            cursor.execute(
                """
                SELECT COUNT(*) as count FROM video_metrics WHERE video_id = ?
            """,
                (video_id,),
            )
            metrics_count = cursor.fetchone()["count"]

        variation_params = None
        if video["variation_params"]:
            try:
                variation_params = json.loads(video["variation_params"])
            except json.JSONDecodeError:
                pass

        return {
            "status": "success",
            "video": {
                "id": video_id,
                "video_filename": video["video_filename"],
                "campaign_id": video["campaign_id"],
                "campaign_name": video["campaign_name"],
                "product_id": video["product_id"],
                "product_name": video["product_name"],
                "variation_name": video["variation_name"],
                "variation_params": variation_params,
                "thumbnail_path": video["thumbnail_path"],
                "pipeline_type": video["pipeline_type"],
                "duration_seconds": video["duration_seconds"],
                "video_status": video["status"],
                "activated_at": video["activated_at"],
                "activated_by": video["activated_by"],
                "created_at": video["created_at"],
                "metrics_count": metrics_count,
            },
        }


def get_activation_summary(campaign_id: int | None = None) -> dict:
    """Get a summary of video statuses across campaigns.

    Args:
        campaign_id: Optional campaign filter

    Returns:
        Dictionary with status counts
    """
    with get_db_cursor() as cursor:
        if campaign_id:
            cursor.execute(
                """
                SELECT status, COUNT(*) as count
                FROM campaign_videos
                WHERE campaign_id = ?
                GROUP BY status
            """,
                (campaign_id,),
            )
        else:
            cursor.execute("""
                SELECT status, COUNT(*) as count
                FROM campaign_videos
                GROUP BY status
            """)

        rows = cursor.fetchall()

        status_counts = {
            "generating": 0,
            "generated": 0,
            "activated": 0,
            "paused": 0,
            "archived": 0,
        }

        for row in rows:
            status_counts[row["status"]] = row["count"]

        total = sum(status_counts.values())

        return {
            "status": "success",
            "campaign_id": campaign_id,
            "total_videos": total,
            "status_counts": status_counts,
            "pending_review": status_counts["generated"],
            "live": status_counts["activated"],
        }


def generate_additional_metrics(video_id: int, days: int = 7) -> dict:
    """Generate additional metrics for an already-activated video.

    Use this to extend the metrics period for a live video.

    Args:
        video_id: The video ID
        days: Number of additional days to generate

    Returns:
        Dictionary with generation result
    """
    with get_db_cursor() as cursor:
        # Check video exists and is activated
        cursor.execute(
            """
            SELECT status FROM campaign_videos WHERE id = ?
        """,
            (video_id,),
        )

        video = cursor.fetchone()
        if not video:
            return {"status": "error", "message": f"Video {video_id} not found"}

        if video["status"] != "activated":
            return {
                "status": "error",
                "message": f"Video {video_id} is not activated (metrics only for live videos)",
            }

        # Advance the demo universe: move the anchor forward by `days` and
        # fill the new dates for EVERY activated video, so no video lags the
        # anchor (single windowing rule, no per-video drift).
        old_anchor = date.fromisoformat(get_demo_anchor_date(cursor))
        new_anchor = old_anchor + timedelta(days=days)
        set_demo_anchor_date(cursor, new_anchor.isoformat())

        cursor.execute("""
            SELECT id, campaign_id FROM campaign_videos WHERE status = 'activated'
        """)
        by_campaign: dict = {}
        for row in cursor.fetchall():
            by_campaign.setdefault(row["campaign_id"], []).append(row["id"])

        for cid, vids in by_campaign.items():
            _insert_derived_metrics(
                cursor, cid, vids, old_anchor + timedelta(days=1), new_anchor
            )

        # The response contract's count is for the requested video
        cursor.execute(
            "SELECT COUNT(*) AS n FROM video_metrics WHERE video_id = ? AND metric_date > ?",
            (video_id, old_anchor.isoformat()),
        )
        metrics_created = cursor.fetchone()["n"]

        return {
            "status": "success",
            "message": f"Generated {metrics_created} additional metric days",
            "video_id": video_id,
            "start_date": (old_anchor + timedelta(days=1)).isoformat(),
            "days_generated": metrics_created,
        }


# =============================================================================
# Video Review Tools (Table View with Preview Links)
# =============================================================================


def get_video_review_table(
    campaign_id: int | None = None, status: str | None = None, limit: int = 20
) -> dict:
    """Get a formatted review table with video preview links.

    Returns a table view optimized for human review with:
    - Video ID (for activation commands like "activate 1, 4, 7")
    - Status indicator (pending, live, paused, archived)
    - Product and campaign/store info
    - View link (public GCS URL - click to preview in browser)
    - Thumbnail link (if available)
    - Variation name

    Args:
        campaign_id: Optional filter by campaign
        status: Optional filter (generated, activated, paused, archived)
        limit: Maximum videos to return (default 20)

    Returns:
        Dict with:
        - table: Markdown-formatted table string for display
        - videos: List of video details with URLs
        - summary: Status counts and action guidance
    """
    from .. import storage

    with get_db_cursor() as cursor:
        # Build query with filters - include full product and campaign details
        query = """
            SELECT cv.*,
                   c.name as campaign_name, c.store_name, c.city, c.state, c.category as campaign_category,
                   p.name as product_name, p.category as product_category,
                   p.color as product_color, p.style as product_style,
                   p.fabric as product_fabric, p.image_filename as product_image
            FROM campaign_videos cv
            JOIN campaigns c ON cv.campaign_id = c.id
            LEFT JOIN products p ON cv.product_id = p.id
        """
        conditions = []
        params: list[Any] = []

        if campaign_id:
            conditions.append("cv.campaign_id = ?")
            params.append(campaign_id)
        if status:
            conditions.append("cv.status = ?")
            params.append(status)

        if conditions:
            query += " WHERE " + " AND ".join(conditions)

        query += " ORDER BY cv.created_at DESC LIMIT ?"
        params.append(limit)

        cursor.execute(query, params)
        rows = cursor.fetchall()

        # Build table and video list
        videos = []
        status_counts = {
            "generated": 0,
            "activated": 0,
            "paused": 0,
            "archived": 0,
        }

        for row in rows:
            # Public URLs (ws09 policy): only for files that actually exist
            video_url = None
            video_exists_in_storage = False
            if row["video_filename"]:
                video_url = storage.get_video_public_url(
                    row["video_filename"], check_exists=True
                )
                video_exists_in_storage = video_url is not None
            product_image_url = (
                storage.get_product_image_public_url(row["product_image"])
                if row["product_image"]
                else None
            )

            # Map status to display text
            status_display = {
                "generated": "pending",
                "activated": "live",
                "paused": "paused",
                "archived": "archived",
            }.get(row["status"], row["status"])

            # Count by status
            if row["status"] in status_counts:
                status_counts[row["status"]] += 1

            # Parse variation params if available
            variation_params = None
            if row["variation_params"]:
                try:
                    variation_params = json.loads(row["variation_params"])
                except json.JSONDecodeError:
                    pass

            videos.append(
                {
                    "id": row["id"],
                    "status": row["status"],
                    "status_display": status_display,
                    # Product info
                    "product_name": row["product_name"],
                    "product_category": row["product_category"],
                    "product_color": row["product_color"],
                    "product_style": row["product_style"],
                    "product_fabric": row["product_fabric"],
                    "product_image_url": product_image_url,
                    # Campaign/location info
                    "campaign_name": row["campaign_name"],
                    "store_name": row["store_name"],
                    "city": row["city"],
                    "state": row["state"],
                    # Video generation info
                    "variation_name": row["variation_name"],
                    "variation_params": variation_params,
                    "video_url": video_url,
                    "video_exists": video_exists_in_storage,  # True if file exists in GCS
                    "duration_seconds": row["duration_seconds"],
                    "pipeline_type": row["pipeline_type"],
                    "generation_time_seconds": row["generation_time_seconds"],
                    "created_at": row["created_at"],
                }
            )

        # Build card-based format with bullet points for proper ADK web rendering
        table_lines = []

        for v in videos:
            vid_id = v["id"]
            status_icon = (
                "🟡"
                if v["status"] == "generated"
                else "🟢"
                if v["status"] == "activated"
                else "⏸️"
                if v["status"] == "paused"
                else "📦"
            )
            status_text = v["status_display"].upper()

            # Video header
            table_lines.append(
                f"## {status_icon} Video #{vid_id} — {status_text}"
            )
            table_lines.append("")

            # Product section
            table_lines.append("**📦 Product Details**")
            table_lines.append(f"- Name: {v['product_name'] or 'N/A'}")
            table_lines.append(
                f"- Category: {v['product_category'] or 'N/A'} | Color: {v['product_color'] or 'N/A'}"
            )
            table_lines.append(
                f"- Style: {v['product_style'] or 'N/A'} | Fabric: {v['product_fabric'] or 'N/A'}"
            )
            if v["product_image_url"]:
                table_lines.append(
                    f"- [🖼️ View Product Image]({v['product_image_url']})"
                )
            table_lines.append("")

            # Location section
            table_lines.append("**📍 Location**")
            table_lines.append(f"- Store: {v['store_name'] or 'N/A'}")
            table_lines.append(
                f"- Location: {v['city'] or 'N/A'}, {v['state'] or 'N/A'}"
            )
            table_lines.append("")

            # Video generation section
            table_lines.append("**🎬 Video Info**")
            table_lines.append(
                f"- Variation: {v['variation_name'] or 'default'}"
            )
            if v["variation_params"]:
                vp = v["variation_params"]
                table_lines.append(
                    f"- Model: {vp.get('model_ethnicity', 'N/A')} | Setting: {vp.get('setting', 'N/A')} | Mood: {vp.get('mood', 'N/A')}"
                )
            table_lines.append(
                f"- Duration: {v['duration_seconds']}s | Pipeline: {v['pipeline_type'] or 'N/A'}"
            )
            if v["generation_time_seconds"]:
                table_lines.append(
                    f"- Generation time: {v['generation_time_seconds']}s"
                )
            table_lines.append(f"- Created: {v['created_at'] or 'N/A'}")
            table_lines.append("")

            # Watch link - prominent, with existence indicator
            if v["video_url"]:
                if v.get("video_exists", True):
                    table_lines.append(
                        f"**👉 [▶️ WATCH VIDEO]({v['video_url']})**"
                    )
                else:
                    table_lines.append(
                        "**⚠️ Video not yet generated** (demo placeholder - generate a real video to view)"
                    )
                table_lines.append("")

            table_lines.append("---")
            table_lines.append("")

        table = "\n".join(table_lines)

        # Build summary
        pending = status_counts["generated"]
        live = status_counts["activated"]
        total = len(videos)

        summary = f"**Summary:** {pending} pending activation, {live} live, {total} total shown"
        if pending > 0:
            summary += f'\n\nTo activate videos, say: "activate {", ".join(str(v["id"]) for v in videos[:3] if v["status"] == "generated")}" (or any IDs)'

        return {
            "status": "success",
            "table": table,
            "videos": videos,
            "summary": summary,
            "counts": status_counts,
            "filter": {"campaign_id": campaign_id, "status": status},
            "message": "Click View links to preview videos in browser. Use activate_batch([id1, id2, ...]) to activate.",
        }


def get_video_details(video_id: int) -> dict:
    """Get detailed preview information for a single video.

    Returns full information for reviewing a specific video including:
    - Video public URL (viewable link)
    - Thumbnail public URL
    - Full variation parameters (model, setting, mood, etc.)
    - Scene and video prompts used
    - Generation metadata (duration, pipeline type, generation time)
    - Product details (name, category, color, style)
    - Campaign info (store, location)
    - Metrics summary (if activated)

    Args:
        video_id: The video ID to get details for

    Returns:
        Dict with comprehensive video information
    """
    from .. import storage

    with get_db_cursor() as cursor:
        cursor.execute(
            """
            SELECT cv.*,
                   c.name as campaign_name, c.store_name, c.city, c.state,
                   p.name as product_name, p.category as product_category,
                   p.color as product_color, p.style as product_style,
                   p.fabric as product_fabric
            FROM campaign_videos cv
            JOIN campaigns c ON cv.campaign_id = c.id
            LEFT JOIN products p ON cv.product_id = p.id
            WHERE cv.id = ?
        """,
            (video_id,),
        )

        row = cursor.fetchone()
        if not row:
            return {"status": "error", "message": f"Video {video_id} not found"}

        # Public URLs (ws09 policy): only for files that actually exist
        video_url = None
        video_exists_in_storage = False
        if row["video_filename"]:
            video_url = storage.get_video_public_url(
                row["video_filename"], check_exists=True
            )
            video_exists_in_storage = video_url is not None

        thumbnail_url = None
        if row["thumbnail_path"]:
            # Handle both full paths and filenames
            thumb_filename = (
                row["thumbnail_path"].split("/")[-1]
                if "/" in row["thumbnail_path"]
                else row["thumbnail_path"]
            )
            thumbnail_url = storage.get_thumbnail_public_url(thumb_filename)

        # Parse variation parameters
        variation_params = None
        if row["variation_params"]:
            try:
                variation_params = json.loads(row["variation_params"])
            except json.JSONDecodeError:
                pass

        # Get metrics summary if activated
        metrics_summary = None
        if row["status"] == "activated":
            cursor.execute(
                """
                SELECT
                    COUNT(*) as days,
                    SUM(impressions) as total_impressions,
                    AVG(dwell_time_seconds) as avg_dwell,
                    SUM(revenue) as total_revenue
                FROM video_metrics
                WHERE video_id = ?
            """,
                (video_id,),
            )
            m = cursor.fetchone()
            if m and m["days"] > 0:
                metrics_summary = {
                    "days_tracked": m["days"],
                    "total_impressions": int(m["total_impressions"]),
                    "avg_dwell_seconds": round(m["avg_dwell"], 1),
                    "total_revenue": round(m["total_revenue"], 2),
                    "rpi": compute_rpi(
                        m["total_revenue"], m["total_impressions"]
                    ),
                }

        # Build view action based on whether video exists
        if video_exists_in_storage:
            view_action = f"Click video_url to preview: {video_url}"
        else:
            view_action = "Video file not yet generated (demo placeholder). Generate a real video using generate_video_from_product()."

        return {
            "status": "success",
            "video": {
                "id": row["id"],
                "video_filename": row["video_filename"],
                "video_url": video_url if video_exists_in_storage else None,
                "video_exists": video_exists_in_storage,
                "thumbnail_url": thumbnail_url,
                "video_status": row["status"],
                "duration_seconds": row["duration_seconds"],
                "aspect_ratio": row["aspect_ratio"],
                "pipeline_type": row["pipeline_type"],
                "generation_time_seconds": row["generation_time_seconds"],
                "created_at": row["created_at"],
                "activated_at": row["activated_at"],
                "activated_by": row["activated_by"],
            },
            "variation": {
                "name": row["variation_name"],
                "params": variation_params,
            },
            "prompts": {
                "scene_prompt": row["scene_prompt"],
                "video_prompt": row["video_prompt"],
            },
            "product": {
                "id": row["product_id"],
                "name": row["product_name"],
                "category": row["product_category"],
                "color": row["product_color"],
                "style": row["product_style"],
                "fabric": row["product_fabric"],
            },
            "campaign": {
                "id": row["campaign_id"],
                "name": row["campaign_name"],
                "store": row["store_name"],
                "location": f"{row['city']}, {row['state']}",
            },
            "metrics": metrics_summary,
            "actions": {
                "view": view_action,
                "activate": f"activate_video({video_id})"
                if row["status"] == "generated"
                else None,
                "pause": f"pause_video({video_id})"
                if row["status"] == "activated"
                else None,
                "archive": f"archive_video({video_id})"
                if row["status"] != "archived"
                else None,
            },
            "note": None
            if video_exists_in_storage
            else "This is a demo placeholder. The video file does not exist in storage. Use generate_video_from_product() to create a real video.",
        }
