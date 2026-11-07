"""AudienceDataSource ABC: structure + reusable conformance battery (Phase 11a).

The battery (AudienceDataSourceContract) is deliberately subclass-pluggable:
Phase 11b's cached/live conformers add their own TestXxxConformance class
with a make_source() override and inherit every check for free.
"""

import inspect
import itertools
from datetime import date, timedelta
from typing import ClassVar

import pytest

from app.audience.datasource import AudienceDataSource
from app.models.attribution import BlueZooVisitInterval

D_FROM = date(2026, 6, 1)
D_TO = date(2026, 6, 3)


class TestAbstractInterface:
    def test_cannot_instantiate_directly(self):
        with pytest.raises(TypeError):
            AudienceDataSource()

    def test_get_visit_intervals_is_abstract_and_keyword_only(self):
        assert "get_visit_intervals" in AudienceDataSource.__abstractmethods__
        sig = inspect.signature(AudienceDataSource.get_visit_intervals)
        params = list(sig.parameters.values())[1:]  # drop self
        assert [p.name for p in params] == [
            "screen_ids",
            "date_from",
            "date_to",
        ]
        assert all(p.kind is inspect.Parameter.KEYWORD_ONLY for p in params)


class AudienceDataSourceContract:
    """Conformance battery. Subclasses provide make_source() and screen ids."""

    # 2-3 screens of one demo campaign (ws10 convention: cid*100+k).
    SCREEN_IDS: ClassVar[list[int]] = []

    def make_source(self) -> AudienceDataSource:
        raise NotImplementedError

    def test_returns_dto_instances(self):
        intervals = self.make_source().get_visit_intervals(
            screen_ids=self.SCREEN_IDS, date_from=D_FROM, date_to=D_TO
        )
        assert intervals, (
            "expected at least one interval for a live demo screen"
        )
        assert all(isinstance(iv, BlueZooVisitInterval) for iv in intervals)

    def test_respects_screen_filter(self):
        one = self.SCREEN_IDS[:1]
        intervals = self.make_source().get_visit_intervals(
            screen_ids=one, date_from=D_FROM, date_to=D_TO
        )
        assert intervals and {iv.screen_id for iv in intervals} == set(one)

    def test_respects_date_window(self):
        intervals = self.make_source().get_visit_intervals(
            screen_ids=self.SCREEN_IDS, date_from=D_FROM, date_to=D_FROM
        )
        assert intervals
        assert {iv.timestamp.date() for iv in intervals} == {D_FROM}

    def test_deterministic_across_calls_and_instances(self):
        a = self.make_source().get_visit_intervals(
            screen_ids=self.SCREEN_IDS, date_from=D_FROM, date_to=D_TO
        )
        b = self.make_source().get_visit_intervals(
            screen_ids=self.SCREEN_IDS, date_from=D_FROM, date_to=D_TO
        )
        assert a == b

    def test_slot_grain_is_15_minutes(self):
        intervals = self.make_source().get_visit_intervals(
            screen_ids=self.SCREEN_IDS[:1], date_from=D_FROM, date_to=D_FROM
        )
        stamps = sorted(iv.timestamp for iv in intervals)
        assert (
            len(stamps) == 48
        )  # 09:00-21:00 at 15-min grain (seed.py contract)
        deltas = {b - a for a, b in itertools.pairwise(stamps)}
        assert deltas == {timedelta(minutes=15)}

    def test_unknown_screen_returns_empty_not_error(self):
        # A sensor the source doesn't know = no data, like a real sensor API.
        intervals = self.make_source().get_visit_intervals(
            screen_ids=[999_999_99], date_from=D_FROM, date_to=D_TO
        )
        assert intervals == []


class TestSyntheticConformance(AudienceDataSourceContract):
    SCREEN_IDS_CAMPAIGN = 101

    @property
    def SCREEN_IDS(self):
        from app.demo_data.attribution import screens_for_campaign

        return screens_for_campaign(self.SCREEN_IDS_CAMPAIGN)

    def make_source(self):
        from app.audience.synthetic import SyntheticAudienceDataSource

        return SyntheticAudienceDataSource()


class TestLiveStubConformance(AudienceDataSourceContract):
    """The live conformer, transport stubbed (fast tier stays network-free),
    run through the same battery the synthetic source passes."""

    SCREEN_IDS: ClassVar[list[int]] = [101, 102]

    @pytest.fixture(autouse=True)
    def _live_env(self, monkeypatch):
        monkeypatch.setenv("BLUEZOO_BASE_URL", "https://stub.invalid/v2/dwh")
        monkeypatch.setenv("BLUEZOO_ACCESS_KEY", "stub-key")
        monkeypatch.setenv("BLUEZOO_SENSOR_MAP", "101:87,102:433")

    def make_source(self):
        from tests.unit.test_live_bluezoo_datasource import _StubbedLive

        return _StubbedLive()

    def test_slot_grain_is_15_minutes(self):
        # Override: _StubbedLive's fixture is a deliberately small 2-hour
        # (09:00-11:00) fake day, unlike the synthetic source's full
        # 09:00-21:00 demo day — same 15-min grain, fewer slots.
        intervals = self.make_source().get_visit_intervals(
            screen_ids=self.SCREEN_IDS[:1], date_from=D_FROM, date_to=D_FROM
        )
        stamps = sorted(iv.timestamp for iv in intervals)
        assert len(stamps) == 8
        deltas = {b - a for a, b in itertools.pairwise(stamps)}
        assert deltas == {timedelta(minutes=15)}


class TestSyntheticEquivalence:
    """The synthetic source is seed.py behind the seam — same rows exactly."""

    def test_intervals_equal_screen_visits_frame(self):
        from app.audience.synthetic import SyntheticAudienceDataSource
        from app.demo_data.attribution import (
            campaign_seed_config,
            screens_for_campaign,
        )
        from app.demo_data.seed import generate_frames

        cid = 101
        screens = screens_for_campaign(cid)
        frames = generate_frames(campaign_seed_config(cid, D_FROM, D_TO))
        expected = sorted(
            (BlueZooVisitInterval(**row) for row in frames["screen_visits"]),
            key=lambda iv: (iv.screen_id, iv.timestamp),
        )
        got = SyntheticAudienceDataSource().get_visit_intervals(
            screen_ids=screens, date_from=D_FROM, date_to=D_TO
        )
        assert got == expected

    def test_subset_request_does_not_change_values(self):
        # Frames must be generated with the FULL campaign screen set, then
        # filtered — generating from a subset config could reseed values.
        from app.audience.synthetic import SyntheticAudienceDataSource
        from app.demo_data.attribution import screens_for_campaign

        screens = screens_for_campaign(101)
        full = SyntheticAudienceDataSource().get_visit_intervals(
            screen_ids=screens, date_from=D_FROM, date_to=D_TO
        )
        subset = SyntheticAudienceDataSource().get_visit_intervals(
            screen_ids=screens[:1], date_from=D_FROM, date_to=D_TO
        )
        assert subset == [iv for iv in full if iv.screen_id == screens[0]]
