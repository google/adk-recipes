"""Tests for the Phase 10 client-contract DTOs (Email 5 shapes)."""

from datetime import date, datetime, timedelta
from typing import Any, ClassVar

import pytest
from pydantic import ValidationError

from app.demo_data.seed import SeedConfig, generate_frames
from app.models.attribution import AdPlayRecord, BlueZooVisitInterval


class TestAdPlayRecord:
    def test_minimal_construction_and_defaults(self):
        rec = AdPlayRecord(
            screen_id=900101,
            ad_campaign_id=9001,
            video_id=101,
            start=datetime(2026, 6, 1, 9, 0),
            end=datetime(2026, 6, 1, 9, 15),
        )
        assert rec.ad_name is None
        assert rec.product_ids == []
        assert rec.end - rec.start == timedelta(minutes=15)


class TestBlueZooVisitInterval:
    def test_validates_seed_screen_visits_rows_verbatim(self):
        """The DTO must accept seed.py's screen_visits rows as-is — that is
        the whole point of the seam (Phase 11 swaps the producer, not the
        shape)."""
        cfg = SeedConfig(
            screen_ids=[900101, 900102],
            campaigns=[
                (9001, "campaign-9001", date(2026, 6, 1), date(2026, 6, 2), 1.0)
            ],
            date_from=date(2026, 6, 1),
            date_to=date(2026, 6, 2),
        )
        rows = generate_frames(cfg)["screen_visits"]
        assert len(rows) == 2 * 2 * 48  # 2 days x 2 screens x 48 slots
        for row in rows:
            interval = BlueZooVisitInterval.model_validate(row)
            assert interval.screen_id in (900101, 900102)
            assert interval.valid is True
            assert interval.ad_campaign_id == 9001

    def test_rejects_fields_outside_sensor_visits_scope(self):
        """Replan amendment: the interval is scoped to sensor_visits fields
        ONLY — extra keys must fail, not silently pass through."""
        cfg = SeedConfig(
            screen_ids=[900101],
            campaigns=[(9001, "c", date(2026, 6, 1), date(2026, 6, 1), 1.0)],
            date_from=date(2026, 6, 1),
            date_to=date(2026, 6, 1),
        )
        row = dict(generate_frames(cfg)["screen_visits"][0])
        row["dwell_histogram"] = [1, 2, 3]
        with pytest.raises(ValidationError):
            BlueZooVisitInterval.model_validate(row)


class TestOccupancyFieldsOptional:
    """Phase 11b: occupancy (sensor_visitors) fields are demo-populated only —
    the live conformer queries sensor_visits alone and leaves them None."""

    FLOW_ONLY: ClassVar[dict[str, Any]] = {
        "timestamp": datetime(2026, 7, 20, 14, 45),
        "screen_id": 101,
        "incoming_inner_count": 3.2,
        "outgoing_inner_count": 2.9,
        "incoming_outer_count": 8.1,
        "outgoing_outer_count": 7.7,
        "valid": True,
    }

    def test_constructible_without_occupancy_fields(self):
        iv = BlueZooVisitInterval(**self.FLOW_ONLY)
        assert iv.minimum_visitors_inner is None
        assert iv.maximum_visitors_inner is None
        assert iv.average_visitors_inner is None
        assert iv.minimum_visitors_outer is None
        assert iv.maximum_visitors_outer is None
        assert iv.average_visitors_outer is None

    def test_occupancy_fields_still_accept_floats(self):
        iv = BlueZooVisitInterval(**self.FLOW_ONLY, average_visitors_inner=4.5)
        assert iv.average_visitors_inner == 4.5

    def test_extra_forbid_still_enforced(self):
        with pytest.raises(ValidationError):
            BlueZooVisitInterval(**self.FLOW_ONLY, not_a_field=1)
