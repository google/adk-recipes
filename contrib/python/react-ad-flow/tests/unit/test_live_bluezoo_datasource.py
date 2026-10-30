"""LiveBlueZooAudienceDataSource unit tests (Phase 11b) — NO network.

Everything here runs against pure helpers or a stubbed `_call`; the fast
tier must never touch BlueZoo (owner directive: `make test` stays
network-free — the live tier in tests/live/ carries the real reads).
"""

import json
import logging
import re
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

from app.audience.live_bluezoo import (
    _COLUMNS,
    BlueZooConfigError,
    BlueZooError,
    BlueZooQuotaExceeded,
    LiveBlueZooAudienceDataSource,
    _build_sql,
    _classify_http_error,
    _coerce_valid,
    _guard_sql,
    _parse_sensor_map,
    _parse_utc_naive,
)
from app.config import BlueZooValidPolicy

D_FROM = date(2026, 7, 20)
D_TO = date(2026, 7, 21)


class TestParseSensorMap:
    def test_parses_pairs(self):
        assert _parse_sensor_map("101:87,102:433") == {101: 87, 102: 433}

    def test_tolerates_whitespace_and_trailing_comma(self):
        assert _parse_sensor_map(" 101:87 , 102:433 ,") == {101: 87, 102: 433}

    def test_empty_fails_closed(self):
        with pytest.raises(BlueZooConfigError, match="BLUEZOO_SENSOR_MAP"):
            _parse_sensor_map("")

    def test_malformed_entry_fails_closed(self):
        for bad in ("101", "101:", ":87", "101:eightyseven", "101=87"):
            with pytest.raises(BlueZooConfigError, match="screen_id:sensor_id"):
                _parse_sensor_map(bad)

    def test_duplicate_screen_rejected(self):
        with pytest.raises(BlueZooConfigError, match="twice"):
            _parse_sensor_map("101:87,101:433")

    def test_duplicate_sensor_rejected(self):
        with pytest.raises(BlueZooConfigError, match="one-to-one"):
            _parse_sensor_map("101:87,102:87")


class TestBuildSql:
    def test_names_exactly_the_seven_columns_never_star(self):
        sql = _build_sql([87, 433], D_FROM, D_TO, BlueZooValidPolicy.VALID_ONLY)
        assert "*" not in sql
        assert ", ".join(_COLUMNS) in sql
        assert _COLUMNS == (
            "timestamp",
            "sensor_id",
            "incoming_inner_count",
            "outgoing_inner_count",
            "incoming_outer_count",
            "outgoing_outer_count",
            "valid",
        )

    def test_half_open_utc_day_window(self):
        sql = _build_sql([87], D_FROM, D_TO, BlueZooValidPolicy.VALID_ONLY)
        assert "timestamp >= '2026-07-20'" in sql
        assert (
            "timestamp < '2026-07-22'" in sql
        )  # date_to inclusive -> +1 day exclusive

    def test_valid_only_appends_rule_r_filter(self):
        sql = _build_sql([87], D_FROM, D_TO, BlueZooValidPolicy.VALID_ONLY)
        assert sql.endswith(" and valid")

    def test_include_all_has_no_valid_filter(self):
        sql = _build_sql([87], D_FROM, D_TO, BlueZooValidPolicy.INCLUDE_ALL)
        assert " and valid" not in sql

    def test_sensor_ids_sorted_into_in_clause(self):
        sql = _build_sql(
            [433, 87], D_FROM, D_TO, BlueZooValidPolicy.INCLUDE_ALL
        )
        assert "sensor_id in (87, 433)" in sql

    def test_passes_its_own_guard(self):
        _guard_sql(
            _build_sql([87], D_FROM, D_TO, BlueZooValidPolicy.VALID_ONLY)
        )


class TestGuardSql:
    def test_rejects_non_select(self):
        with pytest.raises(BlueZooError, match="SELECT"):
            _guard_sql(
                "delete from sensor_visits where timestamp > '2026-01-01'"
            )

    def test_rejects_missing_time_constraint(self):
        with pytest.raises(BlueZooError, match="constrain"):
            _guard_sql("select sensor_id from sensor_visits")


class TestParseUtcNaive:
    def test_z_suffix_becomes_naive_utc(self):
        assert _parse_utc_naive("2026-07-20T14:45:00Z") == datetime(
            2026, 7, 20, 14, 45
        )

    def test_offset_is_converted_then_stripped(self):
        assert _parse_utc_naive("2026-07-20T10:45:00-04:00") == datetime(
            2026, 7, 20, 14, 45
        )

    def test_naive_passes_through(self):
        assert _parse_utc_naive("2026-07-20 14:45:00") == datetime(
            2026, 7, 20, 14, 45
        )


class TestCoerceValid:
    def test_bools_pass_through(self):
        assert _coerce_valid(True) is True
        assert _coerce_valid(False) is False

    def test_strings_parse_case_insensitively(self):
        assert _coerce_valid("true") is True
        assert _coerce_valid("True") is True
        assert (
            _coerce_valid("false") is False
        )  # bool("false") would be True — the bug this exists for


@pytest.fixture
def live_env(monkeypatch):
    monkeypatch.setenv("BLUEZOO_BASE_URL", "https://stub.invalid/v2/dwh")
    monkeypatch.setenv("BLUEZOO_ACCESS_KEY", "stub-key")
    monkeypatch.setenv("BLUEZOO_SENSOR_MAP", "101:87,102:433")
    monkeypatch.setattr("app.audience.live_bluezoo._RETRY_BACKOFF_SECONDS", 0)


class TestConstructorFailsClosed:
    def test_missing_base_url_and_key_named_exactly(self, monkeypatch):
        for var in (
            "BLUEZOO_BASE_URL",
            "BLUEZOO_ACCESS_KEY",
            "BLUEZOO_SENSOR_MAP",
        ):
            monkeypatch.delenv(var, raising=False)
        with pytest.raises(BlueZooConfigError) as excinfo:
            LiveBlueZooAudienceDataSource()
        assert "BLUEZOO_BASE_URL" in str(excinfo.value)
        assert "BLUEZOO_ACCESS_KEY" in str(excinfo.value)

    def test_missing_map_fails_closed(self, monkeypatch, live_env):
        monkeypatch.delenv("BLUEZOO_SENSOR_MAP")
        with pytest.raises(BlueZooConfigError, match="BLUEZOO_SENSOR_MAP"):
            LiveBlueZooAudienceDataSource()

    def test_base_url_has_no_default(self, monkeypatch, live_env):
        monkeypatch.delenv("BLUEZOO_BASE_URL")
        with pytest.raises(BlueZooConfigError, match="BLUEZOO_BASE_URL"):
            LiveBlueZooAudienceDataSource()

    def test_full_env_constructs(self, live_env):
        source = LiveBlueZooAudienceDataSource()
        assert isinstance(source, LiveBlueZooAudienceDataSource)


class TestClassifyHttpError:
    def test_quota_body_maps_to_quota_exceeded(self):
        exc = _classify_http_error(
            400, "fair use limit: data scanned this month ..."
        )
        assert isinstance(exc, BlueZooQuotaExceeded)
        assert "raise" in str(exc)  # remediation: BlueZoo raises it on request

    def test_bad_token_maps_to_config_error_naming_the_pair(self):
        exc = _classify_http_error(403, '{"error": "BAD_TOKEN"}')
        assert isinstance(exc, BlueZooConfigError)
        assert "BLUEZOO_BASE_URL" in str(exc)  # cluster-scoped pair hint

    def test_other_4xx_is_generic_bluezoo_error(self):
        exc = _classify_http_error(400, "syntax error near WHERE")
        assert type(exc) is BlueZooError


class _FlakyOnce(LiveBlueZooAudienceDataSource):
    """First _call raises the given transient error; second succeeds."""

    def __init__(self, first_error):
        super().__init__()
        self._first_error = first_error
        self.calls = 0

    def _call(self, sql):
        self.calls += 1
        if self.calls == 1:
            raise self._first_error
        return []


class TestRetry:
    def test_transient_failure_retried_once_then_succeeds(self, live_env):
        from app.audience.live_bluezoo import _Retryable

        source = _FlakyOnce(_Retryable("HTTP 503"))
        assert (
            source._query(
                "select timestamp from sensor_visits where timestamp >= '2026-07-20'"
            )
            == []
        )
        assert source.calls == 2

    def test_second_transient_failure_surfaces_typed_error(self, live_env):
        from app.audience.live_bluezoo import _Retryable

        class _AlwaysDown(LiveBlueZooAudienceDataSource):
            def _call(self, sql):
                raise _Retryable("HTTP 503")

        with pytest.raises(BlueZooError, match="after one retry"):
            _AlwaysDown()._query(
                "select timestamp from sensor_visits where timestamp >= '2026-07-20'"
            )

    def test_quota_is_never_retried(self, live_env):
        source = _FlakyOnce(BlueZooQuotaExceeded("allowance exhausted"))
        with pytest.raises(BlueZooQuotaExceeded):
            source._query(
                "select timestamp from sensor_visits where timestamp >= '2026-07-20'"
            )
        assert source.calls == 1

    def test_config_error_is_never_retried(self, live_env):
        source = _FlakyOnce(BlueZooConfigError("BAD_TOKEN"))
        with pytest.raises(BlueZooConfigError):
            source._query(
                "select timestamp from sensor_visits where timestamp >= '2026-07-20'"
            )
        assert source.calls == 1


_SQL_IDS = re.compile(r"sensor_id in \(([\d, ]+)\)")
_SQL_WINDOW = re.compile(r"timestamp >= '([^']+)' and timestamp < '([^']+)'")


class _StubbedLive(LiveBlueZooAudienceDataSource):
    """Deterministic fake server: honors the SQL's sensor + window predicates
    (so screen filtering and windowing are genuinely exercised end-to-end),
    returns rows on the 15-min UTC grid, all valid."""

    def _call(self, sql):
        ids = [int(x) for x in _SQL_IDS.search(sql).group(1).split(",")]
        start = date.fromisoformat(_SQL_WINDOW.search(sql).group(1))
        end = date.fromisoformat(_SQL_WINDOW.search(sql).group(2))
        rows = []
        d = start
        while d < end:
            for hour in (9, 10):
                for minute in (0, 15, 30, 45):
                    for sensor_id in ids:
                        rows.append(
                            {
                                "timestamp": f"{d.isoformat()}T{hour:02d}:{minute:02d}:00Z",
                                "sensor_id": sensor_id,
                                "incoming_inner_count": float(sensor_id % 7)
                                + minute / 60.0,
                                "outgoing_inner_count": 1.5,
                                "incoming_outer_count": 4.0,
                                "outgoing_outer_count": float(sensor_id % 5)
                                + hour / 24.0,
                                "valid": True,
                            }
                        )
            d += timedelta(days=1)
        return rows


class TestGetVisitIntervals:
    def test_maps_sensors_back_to_screens(self, live_env):
        out = _StubbedLive().get_visit_intervals(
            screen_ids=[101, 102], date_from=D_FROM, date_to=D_FROM
        )
        assert out and {iv.screen_id for iv in out} == {101, 102}

    def test_unmapped_screens_yield_no_rows_not_error(self, live_env):
        assert (
            _StubbedLive().get_visit_intervals(
                screen_ids=[999], date_from=D_FROM, date_to=D_FROM
            )
            == []
        )

    def test_occupancy_fields_are_none(self, live_env):
        iv = _StubbedLive().get_visit_intervals(
            screen_ids=[101], date_from=D_FROM, date_to=D_FROM
        )[0]
        assert (
            iv.average_visitors_inner is None
            and iv.maximum_visitors_outer is None
        )

    def test_timestamps_are_naive_utc(self, live_env):
        out = _StubbedLive().get_visit_intervals(
            screen_ids=[101], date_from=D_FROM, date_to=D_FROM
        )
        assert all(iv.timestamp.tzinfo is None for iv in out)

    def test_counts_stay_float(self, live_env):
        iv = _StubbedLive().get_visit_intervals(
            screen_ids=[101], date_from=D_FROM, date_to=D_FROM
        )[0]
        assert isinstance(iv.incoming_inner_count, float)

    def test_ad_campaign_id_is_none_live_rows_know_no_campaigns(self, live_env):
        iv = _StubbedLive().get_visit_intervals(
            screen_ids=[101], date_from=D_FROM, date_to=D_FROM
        )[0]
        assert iv.ad_campaign_id is None

    def test_policy_and_rowcount_logged_per_read(self, live_env, caplog):
        with caplog.at_level(logging.INFO, logger="app.audience.live_bluezoo"):
            _StubbedLive().get_visit_intervals(
                screen_ids=[101], date_from=D_FROM, date_to=D_FROM
            )
        joined = " ".join(r.getMessage() for r in caplog.records)
        assert "policy=valid-only" in joined and "rows=8" in joined

    def test_include_all_policy_reaches_the_sql(self, live_env, monkeypatch):
        from app import config as config_module

        monkeypatch.setattr(
            config_module,
            "BLUEZOO_VALID_POLICY",
            config_module.BlueZooValidPolicy.INCLUDE_ALL,
        )
        seen = {}

        class _Spy(_StubbedLive):
            def _call(self, sql):
                seen["sql"] = sql
                return super()._call(sql)

        _Spy().get_visit_intervals(
            screen_ids=[101], date_from=D_FROM, date_to=D_FROM
        )
        assert " and valid" not in seen["sql"]

    def test_foreign_sensor_in_response_fails_loudly(self, live_env):
        class _Foreign(LiveBlueZooAudienceDataSource):
            def _call(self, sql):
                return [
                    {
                        "timestamp": "2026-07-20T09:00:00Z",
                        "sensor_id": 55555,
                        "incoming_inner_count": 1.0,
                        "outgoing_inner_count": 1.0,
                        "incoming_outer_count": 1.0,
                        "outgoing_outer_count": 1.0,
                        "valid": True,
                    }
                ]

        with pytest.raises(BlueZooError, match="unmapped sensor_id=55555"):
            _Foreign().get_visit_intervals(
                screen_ids=[101], date_from=D_FROM, date_to=D_FROM
            )


FIXTURE = Path(__file__).parent / "data" / "bluezoo_sensor_visits_sample.json"
FIXTURE_D_FROM = date(2026, 7, 25)  # <- capture's D1
FIXTURE_D_TO = date(2026, 7, 25)


@pytest.fixture
def live_env_real_sensors(monkeypatch):
    """Same shape as `live_env`, but mapped to this fixture's real captured
    sensor ids (77, 80) rather than `live_env`'s placeholder 87/433 — Task 6's
    Step 0 evidence-based selection (see bluezoo_sensor_visits_sample.md)
    picked 77/80, not 87/433."""
    monkeypatch.setenv("BLUEZOO_BASE_URL", "https://stub.invalid/v2/dwh")
    monkeypatch.setenv("BLUEZOO_ACCESS_KEY", "stub-key")
    monkeypatch.setenv("BLUEZOO_SENSOR_MAP", "101:77,102:80")
    monkeypatch.setattr("app.audience.live_bluezoo._RETRY_BACKOFF_SECONDS", 0)


class TestRealPayloadFixture:
    """The dropped cached provider's replacement: a REAL MO_92 payload,
    parsed by the real conformer code on every fast-tier run."""

    def test_real_payload_parses_into_dtos(self, live_env_real_sensors):
        raw = json.loads(FIXTURE.read_text())
        assert raw, "fixture must not be empty"

        class _Replay(LiveBlueZooAudienceDataSource):
            def _call(self, sql):
                return raw

        # Fixture rows are for sensors 77/80 == mapped screens 101/102
        # (Step 0 evidence-based selection; see bluezoo_sensor_visits_sample.md).
        out = _Replay().get_visit_intervals(
            screen_ids=[101, 102],
            date_from=FIXTURE_D_FROM,
            date_to=FIXTURE_D_TO,
        )
        assert len(out) == len(raw)
        assert {iv.screen_id for iv in out} == {101, 102}
        assert all(iv.timestamp.tzinfo is None for iv in out)
        assert all(
            iv.timestamp.minute % 15 == 0 and iv.timestamp.second == 0
            for iv in out
        )
        assert all(isinstance(iv.incoming_inner_count, float) for iv in out)
        assert all(isinstance(iv.valid, bool) for iv in out)
        assert all(iv.average_visitors_inner is None for iv in out)
