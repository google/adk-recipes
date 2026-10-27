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

"""Tests for cost attribution: the `component=aqa` billing label.

Billing has no principal dimension, so an unlabeled call is an unattributable
call -- silently, and only discoverable months later in the billing export.
These tests pin the label onto every surface that spends money: ADK model
calls, the two direct GenAI call sites, and the BigQuery jobs. The Agent Runtime
resource label is not covered -- see the note at the end of this file.
"""

from __future__ import annotations

import asyncio
import datetime as dt
from types import SimpleNamespace
from typing import Any
from unittest import mock

import pytest
from ambient_quality_agent.core.labels import (
    BILLING_LABELS,
    build_request_labels,
)
from ambient_quality_agent.core.models import LocatedGemini
from google.adk.models.llm_request import LlmRequest

_EXPECTED = {"component": "aqa"}


# --- the label itself ---------------------------------------------------------


def test_the_billing_label_is_component_aqa() -> None:
    """One simple label, shared by resources, model requests, and BQ jobs."""
    assert BILLING_LABELS == _EXPECTED
    assert build_request_labels() == _EXPECTED


def test_request_labels_hands_out_a_fresh_copy() -> None:
    """SDK config objects keep (and sometimes mutate) the map they are given."""
    labels = build_request_labels()
    labels["scribbled"] = "on"

    assert build_request_labels() == _EXPECTED
    assert BILLING_LABELS == _EXPECTED


# --- ADK model calls (orchestrator, metric builder/planner, RCA, summary) -----


def test_adk_model_calls_carry_the_label() -> None:
    """`LocatedGemini` is the chokepoint for all five ADK agents.

    They set no `generate_content_config` of their own, so if this override
    stops stamping, every one of them goes unattributed at once.
    """
    request = LlmRequest()

    asyncio.run(
        LocatedGemini(
            model="gemini-3.5-flash", location="global"
        )._preprocess_request(request)
    )

    assert request.config.labels == _EXPECTED


def test_caller_set_labels_win_over_the_default() -> None:
    """Stamping is a floor, not a ceiling -- an explicit label is never clobbered."""
    request = LlmRequest()
    request.config.labels = {"component": "explicit", "extra": "kept"}

    asyncio.run(
        LocatedGemini(
            model="gemini-3.5-flash", location="global"
        )._preprocess_request(request)
    )

    assert request.config.labels == {"component": "explicit", "extra": "kept"}


# --- direct GenAI call sites (they bypass the ADK chokepoint) -----------------


def test_insight_clustering_call_carries_the_label(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ambient_quality_agent.tools.insights import clustering

    from .conftest import make_config

    captured: dict[str, Any] = {}

    class _FakeModels:
        def generate_content(
            self, *, model: str, contents: str, config: Any
        ) -> Any:
            del model, contents
            captured["labels"] = config.labels
            return SimpleNamespace(text='{"clusters": []}')

    class _FakeClient:
        def __init__(self, **_: Any) -> None:
            self.models = _FakeModels()

    monkeypatch.setattr("ambient_quality_agent.config.config", make_config())
    monkeypatch.setattr("google.genai.Client", _FakeClient)

    clustering.call_clustering_model("prompt")

    assert captured["labels"] == _EXPECTED


# --- BigQuery jobs -----------------------------------------------------------


def test_insight_store_jobs_carry_the_label() -> None:
    """The label is what attributes the bytes a job scans to AQA."""
    from ambient_quality_agent.tools.insights.bigquery_store import (
        BigQueryInsightStore,
    )

    client = mock.MagicMock()
    store = BigQueryInsightStore(
        client=client,
        project_id="test-project",
        dataset="aqua_insights",
        agent_name="root_agent",
    )

    store.resolve_stale_insights(dt.datetime(2026, 6, 1, tzinfo=dt.UTC), 14)

    assert client.query.call_args.kwargs["job_config"].labels == _EXPECTED


def test_ingestion_jobs_carry_the_label(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ambient_quality_agent.tools.evaluation.models import MetricType
    from ambient_quality_agent.tools.ingestion.bigquery_fetcher import (
        BigQueryFetcher,
    )

    monkeypatch.setattr("google.cloud.bigquery.Client", mock.MagicMock())
    fetcher = BigQueryFetcher(
        project_id="test-project",
        dataset="traces",
        table="spans",
        location="us-central1",
    )

    fetcher.submit_query(
        agent_name="root_agent",
        metric_type=MetricType.SINGLE_TURN,
        start=dt.datetime(2026, 6, 1, tzinfo=dt.UTC),
        end=dt.datetime(2026, 6, 2, tzinfo=dt.UTC),
    )

    query = fetcher._client.query
    assert query.call_args.kwargs["job_config"].labels == _EXPECTED


# --- deploy-time resource label -----------------------------------------------
#
# Resource labels applied by agents-cli at deploy time are tested in agents-cli.
