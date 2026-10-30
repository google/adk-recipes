"""Tests for the Phase 10 ad-play attribution join (replaces test_demo_derive.py)."""

from datetime import date, datetime, timedelta

from app.demo_data.attribution import (
    derive_rows_from_windows,
    expand_ad_plays,
    screens_for_campaign,
    slot_budget,
    video_rpi,
)
from app.demo_data.constants import DEMO_RPI, DEMO_WINDOW_DAYS
from app.demo_data.derive import derive_video_metrics_rows
from app.tools.metrics_shared import compute_rpi

CID = 9001
D_FROM = date(2026, 6, 1)
D_TO = date(2026, 6, 7)


def _open_windows(video_ids, screens, active_from_date=D_FROM):
    return [
        {
            "video_id": vid,
            "screen_id": sid,
            "active_from": datetime.combine(
                active_from_date, datetime.min.time()
            ),
            "active_to": None,
        }
        for vid in video_ids
        for sid in screens
    ]


class TestScreensRoster:
    def test_two_to_three_screens_decoupled_from_campaign_id(self):
        for cid in (1, 2, 3, 9001, 424242):
            screens = screens_for_campaign(cid)
            assert 2 <= len(screens) <= 3
            assert (
                cid not in screens
            )  # the 1:1 campaign-as-screen proxy is dead
            assert len(set(screens)) == len(screens)
            assert screens == screens_for_campaign(cid)  # deterministic

    def test_rosters_vary_in_size_across_campaigns(self):
        sizes = {len(screens_for_campaign(cid)) for cid in range(1, 40)}
        assert sizes == {2, 3}


class TestPlaySchedule:
    def test_slot_budget_band_and_determinism(self):
        for vid in (101, 102, 999):
            k = slot_budget(CID, vid, screens_for_campaign(CID)[0])
            assert 10 <= k <= 22
            assert k == slot_budget(CID, vid, screens_for_campaign(CID)[0])

    def test_plays_are_15min_aligned_within_open_hours(self):
        screens = screens_for_campaign(CID)
        plays = expand_ad_plays(
            CID, _open_windows([101], screens), D_FROM, D_FROM
        )
        assert plays, "an open window must produce plays"
        for p in plays:
            assert p.ad_campaign_id == CID
            assert p.video_id == 101
            assert p.screen_id in screens
            assert p.end - p.start == timedelta(minutes=15)
            assert p.start.minute % 15 == 0
            assert 9 <= p.start.hour < 21

    def test_daily_play_count_matches_slot_budget(self):
        screens = screens_for_campaign(CID)
        plays = expand_ad_plays(
            CID, _open_windows([101], screens), D_FROM, D_FROM
        )
        per_screen = {}
        for p in plays:
            per_screen[p.screen_id] = per_screen.get(p.screen_id, 0) + 1
        for sid in screens:
            assert per_screen[sid] == slot_budget(CID, 101, sid)

    def test_no_duplicate_slots_per_video_screen_day(self):
        screens = screens_for_campaign(CID)
        plays = expand_ad_plays(
            CID, _open_windows([101], screens), D_FROM, D_TO
        )
        keys = [(p.screen_id, p.start) for p in plays]
        assert len(keys) == len(set(keys))

    def test_window_clamps_days(self):
        """Plays exist only on days the window covers."""
        screens = screens_for_campaign(CID)
        w = [
            {
                "video_id": 101,
                "screen_id": screens[0],
                "active_from": datetime.combine(
                    D_FROM + timedelta(days=2), datetime.min.time()
                ),
                "active_to": datetime.combine(
                    D_FROM + timedelta(days=4), datetime.min.time()
                ),
            }
        ]
        plays = expand_ad_plays(CID, w, D_FROM, D_TO)
        days = {p.start.date() for p in plays}
        assert days == {
            D_FROM + timedelta(days=2),
            D_FROM + timedelta(days=3),
            D_FROM + timedelta(days=4),
        }

    def test_schedule_is_absolute_per_video(self):
        """Adding a second video's windows never changes the first video's plays."""
        screens = screens_for_campaign(CID)
        solo = expand_ad_plays(CID, _open_windows([101], screens), D_FROM, D_TO)
        both = [
            p
            for p in expand_ad_plays(
                CID, _open_windows([101, 102], screens), D_FROM, D_TO
            )
            if p.video_id == 101
        ]
        assert solo == both


class TestConstants:
    def test_canonical_values(self):
        assert DEMO_RPI == 0.05
        assert DEMO_WINDOW_DAYS == 30


class TestJoinDerivation:
    def test_one_row_per_video_per_day(self):
        rows = derive_video_metrics_rows(CID, [101, 102], D_FROM, D_TO)
        assert len(rows) == 2 * 7
        dates = {r["metric_date"] for r in rows}
        assert min(dates) == D_FROM.isoformat()
        assert max(dates) == D_TO.isoformat()

    def test_impressions_are_played_slot_inner_visits_only(self):
        """impressions = sum of incoming_inner_count over the video's played
        slots — computed independently here from the schedule + frames. An
        inner+outer computation (the donor's bug) must NOT match."""
        from app.demo_data.attribution import _campaign_seed_config
        from app.demo_data.seed import generate_frames

        screens = screens_for_campaign(CID)
        rows = derive_video_metrics_rows(CID, [101], D_FROM, D_FROM)
        frames = generate_frames(_campaign_seed_config(CID, D_FROM, D_FROM))
        ix = {
            (v["screen_id"], v["timestamp"]): v for v in frames["screen_visits"]
        }
        plays = expand_ad_plays(
            CID, _open_windows([101], screens), D_FROM, D_FROM
        )
        inner = sum(
            ix[(p.screen_id, p.start)]["incoming_inner_count"] for p in plays
        )
        both = sum(
            ix[(p.screen_id, p.start)]["incoming_inner_count"]
            + ix[(p.screen_id, p.start)]["incoming_outer_count"]
            for p in plays
        )
        assert rows[0]["impressions"] == round(inner)
        assert rows[0]["impressions"] != round(both)

    def test_revenue_uses_per_creative_rpi(self):
        rows = derive_video_metrics_rows(CID, [101, 102], D_FROM, D_TO)
        for r in rows:
            assert r["revenue"] == round(
                r["impressions"] * video_rpi(CID, r["video_id"]), 2
            )

    def test_video_rpi_band_determinism_and_divergence(self):
        for vid in (101, 102, 999):
            factor = video_rpi(CID, vid)
            assert 0.03 <= factor <= 0.07
            assert factor == video_rpi(CID, vid)
        assert video_rpi(CID, 101) != video_rpi(CID, 102)

    def test_window_rpi_matches_creative_constant(self):
        rows = derive_video_metrics_rows(CID, [101], D_FROM, D_TO)
        total_rev = sum(r["revenue"] for r in rows)
        total_imp = sum(r["impressions"] for r in rows)
        assert (
            abs(compute_rpi(total_rev, total_imp) - video_rpi(CID, 101)) < 0.001
        )

    def test_per_video_schedules_differ(self):
        rows = derive_video_metrics_rows(CID, [101, 102], D_FROM, D_FROM)
        by_video = {r["video_id"]: r["impressions"] for r in rows}
        assert by_video[101] != by_video[102]

    def test_later_activation_never_changes_existing_rows(self):
        solo = derive_video_metrics_rows(CID, [101], D_FROM, D_TO)
        both = [
            r
            for r in derive_video_metrics_rows(CID, [101, 102], D_FROM, D_TO)
            if r["video_id"] == 101
        ]
        assert solo == both

    def test_deterministic_across_calls(self):
        r1 = derive_video_metrics_rows(CID, [101, 102], D_FROM, D_TO)
        r2 = derive_video_metrics_rows(CID, [101, 102], D_FROM, D_TO)
        assert r1 == r2

    def test_windows_are_load_bearing(self):
        """A window covering only part of the range yields rows ONLY for the
        covered days — metrics accrue where the video was actually live."""
        screens = screens_for_campaign(CID)
        w = [
            {
                "video_id": 101,
                "screen_id": sid,
                "active_from": datetime.combine(D_FROM, datetime.min.time()),
                "active_to": datetime.combine(
                    D_FROM + timedelta(days=2), datetime.min.time()
                ),
            }
            for sid in screens
        ]
        rows = derive_rows_from_windows(CID, [101], w, D_FROM, D_TO)
        days = {r["metric_date"] for r in rows}
        assert days == {
            (D_FROM + timedelta(days=i)).isoformat() for i in range(3)
        }

    def test_more_screens_more_impressions(self):
        screens = screens_for_campaign(CID)
        one = derive_rows_from_windows(
            CID, [101], _open_windows([101], screens[:1]), D_FROM, D_FROM
        )
        all_screens = derive_rows_from_windows(
            CID, [101], _open_windows([101], screens), D_FROM, D_FROM
        )
        assert all_screens[0]["impressions"] > one[0]["impressions"]

    def test_dwell_is_synthetic_seconds_scale(self):
        rows = derive_video_metrics_rows(CID, [101], D_FROM, D_TO)
        for r in rows:
            assert 4.0 <= r["dwell_time_seconds"] <= 12.0

    def test_plausible_magnitudes(self):
        """Charts stay sane: per-video daily impressions in the
        hundreds-to-low-thousands, circulation positive."""
        rows = derive_video_metrics_rows(CID, [101], D_FROM, D_TO)
        for r in rows:
            assert 200 <= r["impressions"] <= 8000
            assert r["circulation"] > 0

    def test_overlapping_windows_do_not_double_count(self):
        """Reactivation after pause opens a fresh window per screen without
        deleting the closed one it replaces, so a video can have a closed
        window and an open window whose date coverage overlaps. The join
        must dedup that overlap by (video_id, screen_id, start) rather than
        summing both windows' plays for the same slot."""
        screens = screens_for_campaign(CID)
        open_windows = _open_windows([101], screens)
        closed_windows = [
            {
                "video_id": 101,
                "screen_id": sid,
                "active_from": datetime.combine(D_FROM, datetime.min.time()),
                "active_to": datetime.combine(
                    D_FROM + timedelta(days=2), datetime.min.time()
                ),
            }
            for sid in screens
        ]
        overlapping = closed_windows + open_windows
        rows_overlap = derive_rows_from_windows(
            CID, [101], overlapping, D_FROM, D_TO
        )
        rows_single = derive_rows_from_windows(
            CID, [101], open_windows, D_FROM, D_TO
        )
        assert rows_overlap == rows_single
