"""Refactor invariant: routing the join through AudienceDataSource must not
change a single derived value. GOLDEN was captured from the pre-seam code
(ws11a Task 5 Step 1) — if this test ever needs updating, the seam changed
demo data, which is a bug by definition in this workstream."""

from datetime import date

from app.demo_data.derive import derive_video_metrics_rows

GOLDEN = [
    {
        "video_id": 1,
        "metric_date": "2026-06-01",
        "impressions": 843,
        "dwell_time_seconds": 9.1,
        "circulation": 3793,
        "revenue": 26.22,
    },
    {
        "video_id": 2,
        "metric_date": "2026-06-01",
        "impressions": 1088,
        "dwell_time_seconds": 4.0,
        "circulation": 5015,
        "revenue": 58.64,
    },
    {
        "video_id": 1,
        "metric_date": "2026-06-02",
        "impressions": 801,
        "dwell_time_seconds": 7.6,
        "circulation": 3867,
        "revenue": 24.91,
    },
    {
        "video_id": 2,
        "metric_date": "2026-06-02",
        "impressions": 1020,
        "dwell_time_seconds": 8.8,
        "circulation": 5014,
        "revenue": 54.98,
    },
    {
        "video_id": 1,
        "metric_date": "2026-06-03",
        "impressions": 828,
        "dwell_time_seconds": 11.6,
        "circulation": 3838,
        "revenue": 25.75,
    },
    {
        "video_id": 2,
        "metric_date": "2026-06-03",
        "impressions": 1093,
        "dwell_time_seconds": 4.3,
        "circulation": 5026,
        "revenue": 58.91,
    },
    {
        "video_id": 1,
        "metric_date": "2026-06-04",
        "impressions": 830,
        "dwell_time_seconds": 8.0,
        "circulation": 3903,
        "revenue": 25.81,
    },
    {
        "video_id": 2,
        "metric_date": "2026-06-04",
        "impressions": 1094,
        "dwell_time_seconds": 9.1,
        "circulation": 5087,
        "revenue": 58.97,
    },
    {
        "video_id": 1,
        "metric_date": "2026-06-05",
        "impressions": 804,
        "dwell_time_seconds": 9.6,
        "circulation": 3858,
        "revenue": 25.0,
    },
    {
        "video_id": 2,
        "metric_date": "2026-06-05",
        "impressions": 1063,
        "dwell_time_seconds": 11.4,
        "circulation": 5192,
        "revenue": 57.3,
    },
    {
        "video_id": 1,
        "metric_date": "2026-06-06",
        "impressions": 1034,
        "dwell_time_seconds": 5.5,
        "circulation": 4798,
        "revenue": 32.16,
    },
    {
        "video_id": 2,
        "metric_date": "2026-06-06",
        "impressions": 1325,
        "dwell_time_seconds": 11.7,
        "circulation": 6224,
        "revenue": 71.42,
    },
    {
        "video_id": 1,
        "metric_date": "2026-06-07",
        "impressions": 1025,
        "dwell_time_seconds": 4.1,
        "circulation": 4872,
        "revenue": 31.88,
    },
    {
        "video_id": 2,
        "metric_date": "2026-06-07",
        "impressions": 1327,
        "dwell_time_seconds": 7.9,
        "circulation": 6136,
        "revenue": 71.53,
    },
]


def test_derive_output_is_byte_identical_to_pre_seam_capture():
    rows = derive_video_metrics_rows(
        101, [1, 2], date(2026, 6, 1), date(2026, 6, 7)
    )
    assert rows == GOLDEN
