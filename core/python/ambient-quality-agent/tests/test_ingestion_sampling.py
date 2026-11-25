# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Tests for weighted sampling: the weight, the sampler and the fetchers' SQL."""

from __future__ import annotations

import datetime as dt
import logging
import random
import statistics
from typing import Any
from unittest import mock

import pytest
from ambient_quality_agent.tools.evaluation.models import MetricType
from ambient_quality_agent.tools.ingestion import sampling
from ambient_quality_agent.tools.ingestion.base import BigQueryJobFetcher
from ambient_quality_agent.tools.ingestion.bigquery_fetcher import (
    BigQueryFetcher,
)
from ambient_quality_agent.tools.ingestion.cloud_logging_fetcher import (
    CloudLoggingFetcher,
)
from ambient_quality_agent.tools.ingestion.cloud_ops_fetcher import (
    CloudOpsFetcher,
)
from ambient_quality_agent.tools.ingestion.models import (
    SamplingRecord,
)
from ambient_quality_agent.tools.ingestion.sampling import SamplingMethod
from ambient_quality_agent.tools.trajectories.models import SamplingSignals
from google.cloud import bigquery

from . import sampling_reference as reference
from .sampling_reference import TrajectoryMeasures

_START = dt.datetime(2026, 1, 1, tzinfo=dt.UTC)
_END = dt.datetime(2026, 1, 8, tzinfo=dt.UTC)

_LABELS = (
    "gen_ai_conversation_id",
    "gen_ai_agent_name",
    "gen_ai_response_finish_reasons",
    "gen_ai_usage_input_tokens",
    "gen_ai_usage_output_tokens",
    "gen_ai_output_messages_ref",
)

_SELECTOR = (
    "SELECT session_id AS target_id FROM `p.d.t` "
    "WHERE timestamp BETWEEN @window_start AND @window_end "
    "AND agent = @agent_name"
)

_FETCHERS = (BigQueryFetcher, CloudOpsFetcher, CloudLoggingFetcher)


def _build_fetcher(
    fetcher_class: type[BigQueryJobFetcher],
    method: SamplingMethod = SamplingMethod.WEIGHTED,
    *,
    client: mock.MagicMock | None = None,
    selector_sql: str = "",
    labels: tuple[str, ...] = _LABELS,
) -> BigQueryJobFetcher:
    client = client or mock.MagicMock()
    client.get_table.return_value.schema = [
        bigquery.SchemaField(
            "labels",
            "RECORD",
            fields=[bigquery.SchemaField(name, "STRING") for name in labels],
        )
    ]
    with (
        mock.patch(
            "ambient_quality_agent.tools.ingestion.base.bigquery.Client",
            return_value=client,
        ),
        mock.patch("ambient_quality_agent.tools.ingestion.base.storage"),
    ):
        return fetcher_class(
            project_id="p",
            dataset="d",
            table="t",
            location="us-central1",
            selector_sql=selector_sql,
            sampling_method=method,
        )


def _extract_bound_parameters(client: mock.MagicMock) -> dict[str, Any]:
    job_config = client.query.call_args.kwargs["job_config"]
    return {param.name: param.value for param in job_config.query_parameters}


# --- The weight ------------------------------------------------------------


def test_a_tail_score_is_zero_up_to_the_cutoff_and_one_at_the_top() -> None:
    percentiles = reference.compute_percentiles(list(range(101)))
    scores = [reference.compute_tail_score(p) for p in percentiles]

    assert percentiles[50] == pytest.approx(0.5)
    assert scores[50] == 0
    assert scores[90] == pytest.approx(0)
    assert scores[95] == pytest.approx(0.5)
    assert scores[100] == pytest.approx(1)


def test_tied_values_share_a_percentile_and_a_missing_value_has_none() -> None:
    """`PERCENT_RANK()` ranks ties at their first position."""
    values: list[int | None] = [None, *[1] * 18, 3, 3]

    percentiles = reference.compute_percentiles(values)

    assert percentiles[0] is None
    assert reference.compute_tail_score(percentiles[0]) == 0
    assert percentiles[1:19] == [0.0] * 18
    # Both 3s rank 19th of the 20 recorded values: percentile 18 / 19.
    assert percentiles[19] == percentiles[20] == pytest.approx(18 / 19)


def test_missing_values_push_no_recorded_value_into_the_tail() -> None:
    """Ranked with the missing values below them, all five recorded values
    would sit above the 90th percentile."""
    values: list[int | None] = [*[None] * 95, 1, 2, 3, 4, 5]

    percentiles = reference.compute_percentiles(values)

    assert percentiles[95:] == [0.0, 0.25, 0.5, 0.75, 1.0]
    assert [reference.compute_tail_score(p) for p in percentiles[95:]] == [
        0,
        0,
        0,
        0,
        1,
    ]


def test_a_single_candidate_ranks_at_zero() -> None:
    assert reference.compute_percentiles([7]) == [0.0]


def test_signals_keep_the_measures_and_their_percentiles() -> None:
    measures = [TrajectoryMeasures(total_tokens=v) for v in range(1, 11)]

    _, signals = reference.compute_weights(measures)[-1]

    assert signals.total_tokens == 10
    assert signals.cost_percentile == 1
    assert signals.cost_tail == 1
    assert signals.busy_turn_percentile is None
    assert signals.busy_turn_tail == 0


def test_a_signal_adds_its_coefficient_to_a_base_of_one() -> None:
    measures = [TrajectoryMeasures() for _ in range(99)]
    measures.append(
        TrajectoryMeasures(
            has_error=True,
            has_abnormal_finish=True,
            has_tool_reported_error=True,
        )
    )

    weights = reference.compute_weights(measures)

    assert weights[0][0] == 1
    assert weights[-1][0] == pytest.approx(
        1
        + sampling.ERROR_COEFFICIENT
        + sampling.ABNORMAL_FINISH_COEFFICIENT
        + sampling.TOOL_REPORTED_ERROR_COEFFICIENT
    )
    assert weights[-1][1] == SamplingSignals(
        has_error=True, has_abnormal_finish=True, has_tool_reported_error=True
    )


def test_common_signals_are_scaled_to_an_average_added_weight_of_one() -> None:
    """Half the window erring averages 2 added, so the cap halves it."""
    measures = [TrajectoryMeasures(has_error=i % 2 == 0) for i in range(100)]

    weights = [weight for weight, _ in reference.compute_weights(measures)]

    assert weights[0] == pytest.approx(1 + sampling.ERROR_COEFFICIENT / 2)
    assert weights[1] == 1
    assert statistics.fmean(weights) == pytest.approx(2)


def test_rare_signals_are_not_scaled() -> None:
    measures = [TrajectoryMeasures(has_error=i == 0) for i in range(100)]

    weights = [weight for weight, _ in reference.compute_weights(measures)]

    assert weights[0] == 1 + sampling.ERROR_COEFFICIENT


def test_a_window_without_signals_weighs_every_trajectory_one() -> None:
    weights = reference.compute_weights([TrajectoryMeasures()] * 10)

    assert {weight for weight, _ in weights} == {1.0}


# --- The sampler -----------------------------------------------------------


def test_every_candidate_is_picked_with_certainty_when_they_fit() -> None:
    picked = reference.draw_priority_sample(
        [1.0, 5.0, 2.0], 3, random.Random(0)
    )

    assert sorted(picked) == [(0, 1.0), (1, 1.0), (2, 1.0)]


def test_picks_follow_the_weights_and_the_estimates_are_unbiased() -> None:
    """A weight-5 candidate is picked about five times as often as a weight-1
    one, and summing 1 / p over each draw estimates both group sizes."""
    rng = random.Random(1)
    weights = [5.0] * 50 + [1.0] * 950
    runs = 2000
    heavy_picks = light_picks = 0
    heavy_estimates, light_estimates = [], []
    for _ in range(runs):
        picked = reference.draw_priority_sample(weights, 100, rng)
        heavy = [p for index, p in picked if index < 50]
        light = [p for index, p in picked if index >= 50]
        heavy_picks += len(heavy)
        light_picks += len(light)
        heavy_estimates.append(sum(1 / p for p in heavy))
        light_estimates.append(sum(1 / p for p in light))

    heavy_rate = heavy_picks / runs / 50
    light_rate = light_picks / runs / 950
    assert heavy_rate / light_rate == pytest.approx(5, rel=0.05)
    assert statistics.fmean(heavy_estimates) == pytest.approx(50, rel=0.02)
    assert statistics.fmean(light_estimates) == pytest.approx(950, rel=0.02)


def test_a_sampling_column_reads_into_a_record() -> None:
    record = sampling.parse_sampling_record(
        {
            "weight": 3.0,
            "inclusion_probability": 0.25,
            "signals": {
                "has_error": True,
                "has_abnormal_finish": False,
                "busy_turn_tail": 0.0,
                "cost_tail": 0.5,
                "latency_tail": 0.0,
            },
        }
    )

    assert record == SamplingRecord(
        inclusion_probability=0.25,
        weight=3.0,
        signals=SamplingSignals(has_error=True, cost_tail=0.5),
    )


def test_a_uniform_sampling_column_reads_without_weight_or_signals() -> None:
    assert sampling.parse_sampling_record(
        {"inclusion_probability": 0.1}
    ) == SamplingRecord(inclusion_probability=0.1)


@pytest.mark.parametrize(
    "value",
    [
        None,
        {},
        {"weight": 2.0},
        "0.5",
        {"inclusion_probability": 0.0},
        {"inclusion_probability": -0.5},
        {"inclusion_probability": 1.5},
    ],
)
def test_a_row_without_a_usable_probability_has_no_record(value: Any) -> None:
    assert sampling.parse_sampling_record(value) is None


def test_a_certain_pick_reads_into_a_record() -> None:
    assert sampling.parse_sampling_record(
        {"inclusion_probability": 1.0}
    ) == SamplingRecord(inclusion_probability=1.0)


# --- The SQL ---------------------------------------------------------------


@pytest.mark.parametrize("fetcher_class", _FETCHERS)
@pytest.mark.parametrize("metric_type", list(MetricType))
def test_the_weighted_expressions_appear_only_under_weighted_sampling(
    fetcher_class: type[BigQueryJobFetcher], metric_type: MetricType
) -> None:
    weighted = _build_fetcher(fetcher_class)._build_ingestion_sql(metric_type)
    uniform = _build_fetcher(
        fetcher_class, SamplingMethod.UNIFORM
    )._build_ingestion_sql(metric_type)

    for name in ("Scored AS", "Keyed AS", "Prioritized AS", "FARM_FINGERPRINT"):
        assert name in weighted
        assert name not in uniform
    assert "ORDER BY RAND()" in uniform
    assert "RAND()" not in weighted


@pytest.mark.parametrize("fetcher_class", _FETCHERS)
@pytest.mark.parametrize("method", list(SamplingMethod))
@pytest.mark.parametrize("metric_type", list(MetricType))
def test_every_ingestion_row_carries_how_it_was_picked(
    fetcher_class: type[BigQueryJobFetcher],
    method: SamplingMethod,
    metric_type: MetricType,
) -> None:
    sql = _build_fetcher(fetcher_class, method)._build_ingestion_sql(
        metric_type
    )

    # The query below the sampling expressions carries the column through.
    end_of_sample = (
        "WHERE sample_rank <= @limit\n)"
        if method is SamplingMethod.WEIGHTED
        else "LIMIT @limit\n)"
    )
    below = sql.split(end_of_sample, 1)[1]
    assert (
        f"ANY_VALUE(t.{sampling.SAMPLING_COLUMN}) AS {sampling.SAMPLING_COLUMN}"
        in below
    )


@pytest.mark.parametrize("fetcher_class", _FETCHERS)
@pytest.mark.parametrize("method", list(SamplingMethod))
@pytest.mark.parametrize("metric_type", list(MetricType))
def test_the_count_measures_the_population_the_sample_is_drawn_from(
    fetcher_class: type[BigQueryJobFetcher],
    method: SamplingMethod,
    metric_type: MetricType,
) -> None:
    fetcher = _build_fetcher(fetcher_class, method)
    candidates, _ = fetcher._resolve_candidates(metric_type)

    count = fetcher._build_count_sql(metric_type)

    assert candidates in fetcher._build_ingestion_sql(metric_type)
    assert count == f"SELECT COUNT(*) AS scanned FROM ({candidates})"  # noqa: S608 - trusted test constants


@pytest.mark.parametrize("fetcher_class", _FETCHERS)
def test_a_weighted_selector_runs_once_and_is_scored_like_the_window(
    fetcher_class: type[BigQueryJobFetcher],
) -> None:
    """A selector can call a model on every row, so it must not be evaluated
    once for each reference to its expression."""
    sql = _build_fetcher(
        fetcher_class, selector_sql=_SELECTOR
    )._build_ingestion_sql(MetricType.MULTI_TURN)

    assert sql.count(_SELECTOR) == 1
    assert sql.count("FROM Candidates") == 1
    assert sql.split("Candidates AS (\n", 1)[1].startswith(
        "SELECT DISTINCT target_id AS session_id FROM (\n" + _SELECTOR  # noqa: S608 - trusted test constants
    )


@pytest.mark.parametrize("fetcher_class", _FETCHERS)
def test_a_weighted_run_binds_the_seed_and_the_threshold_row(
    fetcher_class: type[BigQueryJobFetcher],
) -> None:
    client = mock.MagicMock()
    fetcher = _build_fetcher(fetcher_class, client=client)

    fetcher.submit_query(
        "agent-a",
        MetricType.MULTI_TURN,
        _START,
        _END,
        limit=7,
        sampling_seed="run-1",
    )

    assert _extract_bound_parameters(client) == {
        "agent_name": "agent-a",
        "window_start": _START,
        "window_end": _END,
        "limit": 7,
        "limit_plus_one": 8,
        "sampling_seed": "run-1",
    }


def test_a_weighted_run_without_a_seed_draws_a_fresh_one() -> None:
    client = mock.MagicMock()
    fetcher = _build_fetcher(BigQueryFetcher, client=client)

    seeds = set()
    for _ in range(2):
        fetcher.submit_query(
            "agent-a", MetricType.MULTI_TURN, _START, _END, limit=7
        )
        seeds.add(_extract_bound_parameters(client)["sampling_seed"])

    assert len(seeds) == 2


@pytest.mark.parametrize(
    ("candidates", "weighted"),
    [
        (sampling.WEIGHTED_CANDIDATE_LIMIT, True),
        (sampling.WEIGHTED_CANDIDATE_LIMIT + 1, False),
    ],
)
def test_a_window_too_large_to_rank_is_sampled_uniformly(
    candidates: int, weighted: bool, caplog: pytest.LogCaptureFixture
) -> None:
    client = mock.MagicMock()
    client.query.return_value.result.return_value = [{"scanned": candidates}]
    fetcher = _build_fetcher(BigQueryFetcher, client=client)
    fetcher.count_scanned("agent-a", MetricType.MULTI_TURN, _START, _END)

    with caplog.at_level(logging.WARNING):
        fetcher.submit_query(
            "agent-a",
            MetricType.MULTI_TURN,
            _START,
            _END,
            limit=7,
            sampling_seed="run-1",
        )

    sql = client.query.call_args.args[0]
    assert ("Prioritized AS" in sql) is weighted
    assert ("ORDER BY RAND()" in sql) is not weighted
    assert ("sampling_seed" in _extract_bound_parameters(client)) is weighted
    assert (
        any("sampling them uniformly" in r.getMessage() for r in caplog.records)
        is not weighted
    )


def test_a_uniform_run_binds_no_sampling_parameter() -> None:
    client = mock.MagicMock()
    fetcher = _build_fetcher(
        BigQueryFetcher, SamplingMethod.UNIFORM, client=client
    )

    fetcher.submit_query(
        "agent-a",
        MetricType.MULTI_TURN,
        _START,
        _END,
        limit=7,
        sampling_seed="run-1",
    )

    assert set(_extract_bound_parameters(client)) == {
        "agent_name",
        "window_start",
        "window_end",
        "limit",
    }


def test_the_selector_precheck_dry_runs_the_weighted_query_it_will_run() -> (
    None
):
    client = mock.MagicMock()
    client.query.return_value.referenced_tables = []
    client.query.return_value._properties = {}
    fetcher = _build_fetcher(
        BigQueryFetcher, client=client, selector_sql=_SELECTOR
    )

    fetcher.precheck_selector("agent-a", _START, _END, limit=5)

    runs = [
        call
        for call in client.query.call_args_list
        if "Prioritized AS" in call.args[0]
    ]
    assert len(runs) == 1
    parameters = {
        param.name for param in runs[0].kwargs["job_config"].query_parameters
    }
    assert {"limit_plus_one", "sampling_seed"} <= parameters


@pytest.mark.parametrize("method", list(SamplingMethod))
def test_the_count_records_only_the_candidates(method: SamplingMethod) -> None:
    client = mock.MagicMock()
    client.query.return_value.result.return_value = [{"scanned": 10}]
    fetcher = _build_fetcher(BigQueryFetcher, method, client=client)

    scanned = fetcher.count_scanned(
        "agent-a", MetricType.MULTI_TURN, _START, _END
    )

    assert scanned == fetcher.counts.scanned == 10
    assert "Measures" not in client.query.call_args.args[0]
    assert set(_extract_bound_parameters(client)) == {
        "agent_name",
        "window_start",
        "window_end",
    }


def test_each_mapped_case_keeps_how_it_was_picked() -> None:
    fetcher = _build_fetcher(BigQueryFetcher)
    row = {
        "session_id": "s1",
        "events": [
            {
                "invocation_id": "i1",
                "event_type": "USER_MESSAGE_RECEIVED",
                "agent": "a",
                "content": {"text_summary": "hi"},
            }
        ],
        "sampling": {
            "weight": 5.0,
            "inclusion_probability": 0.5,
            "signals": {"has_error": True},
        },
    }

    _, _, samplings = fetcher._map_page([row], MetricType.MULTI_TURN)

    assert samplings == {
        "s1": SamplingRecord(
            inclusion_probability=0.5,
            weight=5.0,
            signals=SamplingSignals(has_error=True),
        )
    }


def test_cloud_logging_reads_a_silent_entry_as_an_error_only_with_both_labels() -> (
    None
):
    """A sink that never received a finish reason or an output would read
    every entry as a failure."""
    with_both = _build_fetcher(CloudLoggingFetcher)._build_measures_sql(
        MetricType.MULTI_TURN
    )
    without_output = _build_fetcher(
        CloudLoggingFetcher,
        labels=tuple(
            name for name in _LABELS if name != "gen_ai_output_messages_ref"
        ),
    )._build_measures_sql(MetricType.MULTI_TURN)

    assert "NULLIF(labels.gen_ai_output_messages_ref, '') IS NULL" in with_both
    assert (
        "NULLIF(labels.gen_ai_response_finish_reasons, '') IS NULL AND"
        in with_both
    )
    assert "labels.gen_ai_response_finish_reasons, '') IS NULL AND" not in (
        without_output
    )
    # `error_type` was never written, so it reads as a typed NULL.
    assert "CAST(NULL AS STRING) IS NOT NULL" in without_output


@pytest.mark.parametrize(
    ("metric_type", "unit"),
    [
        (MetricType.MULTI_TURN, "labels.gen_ai_conversation_id"),
        (MetricType.SINGLE_TURN, "trace"),
    ],
)
def test_cloud_logging_measures_every_call_of_the_agents_units(
    metric_type: MetricType, unit: str
) -> None:
    """A sub-agent's entries carry its own name, so filtering entries by the
    observed agent's name would leave out the calls it delegated."""
    sql = " ".join(
        _build_fetcher(CloudLoggingFetcher)
        ._build_measures_sql(metric_type)
        .split()
    )

    assert (
        f"AND {unit} IN ( SELECT {unit} FROM `p.d.t` WHERE timestamp BETWEEN"  # noqa: S608 - trusted test constants
        " @window_start AND @window_end AND labels.gen_ai_agent_name = @agent_name )"
    ) in sql
    assert sql.count("labels.gen_ai_agent_name = @agent_name") == 1
    assert "COUNT(*) AS calls" in sql
    assert (
        "COALESCE(MAX(SAFE_CAST(labels.gen_ai_usage_input_tokens AS INT64)), 0)"
    ) in sql


@pytest.mark.parametrize("fetcher_class", _FETCHERS)
@pytest.mark.parametrize("metric_type", list(MetricType))
def test_each_source_keeps_the_measure_choices_the_weight_relies_on(
    fetcher_class: type[BigQueryJobFetcher], metric_type: MetricType
) -> None:
    """Choices a later edit could silently undo. The SQL is run against
    BigQuery outside this repository; these only guard that each is there."""
    fetcher = _build_fetcher(fetcher_class)
    measures = " ".join(fetcher._build_measures_sql(metric_type).split())

    # Missing measures are ranked apart, so they push no recorded value up.
    assert "PARTITION BY m.total_tokens IS NULL" in (
        fetcher._build_ingestion_sql(metric_type)
    )
    # Finish reasons are compared in lowercase against one list.
    assert "LOWER(" in measures
    assert "NOT IN ('stop', " in measures
    if fetcher_class is CloudLoggingFetcher:
        # The log records no tool calls.
        assert "FALSE AS has_tool_reported_error" in measures
    else:
        # A hand-off to another agent is not a busy turn.
        assert "IS DISTINCT FROM 'transfer_to_agent'" in measures


@pytest.mark.parametrize("fetcher_class", _FETCHERS)
@pytest.mark.parametrize("metric_type", list(MetricType))
def test_each_source_projects_every_measure_column(
    fetcher_class: type[BigQueryJobFetcher], metric_type: MetricType
) -> None:
    sql = _build_fetcher(fetcher_class)._build_measures_sql(metric_type)

    for column in sampling.MEASURE_COLUMNS:
        assert f"AS {column}" in sql or f" {column}," in sql


def test_bigquery_counts_every_response_of_a_turn_without_a_final_one() -> None:
    """ADK 1.35's plugin marks no response final, and its turns still have
    token counts."""
    sql = _build_fetcher(BigQueryFetcher)._build_measures_sql(
        MetricType.MULTI_TURN
    )

    assert "SUM(IF(event_type = 'LLM_RESPONSE'," in " ".join(sql.split())


def test_cloud_trace_tells_a_raised_tool_error_from_a_reported_one() -> None:
    """ADK records an `exception` event on a tool span only when the tool
    raised; a result reporting a failure fails the span without one."""
    sql = _build_fetcher(CloudOpsFetcher)._build_measures_sql(
        MetricType.SINGLE_TURN
    )

    assert "event.name = 'exception'" in sql
    assert "AS has_tool_reported_error" in sql
