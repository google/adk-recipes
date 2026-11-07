"""Shared attribution-window DB load (ws10 carried items 1+2).

Lives in app/demo_data so both app/tools (review_tools) and app/database
(mock_data) can share one implementation without breaking the layering
rule that app.database never imports app.tools.

Rows are indexed positionally so the helper serves both sqlite3.Row
cursors (review_tools, via app/database/db.py's row_factory) and plain
tuple cursors (mock_data's bulk path).
"""

from datetime import datetime


def load_attribution_windows(cursor, video_ids) -> list[dict]:
    """Read the video set's attribution windows in join-ready dict shape.

    An empty video_ids returns [] without touching the DB — building
    ``IN ()`` is invalid SQLite (ws10 carried item 1).
    """
    video_ids = list(video_ids)
    if not video_ids:
        return []
    placeholders = ",".join("?" for _ in video_ids)
    cursor.execute(
        f"SELECT video_id, screen_id, active_from, active_to FROM video_attribution "  # noqa: S608
        f"WHERE video_id IN ({placeholders})",
        video_ids,
    )
    return [
        {
            "video_id": row[0],
            "screen_id": row[1],
            "active_from": datetime.fromisoformat(row[2]),
            "active_to": datetime.fromisoformat(row[3]) if row[3] else None,
        }
        for row in cursor.fetchall()
    ]
