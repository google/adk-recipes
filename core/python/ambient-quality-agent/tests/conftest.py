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

"""Shared pytest configuration, fakes, and context stand-ins."""

from __future__ import annotations

import asyncio
import datetime as dt
import os
import pathlib
import types
from collections.abc import Iterator, Sequence
from types import SimpleNamespace
from typing import Any

# Prevent local `.env` settings from polluting `os.environ` during test runs
# when `ambient_quality_agent` calls `load_dotenv()`.
os.environ["PYTHON_DOTENV_DISABLED"] = "1"

os.environ.setdefault("AQA_OBSERVED_AGENT_NAME", "test-observed-agent")
os.environ.setdefault("GOOGLE_CLOUD_PROJECT", "test-project")
os.environ.setdefault("GOOGLE_CLOUD_LOCATION", "us-central1")

# Imported after env defaults so module-level config construction succeeds.
import google.auth
import pytest
import vertexai
from agentplatform._genai.types import (
    EvalCase,
    EvaluationDataset,
    EvaluationResult,
)
from agentplatform._genai.types.evals import (
    AgentData,
    AgentEvent,
    ConversationTurn,
)
from ambient_quality_agent.config import Config
from ambient_quality_agent.standalone.files import FileObjectStore
from ambient_quality_agent.tools.ingestion.models import IngestionCounts, Page
from ambient_quality_agent.tools.insights.models import ProposedEdit, RootCause
from ambient_quality_agent.tools.investigations.bigquery_store import (
    INVESTIGATIONS_TABLE,
    BigQueryInvestigationStore,
)
from ambient_quality_agent.tools.investigations.models import (
    IN_FLIGHT_STATUSES,
    NON_AMBIENT_TRIGGER_TYPES,
    InvestigationCounters,
    InvestigationEvent,
    InvestigationRecord,
    RunStatus,
)
from ambient_quality_agent.tools.investigations.store import DEFAULT_LIST_LIMIT
from google.auth.credentials import AnonymousCredentials, Credentials
from google.genai import types as gt

REAL_GOOGLE_AUTH_DEFAULT = google.auth.default
"""The unpatched `google.auth.default`, for opt-in tests against live services."""

_credentials_patch = pytest.MonkeyPatch()


def _build_anonymous_default_credentials(
    scopes: Sequence[str] | None = None,
    request: Any = None,
    quota_project_id: str | None = None,
    default_scopes: Sequence[str] | None = None,
) -> tuple[Credentials, str | None]:
    """Stands in for `google.auth.default` so no test reads real credentials.

    Importing `fast_api_app` builds the ADK app, which calls
    `google.auth.default()` for the project; CI has no Application Default
    Credentials. Tests that exercise credential lookup patch it themselves.

    Args:
        scopes: Ignored; anonymous credentials carry no scopes.
        request: Ignored; nothing is fetched.
        quota_project_id: Ignored; anonymous credentials bill no project.
        default_scopes: Ignored; anonymous credentials carry no scopes.

    Returns:
        Anonymous credentials and the test project ID, if set.
    """
    return AnonymousCredentials(), os.environ.get("GOOGLE_CLOUD_PROJECT")


def pytest_configure(config: pytest.Config) -> None:
    """Keeps credential lookup off the network before collection imports tests.

    Test modules import `fast_api_app` during collection, before any fixture
    runs, so the stub is installed here.

    Args:
        config: The pytest configuration.
    """
    del config
    _credentials_patch.setattr(
        google.auth, "default", _build_anonymous_default_credentials
    )
    # With only GOOGLE_CLOUD_PROJECT set, Vertex AI looks the project up in
    # Resource Manager; an explicit project skips that call.
    vertexai.init(project=os.environ["GOOGLE_CLOUD_PROJECT"])


def pytest_unconfigure(config: pytest.Config) -> None:
    """Restores the real `google.auth.default`.

    Args:
        config: The pytest configuration.
    """
    del config
    _credentials_patch.undo()


# Canned clustering response; a test that cares sets its own.
FAKE_CLUSTER_RESPONSE = '{"clusters": []}'

# Canned cluster-verification verdict, confirming the candidate as it stands.
FAKE_VERIFY_RESPONSE = (
    '{"valid": true, "proposed_label": "", '
    '"description": "The agent did not call the tool the prompt requires."}'
)

# Default canned session-review JSON response for tests.
FAKE_REVIEW_RESPONSE = '{"findings": []}'


# --------------------------------------------------------------------------- #
# Telemetry fakes (fetcher + evaluator) and node-factory wiring               #
# --------------------------------------------------------------------------- #


class FakeFetcher:
    """Stand-in for a telemetry fetcher.

    Yields the given `pages` in order (recording the page tokens it was asked
    for); once exhausted it returns an empty terminal page. `counts` stands in
    for what a real fetcher tallies while mapping rows, so a test can hand the
    node ingestion figures without any rows to map; `count_scanned` reports the
    scanned figure it was seeded with.

    Unseeded, that figure is the number of cases across `pages`. A window this
    serves trajectories from is not an empty one, and the sweep reads a scanned
    count of zero as empty -- so a fake answering zero while handing over pages
    would hang a misconfiguration warning on a healthy run.
    """

    def __init__(
        self,
        pages: list[Page] | None = None,
        counts: IngestionCounts | None = None,
    ) -> None:
        self._pages = list(pages or [])
        self.page_tokens_seen: list[str | None] = []
        self.counts = counts or IngestionCounts(
            scanned=sum(len(p.dataset.eval_cases) for p in self._pages)
        )
        self.scanned_calls = 0
        self.has_selector = False

    def count_scanned(self, *_: Any, **__: Any) -> int:
        self.scanned_calls += 1
        return self.counts.scanned

    def submit_query(self, **_: Any) -> str:
        return "job-id"

    def fetch_page(self, page_token: str | None = None, **_: Any) -> Page:
        self.page_tokens_seen.append(page_token)
        if not self._pages:
            return Page(
                dataset=EvaluationDataset(eval_cases=[]), next_page_token=None
            )
        return self._pages.pop(0)


class FakeEvaluator:
    """Stand-in for the evaluator.

    Returns the queued `results` from `evaluate` (empty `EvaluationResult`
    once exhausted).
    """

    def __init__(self, results: list[EvaluationResult] | None = None) -> None:
        self._results = list(results or [])
        self.calls: list[tuple[Any, Any]] = []
        """The ``(dataset, metrics)`` of each call, in order.

        Recorded because a remote code metric is only remote if its source
        actually reached the service: asserting on the findings alone would
        pass just as well if it had been run in this process.
        """

    def evaluate(
        self, dataset: Any = None, metrics: Any = None
    ) -> EvaluationResult:
        self.calls.append((dataset, metrics))
        if not self._results:
            return EvaluationResult()
        return self._results.pop(0)


class FakeReviewCall:
    """Test fake for session-review LLM calls.

    Returns canned JSON and records prompts for assertion verification.
    """

    def __init__(self, response: str = FAKE_REVIEW_RESPONSE) -> None:
        self.response = response
        self.prompts: list[str] = []

    def __call__(self, prompt: str) -> str:
        self.prompts.append(prompt)
        return self.response


class FakeInsightModelCall:
    """Stand-in for the insights clustering LLM call.

    Returns canned JSON and records each prompt, so a test can assert both on
    the resulting clusters and on what the node asked.
    """

    def __init__(self, response: str = FAKE_CLUSTER_RESPONSE) -> None:
        self.response = response
        self.prompts: list[str] = []

    def __call__(self, prompt: str) -> str:
        self.prompts.append(prompt)
        return self.response


class FakeInsightStore:
    """Stand-in for `InsightStore`, recording calls in memory without touching BigQuery.

    `matches` seeds `find_existing_insights` (a ``{candidate index: insight id}``
    map, absent keys meaning no match) and `candidate_labels` records which
    clusters were put to it, which is how a test tells a cluster withheld from
    matching from one that matched nothing. `calls` records method names in
    order so a test can assert `delete_failed_insights` ran first. The `save_*`,
    `touch_*` and `resolve_*` arguments are captured verbatim for assertions.
    """

    def __init__(self, matches: dict[int, str] | None = None) -> None:
        self.matches = matches or {}
        self.calls: list[str] = []
        self.cleanup_run_ids: list[str] = []
        self.candidate_labels: list[str] = []
        self.saved: list[tuple[list[Any], list[str], list[Any]]] = []
        self.seen: list[str] = []
        self.relabelled: dict[str, str] = {}
        self.resolve_calls: list[tuple[Any, int]] = []

    def delete_failed_insights(self, run_id: str) -> None:
        self.calls.append("cleanup")
        self.cleanup_run_ids.append(run_id)

    def find_existing_insights(self, candidates: Any) -> dict[int, str | None]:
        self.calls.append("find")
        self.candidate_labels = [c.label for c in candidates]
        return {
            i: self.matches[i]
            for i in range(len(candidates))
            if i in self.matches
        }

    def save_investigation_result(
        self,
        new_insights: Any,
        seen_insight_ids: Any,
        recurring_insight_ids: Any,
        occurrences: Any,
        now: Any,
        *,
        relabelled: Any = None,
    ) -> None:
        self.calls.append("save")
        self.relabelled = dict(relabelled or {})
        self.saved.append(
            (list(new_insights), list(recurring_insight_ids), list(occurrences))
        )
        self.seen.extend(seen_insight_ids)
        del now  # Recorded on the insight row, not asserted through this fake.

    def resolve_stale_insights(self, now: Any, window_days: int) -> None:
        self.calls.append("resolve")
        self.resolve_calls.append((now, window_days))


class FakeTrajectoryRecorder:
    """Stand-in for `TrajectoryRecorder`: the seam ingestion writes through.

    Buffers like the real one so a test can tell a page that was flushed from
    one that was not, and keeps `records` flat across pages, which is how the
    ingestion invariants are asserted -- the rows a whole run wrote against the
    counters that run reported.
    """

    def __init__(self) -> None:
        self.cleanup_calls = 0
        self.records: list[Any] = []
        self._pending: list[Any] = []

    def add(
        self,
        *,
        trajectory_id: str,
        session_id: str | None,
        trace_ids: Any,
        status: Any,
        case: Any = None,
        agent_revision: str = "",
    ) -> None:
        self._pending.append(
            SimpleNamespace(
                trajectory_id=trajectory_id,
                session_id=session_id,
                trace_ids=tuple(trace_ids),
                ingest_status=status,
                case=case,
                agent_revision=agent_revision,
            )
        )

    def flush(self) -> None:
        self.records.extend(self._pending)
        self._pending.clear()

    def delete_run_trajectories(self) -> None:
        self.cleanup_calls += 1


# --------------------------------------------------------------------------- #
# Root-cause fakes and test fixtures                                           #
# --------------------------------------------------------------------------- #


class InMemoryRootCauseStore:
    """In-memory `RootCauseWriter` appending to a list for test assertions."""

    def __init__(self) -> None:
        self.saved: list[RootCause] = []

    def save(self, record: RootCause) -> None:
        self.saved.append(record)


@pytest.fixture
def root_cause_store() -> InMemoryRootCauseStore:
    """Provides an in-memory root-cause store for test assertions."""
    return InMemoryRootCauseStore()


@pytest.fixture
def golden_root_cause() -> RootCause:
    """Provides a realistic sample `RootCause` fixture for testing.

    Populates `before` code snippets mirroring production writer output to
    validate stores and renderers.
    """
    return RootCause(
        root_cause_id="4f2c9a1b7e5d4c8f9a0b1c2d3e4f5a6b",
        insight_id="ins-travel-desk-1",
        occurrence_id="occ-travel-desk-1",
        agent_revision="travel-desk-00042-abc",
        summary=(
            "The agent invents an expense category because the tool docstring "
            "lists no allowed values and the parameter is a free-form string, "
            "so `file_expense` accepts whatever the model writes."
        ),
        edits=[
            ProposedEdit(
                path="app/agent.py",
                start_line=48,
                end_line=52,
                before=(
                    "def file_expense(amount: float, category: str) -> dict:\n"
                    '    """File a travel expense."""\n'
                    "    return _expenses_api.create(\n"
                    "        amount=amount, category=category\n"
                    "    )\n"
                ),
                after=(
                    "def file_expense(amount: float, category: ExpenseCategory) -> dict:\n"
                    '    """File a travel expense under one of the allowed categories."""\n'
                    "    return _expenses_api.create(\n"
                    "        amount=amount, category=category.value\n"
                    "    )\n"
                ),
                rationale=(
                    "An enum makes an unknown category a validation error rather "
                    "than a filed expense."
                ),
            ),
            ProposedEdit(
                path="app/agent.py",
                start_line=15,
                end_line=15,
                before="from app.expenses import ExpensesApi\n",
                after=(
                    "from app.expenses import ExpenseCategory, ExpensesApi\n"
                ),
                rationale="Import the enum the tool signature now takes.",
            ),
        ],
        created_at=dt.datetime(2026, 6, 1, 12, 30, tzinfo=dt.UTC),
    )


# --------------------------------------------------------------------------- #
# Investigation registry fake                                                  #
# --------------------------------------------------------------------------- #


class FakeInvestigationStore(BigQueryInvestigationStore):
    """`BigQueryInvestigationStore` backed by in-memory row lists.

    Only the BigQuery seams are replaced, so the row shapes, the JSON encoding
    and the model validators are all production code: a test that writes a run
    and reads it back exercises the real snapshot round trip.
    """

    def __init__(self) -> None:
        super().__init__(
            client=None, project_id="test-project", dataset="aqua_insights"
        )
        self.rows: list[dict[str, Any]] = []
        self.event_rows: list[dict[str, Any]] = []

    def _insert_rows(self, table: str, rows: list[dict[str, Any]]) -> None:
        target = self.rows if table == INVESTIGATIONS_TABLE else self.event_rows
        target.extend(rows)

    def get(self, run_id: str, *, include_events: bool = True):
        snapshots = [row for row in self.rows if row["run_id"] == run_id]
        if not snapshots:
            return None
        # `max` keeps the first of equal keys, so scan in reverse: an in-memory
        # store can write two snapshots inside one clock tick, and the later
        # write is still the newer one.
        newest = max(reversed(snapshots), key=lambda row: row["updated_at"])
        record = InvestigationRecord.model_validate(newest)
        if include_events:
            record = record.model_copy(
                update={"events": self._list_events(run_id)}
            )
        return record

    def _list_events(self, run_id: str) -> list[InvestigationEvent]:
        return [
            InvestigationEvent.model_validate(row)
            for row in sorted(self.event_rows, key=lambda r: r["created_at"])
            if row["run_id"] == run_id
        ]

    def list_recent(
        self,
        *,
        status=None,
        limit: int = DEFAULT_LIST_LIMIT,
        window_start: str | None = None,
        window_end: str | None = None,
    ):
        run_ids = {row["run_id"] for row in self.rows}
        found = [self.get(run_id, include_events=False) for run_id in run_ids]
        records = [r for r in found if r is not None]
        if status is not None:
            records = [r for r in records if r.status == status]
        # Windowed on `window_end` like the query, and *before* the limit, so a
        # caller exercising a period sees the same "most recent page of the
        # period" the real store returns, not a page of everything.
        records = [
            r
            for r in records
            if _in_window(r.window_end, window_start, window_end)
        ]
        records.sort(key=lambda r: r.created_at)
        return records[-limit:]

    def sum_counters_by_day(
        self, *, window_start: str | None = None, window_end: str | None = None
    ) -> list[dict[str, int | str]]:
        """Per-day sums over the rows here, as the real store does in SQL.

        Keyed on the UTC date of ``window_end``; runs without one are absent,
        as are days nothing ran -- a caller drawing a fixed range zero-fills the
        gaps itself, and cannot otherwise tell "quiet" from "down".

        Args:
            window_start: Optional ISO timestamp lower bound for the window.
            window_end: Optional ISO timestamp upper bound for the window.

        Returns:
            List of per-day counter dictionaries sorted by date.
        """
        buckets: dict[str, dict[str, int | str]] = {}
        run_ids = {row["run_id"] for row in self.rows}
        for record in (self.get(r, include_events=False) for r in run_ids):
            if record is None or not record.window_end:
                continue
            if not _in_window(record.window_end, window_start, window_end):
                continue
            day = record.window_end[:10]
            bucket = buckets.setdefault(
                day, {"day": day, "investigations": 0, "unmeasured": 0}
            )
            bucket["investigations"] = int(bucket["investigations"]) + 1
            counters = record.counters.model_dump()
            if not sum(int(v or 0) for v in counters.values()):
                bucket["unmeasured"] = int(bucket["unmeasured"]) + 1
            for name, value in counters.items():
                bucket[name] = int(bucket.get(name, 0)) + int(value or 0)
        return [buckets[day] for day in sorted(buckets)]

    def sum_counters(
        self, *, window_start: str | None = None, window_end: str | None = None
    ) -> tuple[dict[str, int], dict[str, str | None]]:
        """The same aggregation the real store does in SQL, over the rows here.

        Scaled to an aggregation window if requested.

        Args:
            window_start: Optional ISO timestamp lower bound.
            window_end: Optional ISO timestamp upper bound.

        Returns:
            Tuple of (aggregated counter totals dict, window coverage dict).
        """
        run_ids = {row["run_id"] for row in self.rows}
        records = [self.get(run_id, include_events=False) for run_id in run_ids]
        kept = [
            record
            for record in records
            if record is not None
            and _in_window(record.window_end, window_start, window_end)
        ]
        totals = dict.fromkeys(InvestigationCounters.model_fields, 0)
        for record in kept:
            for name, value in record.counters.model_dump().items():
                totals[name] += value
        windows = sorted(r.window_end for r in kept if r.window_end)
        covered = {
            "start": windows[0] if windows else None,
            "end": windows[-1] if windows else None,
        }
        return {"investigations": len(kept), **totals}, covered

    def list_stale_pending(self, *, lease_minutes: int) -> list[str]:
        """The same pick the SQL makes: newest snapshot, pending, past the lease.

        Args:
            lease_minutes: Expiration threshold in minutes for pending runs.

        Returns:
            List of stale run IDs.
        """
        deadline = dt.datetime.now(tz=dt.UTC) - dt.timedelta(
            minutes=lease_minutes
        )
        run_ids = {row["run_id"] for row in self.rows}
        records = [self.get(run_id, include_events=False) for run_id in run_ids]
        return [
            record.run_id
            for record in records
            if record is not None
            and record.status == RunStatus.PENDING
            and _parse_iso(record.updated_at) < deadline
        ]

    def count_in_flight(self, observed_agent_name: str) -> int:
        """Count active, non-terminal runs for an observed agent using latest snapshots.

        Args:
            observed_agent_name: Name of the observed agent to query.

        Returns:
            Number of in-flight runs.
        """
        run_ids = {row["run_id"] for row in self.rows}
        records = [self.get(run_id, include_events=False) for run_id in run_ids]
        return len(
            [
                record
                for record in records
                if record is not None
                and record.observed_agent_name == observed_agent_name
                and record.status in IN_FLIGHT_STATUSES
            ]
        )

    def list_overdue_scheduled(self, *, grace_minutes: int) -> list[str]:
        """The same pick the SQL makes: newest snapshot, scheduled, past due.

        Args:
            grace_minutes: Minutes past `due_at` before a scheduled run is lost.

        Returns:
            List of overdue scheduled run IDs.
        """
        deadline = dt.datetime.now(tz=dt.UTC) - dt.timedelta(
            minutes=grace_minutes
        )
        run_ids = {row["run_id"] for row in self.rows}
        records = [self.get(run_id, include_events=False) for run_id in run_ids]
        return [
            record.run_id
            for record in records
            if record is not None
            and record.status == RunStatus.SCHEDULED
            and record.due_at is not None
            and _parse_iso(record.due_at) < deadline
        ]

    def get_last_finished_window_end(
        self, observed_agent_name: str
    ) -> str | None:
        ends = [
            record.window_end
            for record in self.list_recent(
                status=RunStatus.DONE, limit=len(self.rows)
            )
            if record.observed_agent_name == observed_agent_name
            and record.trigger_type not in NON_AMBIENT_TRIGGER_TYPES
            and not record.dry_run
            and record.window_end
        ]
        # The writer stamps UTC, so the stored strings sort as the timestamps do.
        return max(ends) if ends else None


def _parse_iso(stamp: str) -> dt.datetime:
    when = dt.datetime.fromisoformat(stamp)
    return when if when.tzinfo is not None else when.replace(tzinfo=dt.UTC)


def _in_window(stamp: str | None, start: str | None, end: str | None) -> bool:
    """Whether ``stamp`` falls in ``[start, end]``, either bound optional.

    A missing stamp is outside any bounded period and inside an unbounded one,
    which is how the SQL behaves: a NULL fails every comparison.

    Args:
        stamp: Timestamp string to check.
        start: Optional start of window.
        end: Optional end of window.

    Returns:
        True if stamp falls within window, False otherwise.
    """
    if not start and not end:
        return True
    if not stamp:
        return False
    when = _parse_iso(stamp)
    if start and when < _parse_iso(start):
        return False
    return not (end and when > _parse_iso(end))


def create_run(state: Any, record: Any) -> Any:
    """`model.create_run`, driven to completion for a synchronous test.

    The registry is async because its BigQuery reads are offloaded from the event
    loop; calling this helper lets synchronous tests seed runs without running
    as coroutines.

    Args:
        state: State dictionary or context.
        record: Investigation record to create.

    Returns:
        The created run record.
    """
    return asyncio.run(run_model().create_run(state, record))


def get_run(state: Any, run_id: str) -> Any:
    """`model.get_run`, driven to completion for a synchronous test.

    Args:
        state: State dictionary or context.
        run_id: Investigation run identifier.

    Returns:
        The fetched run record, or None if not found.
    """
    return asyncio.run(run_model().get_run(state, run_id))


def run_model() -> Any:
    """The run registry module, imported late (it builds config on import).

    Returns:
        The `ambient_quality_agent.core.investigation.model` module.
    """
    from ambient_quality_agent.core.investigation import model

    return model


@pytest.fixture(scope="session", autouse=True)
def storage_backend() -> None:
    """Install the deployed backend once, as an entry point does.

    The storage providers hold an unbound provider until a backend is bound, so without
    this every test that reaches a store raises. The deployed backend is the
    baseline these tests assume; `backends.override_providers(standalone=True)` is how a
    test asks for the other.

    Session-scoped, so it is a floor rather than a competitor. The per-test
    fixtures below swap individual providers and restore what they found. Two
    autouse fixtures writing the same attributes would let fixture ordering
    decide which one landed last.
    """
    from ambient_quality_agent import backends

    backends.bind_providers(backends.build_deployed_bindings())


@pytest.fixture(autouse=True)
def investigation_store() -> Iterator[FakeInvestigationStore]:
    """Point the run registry and the node event writer at one in-memory store.

    Autouse so no test can reach BigQuery through the registry by omission, and
    shared between the two seams so a run scheduled through the tools and the
    events its nodes emit land in the same place -- as they do in a deployment.
    """
    from ambient_quality_agent.core import state as state_module
    from ambient_quality_agent.core.investigation import model as run_model

    store = FakeInvestigationStore()
    original_store_factory = run_model.store_factory
    original_event_writer = state_module.event_writer
    run_model.store_factory = lambda state: store
    state_module.event_writer = lambda state, text, source: store.append_event(
        state["run_id"], text, source=source
    )
    try:
        yield store
    finally:
        run_model.store_factory = original_store_factory
        state_module.event_writer = original_event_writer


@pytest.fixture(autouse=True)
def jobs_store(tmp_path: pathlib.Path) -> Iterator[FileObjectStore]:
    """Point the jobs store at an empty directory of this test's own.

    Autouse so no test reaches the jobs bucket by omission. The deployed binding
    reads the bucket from the environment, which on a developer's machine may
    name a real one.
    """
    from ambient_quality_agent.tools.objects import store as objects

    store = FileObjectStore(tmp_path / "jobs")
    original = objects.jobs_store_factory
    objects.jobs_store_factory = lambda: store
    try:
        yield store
    finally:
        objects.jobs_store_factory = original


@pytest.fixture(autouse=True)
def _no_cached_agent_config() -> Iterator[None]:
    """Start and finish each test with an empty agent-config cache.

    The cache is module state with a time-based expiry, so without this a record
    read in one test answers the next one -- and which tests share a process is
    not something any of them state.
    """
    from ambient_quality_agent.tools.observed_agent_config import (
        effective_config,
    )

    effective_config.clear_cache()
    try:
        yield
    finally:
        effective_config.clear_cache()


@pytest.fixture
def patched_factories() -> Iterator[dict[str, Any]]:
    """Swap node seams with test fakes.

    Yields a dictionary of fake implementations (fetcher, evaluator, reviewer,
    model_call, verification_call, insight_store, trajectory_recorder, goal) to
    avoid network calls during graph execution.
    """
    from ambient_quality_agent.core.nodes import _common
    from ambient_quality_agent.tools.documents.goal import InvestigationGoal

    one_page = Page(
        dataset=EvaluationDataset(eval_cases=[EvalCase()]), next_page_token=None
    )
    holder: dict[str, Any] = {
        "fetcher": FakeFetcher(pages=[one_page, one_page]),
        "evaluator": FakeEvaluator(),
        "reviewer": FakeReviewCall(),
        "model_call": FakeInsightModelCall(),
        "verification_call": FakeInsightModelCall(FAKE_VERIFY_RESPONSE),
        "insight_store": FakeInsightStore(),
        "trajectory_recorder": FakeTrajectoryRecorder(),
        "goal": InvestigationGoal(),
    }
    original_fetcher = _common.fetcher_factory
    original_evaluator = _common.evaluator_factory
    original_reviewer = _common.reviewer_factory
    original_model_call_factory = _common.model_call_factory
    original_verification_call_factory = _common.verification_call_factory
    original_insight_store_factory = _common.insight_store_factory
    original_trajectory_recorder_factory = _common.trajectory_recorder_factory
    original_goal_loader = _common.goal_loader
    _common.fetcher_factory = lambda ctx: holder["fetcher"]
    _common.evaluator_factory = lambda ctx: holder["evaluator"]
    _common.reviewer_factory = lambda ctx: holder["reviewer"]
    _common.model_call_factory = lambda ctx: holder["model_call"]
    _common.verification_call_factory = lambda ctx: holder["verification_call"]
    _common.insight_store_factory = lambda ctx: holder["insight_store"]
    _common.trajectory_recorder_factory = lambda ctx: holder[
        "trajectory_recorder"
    ]
    _common.goal_loader = lambda ctx: holder["goal"]
    try:
        yield holder
    finally:
        _common.fetcher_factory = original_fetcher
        _common.evaluator_factory = original_evaluator
        _common.reviewer_factory = original_reviewer
        _common.model_call_factory = original_model_call_factory
        _common.verification_call_factory = original_verification_call_factory
        _common.insight_store_factory = original_insight_store_factory
        _common.trajectory_recorder_factory = (
            original_trajectory_recorder_factory
        )
        _common.goal_loader = original_goal_loader


# --------------------------------------------------------------------------- #
# GCS fake                                                                     #
# --------------------------------------------------------------------------- #


class FakeGcsClient:
    """In-memory `storage.Client` stand-in: contents, listing, and
    `if_generation_match=0`.

    Holds what was written, so a caller that reads its own writes
    (`objects.gcs_store.GcsObjectStore`) can be exercised end to end. Tracks claimed
    ``(bucket, object_name)`` keys separately; a create with
    ``if_generation_match=0`` for an already-claimed key raises
    `PreconditionFailed`, mirroring GCS's create-if-absent precondition, which
    lets `tools.gcs.claim_once` be exercised for real (including its 412 path)
    without network. Patch `gcs.client` to return an instance, or pass one
    through a `client_factory`.
    """

    def __init__(
        self, contents: dict[tuple[str, str], bytes] | None = None
    ) -> None:
        self.contents: dict[tuple[str, str], bytes] = dict(contents or {})
        self.objects: set[tuple[str, str]] = set(self.contents)
        self.uploads: list[tuple[tuple[str, str], Any, int | None]] = []
        self.deletes: list[tuple[str, str]] = []
        self.downloads: list[tuple[str, str]] = []
        self.content_types: dict[tuple[str, str], str | None] = {}

    def bucket(self, name: str) -> _FakeGcsBucket:
        return _FakeGcsBucket(self, name)

    def list_blobs(
        self, bucket: str, prefix: str = "", delimiter: str | None = None
    ) -> _FakeGcsListing:
        """List as GCS does: with a delimiter, deeper names become `prefixes`.

        Args:
            bucket: Bucket to list.
            prefix: Leading part of the names to list.
            delimiter: Folds every name with this after the prefix into one
                entry of `prefixes`.

        Returns:
            An iterable of the blobs, whose `prefixes` is set once iterated.
        """
        blobs: list[_FakeGcsBlob] = []
        prefixes: set[str] = set()
        for key in sorted(self.contents):
            if key[0] != bucket or not key[1].startswith(prefix):
                continue
            rest = key[1][len(prefix) :]
            if delimiter and delimiter in rest:
                prefixes.add(prefix + rest.split(delimiter, 1)[0] + delimiter)
            else:
                blobs.append(_FakeGcsBlob(self, key))
        return _FakeGcsListing(blobs, prefixes)


class _FakeGcsListing(list):
    """The blobs of a listing; `prefixes` fills only once it is iterated, as in GCS."""

    def __init__(self, blobs: list[_FakeGcsBlob], prefixes: set[str]) -> None:
        super().__init__(blobs)
        self._pending = prefixes
        self.prefixes: set[str] = set()

    def __iter__(self) -> Iterator[_FakeGcsBlob]:
        self.prefixes = set(self._pending)
        return super().__iter__()


class _FakeGcsBucket:
    def __init__(self, client: FakeGcsClient, name: str) -> None:
        self._client = client
        self._name = name

    def blob(self, name: str) -> _FakeGcsBlob:
        return _FakeGcsBlob(self._client, (self._name, name))


class _FakeGcsBlob:
    def __init__(self, client: FakeGcsClient, key: tuple[str, str]) -> None:
        self._client = client
        self._key = key

    @property
    def name(self) -> str:
        return self._key[1]

    def upload_from_string(
        self,
        data: Any,
        if_generation_match: int | None = None,
        content_type: str | None = None,
    ) -> None:
        from google.api_core import exceptions as api_exceptions

        self._client.uploads.append((self._key, data, if_generation_match))
        if if_generation_match == 0 and self._key in self._client.objects:
            raise api_exceptions.PreconditionFailed("object exists")
        self._client.objects.add(self._key)
        self._client.content_types[self._key] = content_type
        self._client.contents[self._key] = (
            data.encode("utf-8") if isinstance(data, str) else data
        )

    def download_as_text(self) -> str:
        return self.download_as_bytes().decode("utf-8")

    def download_as_bytes(self) -> bytes:
        """Read, raising `NotFound` on an absent object (like GCS).

        Returns:
            The stored blob contents as bytes.

        Raises:
            NotFound: If the object does not exist.
        """
        from google.api_core import exceptions as api_exceptions

        self._client.downloads.append(self._key)
        if self._key not in self._client.contents:
            raise api_exceptions.NotFound("object missing")
        return self._client.contents[self._key]

    def delete(self) -> None:
        """Delete the marker, raising `NotFound` on an absent object (like GCS).

        Raises:
            NotFound: If the object does not exist.
        """
        from google.api_core import exceptions as api_exceptions

        self._client.deletes.append(self._key)
        if self._key not in self._client.objects:
            raise api_exceptions.NotFound("object missing")
        self._client.objects.discard(self._key)
        self._client.contents.pop(self._key, None)


# --------------------------------------------------------------------------- #
# Context stand-ins                                                            #
# --------------------------------------------------------------------------- #


class StateContext:
    """Minimal context exposing a mutable `.state` (Callback/ToolContext)."""

    def __init__(self, state: dict[str, Any] | None = None) -> None:
        self.state: dict[str, Any] = state if state is not None else {}


class ToolContext(StateContext):
    """ToolContext stand-in: `.state` plus a public `.session`.

    Mirrors the unified ADK `Context` (both `ToolContext` and `CallbackContext`
    resolve to `Context` in this ADK version), which exposes the invocation's
    session via the public `.session` property that `submit_investigation`
    reads.
    """

    def __init__(
        self,
        state: dict[str, Any] | None = None,
        *,
        app_name: str = "eng",
        user_id: str = "u",
        session_id: str = "s",
    ) -> None:
        super().__init__(state)
        self.session = types.SimpleNamespace(
            app_name=app_name, user_id=user_id, id=session_id
        )


class CallbackContext(StateContext):
    """CallbackContext stand-in: `.state`, a `.user_content` text message, and
    a public `.session`.

    Also resolves to the unified ADK `Context`, so like `ToolContext` it exposes
    `.session`; the ambient handler (Phase 2) schedules from a `CallbackContext`,
    and `submit_investigation` reads `.session` off it.
    """

    def __init__(
        self,
        text: str = "",
        state: dict[str, Any] | None = None,
        *,
        app_name: str = "eng",
        user_id: str = "u",
        session_id: str = "s",
    ) -> None:
        super().__init__(state)
        self.user_content = types.SimpleNamespace(
            parts=[types.SimpleNamespace(text=text)]
        )
        self.session = types.SimpleNamespace(
            app_name=app_name, user_id=user_id, id=session_id
        )


# --------------------------------------------------------------------------- #
# Config helper                                                                #
# --------------------------------------------------------------------------- #


def make_config(**overrides: Any) -> Config:
    """Build a `Config` with sensible test defaults, overridable per-field.

    Args:
        **overrides: Field values overriding default test configuration.

    Returns:
        A configured Config instance.
    """
    fields: dict[str, Any] = {
        "observed_agent_name": "test-agent",
        "project_id": "test-project",
        "location": "us-central1",
        "multi_turn_metrics": ["task_success"],
        "single_turn_metrics": ["safety"],
        "data_evaluation_cap": 100,
        "data_lookback_window": 7,
    }
    fields.update(overrides)
    return Config(**fields)


def make_agent_case(
    case_id: str = "s1", text: str | None = "Hello."
) -> EvalCase:
    """An eval case shaped the way the fetchers actually build one.

    ``agent_data`` only, with no response candidate and no prompt -- which is
    what makes the code-metric runner derive the response from the conversation.
    ``text`` of ``None`` is a turn the agent took without saying anything.

    Args:
        case_id: Evaluation case identifier.
        text: Response text for the agent turn, or None for an empty turn.

    Returns:
        An EvalCase instance with agent conversation data.
    """
    parts = [] if text is None else [gt.Part(text=text)]
    return EvalCase(
        eval_case_id=case_id,
        agent_data=AgentData(
            agents={},
            turns=[
                ConversationTurn(
                    turn_index=0,
                    turn_id="t0",
                    events=[
                        AgentEvent(
                            author="agent",
                            content=gt.Content(role="model", parts=parts),
                        )
                    ],
                )
            ],
        ),
    )


def make_page(cases: list[EvalCase], **kwargs: Any) -> Page:
    """One page of eval cases, exhausted unless a token is passed.

    Args:
        cases: Evaluation cases for this page.
        **kwargs: Additional keyword arguments for Page construction.

    Returns:
        A Page instance.
    """
    kwargs.setdefault("next_page_token", None)
    return Page(dataset=EvaluationDataset(eval_cases=cases), **kwargs)


def make_code_metric(
    outcome: Any, name: str = "m", expected: str = "The agent answers."
):
    """A `CodeMetric` returning ``outcome``, or raising it if it is an exception.

    Args:
        outcome: Return value or exception to raise during evaluation.
        name: Name of the code metric.
        expected: Expected behavior description.

    Returns:
        A CodeMetric instance.
    """
    from ambient_quality_agent.tools.metrics.library import CodeMetric

    def evaluate(instance: dict[str, Any]) -> Any:
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

    return CodeMetric(name=name, expected=expected, evaluate=evaluate)
