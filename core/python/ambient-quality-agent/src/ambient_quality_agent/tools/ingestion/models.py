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

"""Shared ingestion data models."""

from __future__ import annotations

import dataclasses

from agentplatform._genai.types import EvalCase, EvaluationDataset


@dataclasses.dataclass(frozen=True)
class MappedRow:
    """Represents the ingestion outcome of a single source row.

    Distinguishes between a complete case, a case built with partial telemetry loss,
    and a failed mapping where the row could not be evaluated.
    """

    case: EvalCase | None = None
    """The mapped case, or ``None`` when the row could not be used at all."""

    partial: bool = False
    """Whether the case was built from incomplete telemetry. Ignored when
    `case` is ``None`` because a dropped row retains no case data."""

    agent_revision: str = ""
    """Deployment revision of the agent that produced the row's telemetry.
    Empty for sources that do not report one, which denotes their single
    unnamed revision rather than an unknown one."""

    trajectory_id: str = ""
    """Identifier for the trajectory: the session ID for multi-turn evaluations,
    or the invocation/trace ID for single-turn evaluations.

    Populated for all outcomes, including when `case` is ``None``, allowing the
    trajectory store to record dropped cases that have no `EvalCase` instance."""

    session_id: str | None = None
    """Session ID associated with this row, or ``None`` if unspecified by the source.
    For single-turn trajectories, this identifies the parent conversation."""

    trace_ids: tuple[str, ...] = ()
    """Cloud Trace IDs comprising the trajectory in chronological order.
    The first trace ID corresponds to the opening turn used for console links.
    Empty for sources whose schema does not record trace IDs."""


@dataclasses.dataclass
class IngestionCounts:
    """Cumulative row ingestion outcomes for a fetcher across an entire scope.

    Accumulated across all pages so that the evaluation step can aggregate
    outcomes in a single pass.
    """

    scanned: int = 0
    """Traces the window held before sampling; from `BaseFetcher.count_scanned`."""

    ingested: int = 0
    """Rows mapped to a complete case."""

    partial: int = 0
    """Rows mapped to a case that lost some telemetry."""

    failed: int = 0
    """Rows dropped before evaluation."""


@dataclasses.dataclass(frozen=True)
class Page:
    """One page of evaluation data returned by `BaseFetcher.fetch_page`."""

    dataset: EvaluationDataset
    """The page's evaluation cases, as an `EvaluationDataset`."""

    next_page_token: str | None
    """Token to pass to the next `fetch_page` call, or ``None`` if this is
    the last page."""

    memory_truncated: bool = False
    """Whether mapping stopped early because the process neared its memory
    limit. When set, the fetcher also clears `next_page_token` so paging
    stops; the flag lets callers report the run as truncated."""

    revisions: dict[str, str] = dataclasses.field(default_factory=dict)
    """Deployment revision per case, keyed by ``eval_case_id`` (session ID for
    multi-turn, trace ID for single-turn). Empty for sources that do not report
    revisions, which denotes a single unnamed revision."""

    @property
    def is_exhausted(self) -> bool:
        """Whether no further pages remain after this one."""
        return self.next_page_token is None
