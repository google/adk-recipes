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

"""Tests verifying `InMemoryRootCauseStore` integration with `InMemoryInsightReader`.

Ensures root cause diagnoses persisted in memory are correctly queried,
versioned, and scoped by occurrence and insight ID.
"""

from __future__ import annotations

import datetime as dt

import pytest
from ambient_quality_agent.standalone.insight_reader import (
    InMemoryInsightReader,
)
from ambient_quality_agent.standalone.root_causes import InMemoryRootCauseStore
from ambient_quality_agent.standalone.store import InMemoryStore
from ambient_quality_agent.tools.insights.models import RootCause

_AGENT = "watched-agent"
_NOW = dt.datetime(2026, 6, 1, tzinfo=dt.UTC)


@pytest.fixture
def backing() -> InMemoryStore:
    return InMemoryStore()


@pytest.fixture
def store(backing: InMemoryStore) -> InMemoryRootCauseStore:
    return InMemoryRootCauseStore(backing)


@pytest.fixture
def reader(backing: InMemoryStore) -> InMemoryInsightReader:
    return InMemoryInsightReader(backing, agent_name=_AGENT)


def _root_cause(
    root_cause_id: str,
    *,
    occurrence_id: str = "occ-1",
    insight_id: str = "insight-1",
    summary: str = "the retry loop never terminates",
    created_at: dt.datetime = _NOW,
) -> RootCause:
    return RootCause(
        root_cause_id=root_cause_id,
        insight_id=insight_id,
        occurrence_id=occurrence_id,
        agent_revision="v7",
        summary=summary,
        created_at=created_at,
    )


def test_a_diagnosis_is_readable_once_it_is_saved(
    store: InMemoryRootCauseStore, reader: InMemoryInsightReader
) -> None:
    store.save(_root_cause("rc-1"))

    assert [r.root_cause_id for r in reader.list_root_causes("insight-1")] == [
        "rc-1"
    ]


def test_a_revision_is_another_record_and_the_newest_is_current(
    store: InMemoryRootCauseStore, reader: InMemoryInsightReader
) -> None:
    """Appended rather than edited, so how a diagnosis changed stays readable."""
    store.save(_root_cause("rc-1", summary="first guess"))
    store.save(
        _root_cause(
            "rc-2",
            summary="second guess",
            created_at=_NOW + dt.timedelta(hours=1),
        )
    )

    current = reader.list_root_causes("insight-1")
    history = reader.list_root_causes("insight-1", history=True)

    assert [r.root_cause_id for r in current] == ["rc-2"]
    assert [r.root_cause_id for r in history] == ["rc-2", "rc-1"]


def test_each_occurrence_keeps_its_own_current_diagnosis(
    store: InMemoryRootCauseStore, reader: InMemoryInsightReader
) -> None:
    store.save(_root_cause("rc-1", occurrence_id="occ-1"))
    store.save(_root_cause("rc-2", occurrence_id="occ-2"))

    current = reader.list_root_causes("insight-1")

    assert {r.root_cause_id for r in current} == {"rc-1", "rc-2"}


def test_a_diagnosis_belongs_to_the_insight_it_names(
    store: InMemoryRootCauseStore, reader: InMemoryInsightReader
) -> None:
    store.save(_root_cause("rc-1", insight_id="insight-1"))
    store.save(_root_cause("rc-2", insight_id="insight-2"))

    assert [r.root_cause_id for r in reader.list_root_causes("insight-2")] == [
        "rc-2"
    ]
