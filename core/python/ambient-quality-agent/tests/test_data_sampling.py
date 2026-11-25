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

"""Tests for how an investigation chooses and applies its sampling method."""

from __future__ import annotations

from typing import Any
from unittest import mock

import pytest
from ambient_quality_agent import backends
from ambient_quality_agent.config import (
    ALLOWED_DATA_SAMPLING,
    DEFAULT_DATA_SAMPLING,
)
from ambient_quality_agent.core.nodes import _common
from ambient_quality_agent.tools.ingestion.sampling import SamplingMethod
from ambient_quality_agent.tools.investigations.models import CustomOverrides

from .conftest import StateContext, make_config

_STATE: dict[str, Any] = {
    "project_id": "p",
    "observed_agent_name": "watched-agent",
    "telemetry_ingestion_source": "big_query",
    "telemetry_dataset": "some_dataset",
}


def test_the_config_names_exactly_the_sampling_methods() -> None:
    assert ALLOWED_DATA_SAMPLING == {method.value for method in SamplingMethod}
    assert DEFAULT_DATA_SAMPLING == SamplingMethod.WEIGHTED


def test_an_unknown_sampling_method_is_refused() -> None:
    with pytest.raises(ValueError, match="data_sampling must be one of"):
        make_config(data_sampling="risky")


@pytest.mark.parametrize(
    ("state", "expected"),
    [
        ({}, SamplingMethod.WEIGHTED),
        ({"data_sampling": "uniform"}, SamplingMethod.UNIFORM),
        ({"data_sampling": "weighted"}, SamplingMethod.WEIGHTED),
    ],
)
def test_the_fetcher_samples_as_the_investigation_says(
    state: dict[str, Any], expected: SamplingMethod
) -> None:
    with mock.patch.object(backends.bigquery, "Client"):
        fetcher = _common._create_default_fetcher(
            StateContext({**_STATE, **state})
        )

    assert fetcher._sampling_method is expected


@pytest.mark.parametrize(
    ("overrides", "attached", "expected"),
    [
        # A selector names the slice the user chose.
        (CustomOverrides(selector_sql="SELECT 1"), "weighted", "uniform"),
        (CustomOverrides(review_focus="refunds"), "weighted", "weighted"),
        (CustomOverrides(), "uniform", "uniform"),
        (
            CustomOverrides(selector_sql="SELECT 1", sampling="weighted"),
            "uniform",
            "weighted",
        ),
        (CustomOverrides(sampling="uniform"), "weighted", "uniform"),
    ],
)
def test_a_custom_investigation_samples_by_its_own_choice_or_the_default(
    overrides: CustomOverrides, attached: str, expected: str
) -> None:
    assert overrides.resolve_sampling(attached) == expected
