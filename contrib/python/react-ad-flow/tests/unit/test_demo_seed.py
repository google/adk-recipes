"""Tests for the deterministic demo-data seed generator.

Ported from the donor repo's test_seed.py (ad-campaign-agent @ b6e3302),
adapted: no pandas (frames are lists of dicts), screen vocabulary,
ad_campaign_id columns, HHMM bin names, plus a cross-process determinism
check the phase doc's validation requires.
"""

import subprocess
import sys
from datetime import date

from app.demo_data.seed import (
    DWELL_BIN_COLUMNS,
    SeedConfig,
    generate_frames,
)


def _config() -> SeedConfig:
    return SeedConfig(
        screen_ids=[1, 2],
        campaigns=[
            (42, "Spring Promo", date(2026, 3, 1), date(2026, 3, 7), 1.0),
            (43, "Mother's Day", date(2026, 5, 1), date(2026, 5, 7), 1.4),
        ],
        date_from=date(2026, 3, 1),
        date_to=date(2026, 5, 10),
    )


class TestSeedShape:
    def test_returns_all_five_frames(self):
        frames = generate_frames(_config())
        assert set(frames.keys()) == {
            "screen_visits",
            "screen_dwell",
            "campaign_uv_daily",
            "campaign_flow_transition",
            "video_attribution",
        }

    def test_screen_visits_has_corrected_columns(self):
        frames = generate_frames(_config())
        row = frames["screen_visits"][0]
        assert "incoming_inner_count" in row
        assert "outgoing_outer_count" in row
        assert "ad_campaign_id" in row
        assert "minimum_visitors_inner" in row
        assert "maximum_visitors_outer" in row
        # The donor names this port corrects must be gone
        assert "campaign_id" not in row
        assert "store_id" not in row
        assert "min_visitors_inner" not in row

    def test_dwell_bins_are_hhmm_encoded(self):
        assert len(DWELL_BIN_COLUMNS) == 106
        # Sub-hour names are identical in both encodings
        assert DWELL_BIN_COLUMNS[0] == "distribution_bin_0000_to_0001"
        # The boundary bin: 58 min -> 60 min is _0058_to_0100, never _0058_to_0060
        assert DWELL_BIN_COLUMNS[44] == "distribution_bin_0058_to_0100"
        assert "distribution_bin_0058_to_0060" not in DWELL_BIN_COLUMNS
        # First bin above the hour: HHMM, not minutes
        assert DWELL_BIN_COLUMNS[45] == "distribution_bin_0100_to_0105"
        assert "distribution_bin_0060_to_0065" not in DWELL_BIN_COLUMNS
        # Last regular bin and the open-ended bin
        assert DWELL_BIN_COLUMNS[104] == "distribution_bin_2300_to_2400"
        assert DWELL_BIN_COLUMNS[105] == "distribution_bin_2400_to_beyond"

    def test_screen_dwell_has_106_bins_summing_to_one(self):
        frames = generate_frames(_config())
        d = frames["screen_dwell"]
        assert d, "dwell frame must not be empty"
        row = d[0]
        bins = [k for k in row if k.startswith("distribution_bin_")]
        assert len(bins) == 106
        for r in d[:50]:
            assert abs(sum(r[c] for c in DWELL_BIN_COLUMNS) - 1.0) < 1e-6

    def test_cuv_freq_columns_are_floats(self):
        frames = generate_frames(_config())
        row = frames["campaign_uv_daily"][0]
        for i in range(1, 11):
            assert isinstance(row[f"cuv_freq_{i}"], float)


class TestDeterminism:
    def test_same_config_produces_identical_data(self):
        f1 = generate_frames(_config())
        f2 = generate_frames(_config())
        assert f1 == f2

    def test_cross_process_determinism(self):
        """Same key in two separate processes -> byte-identical output
        (proves sha256 seeding survived the port; no PYTHONHASHSEED dependence)."""
        script = (
            "import hashlib, json;"
            "from datetime import date;"
            "from app.demo_data.seed import SeedConfig, generate_frames;"
            "cfg = SeedConfig(screen_ids=[1], campaigns=[(7, 'x', date(2026, 5, 1),"
            " date(2026, 5, 3), 1.1)], date_from=date(2026, 5, 1), date_to=date(2026, 5, 3));"
            "frames = generate_frames(cfg);"
            "print(hashlib.sha256(repr(frames).encode()).hexdigest())"
        )
        runs = [
            subprocess.run(
                [sys.executable, "-c", script],
                capture_output=True,
                text=True,
                check=True,
            ).stdout.strip()
            for _ in range(2)
        ]
        assert runs[0] == runs[1]
        assert len(runs[0]) == 64

    def test_different_keys_differ(self):
        cfg_a = SeedConfig(
            screen_ids=[1],
            campaigns=[(42, "x", date(2026, 5, 1), date(2026, 5, 3), 1.0)],
            date_from=date(2026, 5, 1),
            date_to=date(2026, 5, 3),
        )
        cfg_b = SeedConfig(
            screen_ids=[1],
            campaigns=[(43, "x", date(2026, 5, 1), date(2026, 5, 3), 1.0)],
            date_from=date(2026, 5, 1),
            date_to=date(2026, 5, 3),
        )
        va = generate_frames(cfg_a)["screen_visits"]
        vb = generate_frames(cfg_b)["screen_visits"]
        assert [r["incoming_inner_count"] for r in va] != [
            r["incoming_inner_count"] for r in vb
        ]

    def test_campaign_uplift_increases_impressions(self):
        def cfg(uplift):
            return SeedConfig(
                screen_ids=[1],
                campaigns=[
                    (42, "x", date(2026, 5, 1), date(2026, 5, 3), uplift)
                ],
                date_from=date(2026, 5, 1),
                date_to=date(2026, 5, 3),
            )

        low = generate_frames(cfg(0.7))["screen_visits"]
        high = generate_frames(cfg(1.4))["screen_visits"]
        assert sum(r["incoming_inner_count"] for r in high) > sum(
            r["incoming_inner_count"] for r in low
        )


class TestRealism:
    def test_weekend_lift_is_visible(self):
        cfg = SeedConfig(
            screen_ids=[1],
            campaigns=[(42, "x", date(2026, 5, 1), date(2026, 5, 14), 1.0)],
            date_from=date(2026, 5, 1),
            date_to=date(2026, 5, 14),
        )
        visits = generate_frames(cfg)["screen_visits"]
        weekend = [
            r["incoming_inner_count"]
            for r in visits
            if r["timestamp"].weekday() >= 5
        ]
        weekday = [
            r["incoming_inner_count"]
            for r in visits
            if r["timestamp"].weekday() < 5
        ]
        assert sum(weekend) / len(weekend) > sum(weekday) / len(weekday)
