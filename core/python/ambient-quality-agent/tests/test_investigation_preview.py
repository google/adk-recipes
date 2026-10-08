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

"""Tests for selector preview execution (`ambient_quality_agent.core.investigation.preview`).

Verifies selector validation, preview counting, payload archiving without index
records, lock avoidance, and thread offloading.
"""

from __future__ import annotations

import asyncio
import dataclasses
import threading
from collections.abc import Callable
from typing import Any
from unittest import mock

import pytest
from agentplatform._genai.types import EvalCase, EvaluationDataset
from agentplatform._genai.types.evals import (
    AgentData,
    AgentEvent,
    ConversationTurn,
)
from ambient_quality_agent.core.investigation import locking, preview
from ambient_quality_agent.tools.ingestion import selector
from ambient_quality_agent.tools.ingestion.models import Page
from ambient_quality_agent.tools.trajectories.models import IngestStatus
from google.genai import types as gt

from .conftest import StateContext, make_config

_AGENT = "test-agent"
_PROJECT = "test-project"
_SELECTOR = """
SELECT session_id AS target_id
FROM `t`
WHERE timestamp BETWEEN @window_start AND @window_end
  AND agent = @agent_name
"""
_START = "2026-01-01T00:00:00+00:00"
_END = "2026-01-08T00:00:00+00:00"


def _case(
    case_id: str, *, text: str = "Book me a flight to Zurich."
) -> EvalCase:
    """Builds a test EvalCase with a single user turn.

    Args:
        case_id: Evaluated case identifier.
        text: Prompt text for the user turn.

    Returns:
        Constructed EvalCase instance.
    """
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
                            author="user",
                            content=gt.Content(
                                role="user", parts=[gt.Part(text=text)]
                            ),
                        )
                    ],
                )
            ],
        ),
    )


class FakePreviewFetcher:
    """Test fake for BaseFetcher tracking method calls and returning configured cases."""

    def __init__(
        self,
        *,
        cases: list[EvalCase] | None = None,
        matched: int = 0,
        precheck: selector.PrecheckResult | None = None,
        trace_ids: tuple[str, ...] = ("trace-1",),
        status: IngestStatus = IngestStatus.INGESTED,
    ) -> None:
        self._cases = list(cases or [])
        self._matched = matched
        self._precheck = precheck or selector.PrecheckResult()
        self._trace_ids = trace_ids
        self._status = status
        self.recorder: Any = None
        self.calls: list[str] = []
        self.limits: list[int] = []
        # Callbacks allowing tests to simulate custom fetcher behaviors, such as
        # dropped rows or deadline timeouts, without patching bound methods.
        self.on_count_scanned: Callable[[], int] | None = None
        self.on_fetch_page: Callable[[], Page] | None = None

    def precheck_selector(
        self, *_: Any, **kwargs: Any
    ) -> selector.PrecheckResult:
        self.calls.append("precheck")
        self.limits.append(kwargs.get("limit", 0))
        return self._precheck

    def count_scanned(self, *_: Any, **__: Any) -> int:
        self.calls.append("count_scanned")
        if self.on_count_scanned is not None:
            return self.on_count_scanned()
        return self._matched

    def submit_query(self, **kwargs: Any) -> str:
        self.calls.append("submit_query")
        self.limits.append(kwargs["limit"])
        return "job-1"

    def fetch_page(self, **_: Any) -> Page:
        self.calls.append("fetch_page")
        if self.on_fetch_page is not None:
            return self.on_fetch_page()
        for case in self._cases:
            self.recorder.add(
                trajectory_id=case.eval_case_id or "",
                session_id=case.eval_case_id,
                trace_ids=self._trace_ids,
                status=self._status,
                case=case,
            )
        # Mirror fetch_page behavior by flushing the recorder at end of page.
        self.recorder.flush()
        return Page(
            dataset=EvaluationDataset(eval_cases=self._cases),
            next_page_token=None,
        )


class FakePayloadStore:
    """Test fake for the payload store, recording archives and index writes."""

    def __init__(self) -> None:
        self.archives: list[Any] = []
        self.index_writes: list[Any] = []
        self.thread_names: list[str] = []

    def record_payloads(self, archives: Any) -> None:
        self.archives.extend(archives)
        self.thread_names.append(threading.current_thread().name)

    def record(self, trajectories: Any) -> None:
        self.index_writes.extend(trajectories)


@dataclasses.dataclass
class Harness:
    """Test fixture bundle containing mock fetcher, store, and context."""

    fetcher: FakePreviewFetcher
    store: FakePayloadStore
    context: StateContext


@pytest.fixture
def harness(monkeypatch: pytest.MonkeyPatch) -> Harness:
    """Fixture providing a configured Harness with default mocks."""
    fetcher = FakePreviewFetcher(cases=[_case("s1")], matched=1)
    store = FakePayloadStore()

    def fetcher_factory(
        config: Any, selector_sql: str, *, recorder: Any
    ) -> Any:
        del config, selector_sql
        fetcher.recorder = recorder
        return fetcher

    monkeypatch.setattr(preview, "fetcher_factory", fetcher_factory)
    monkeypatch.setattr(preview, "store_factory", lambda _config: store)
    return Harness(fetcher=fetcher, store=store, context=StateContext())


def _install(
    harness: Harness, monkeypatch: pytest.MonkeyPatch, **overrides: Any
) -> None:
    """Patch effective_config.load to return test configuration with overrides."""
    config = make_config(
        observed_agent_name=_AGENT, project_id=_PROJECT, **overrides
    )
    monkeypatch.setattr(preview.effective_config, "load", lambda _state: config)


async def _preview(harness: Harness, **kwargs: Any) -> preview.SelectorPreview:
    kwargs.setdefault("selector_sql", _SELECTOR)
    kwargs.setdefault("window_start", _START)
    kwargs.setdefault("window_end", _END)
    return await preview.preview_selector(harness.context, **kwargs)


# --- what the caller gets back --------------------------------------------- #


def test_the_preview_reports_how_many_conversations_matched(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify that the unsampled match count is reported in the preview result."""
    harness.fetcher._matched = 4200
    _install(harness, monkeypatch, data_evaluation_cap=1000)

    result = asyncio.run(_preview(harness))

    assert result.matched == 4200
    assert result.would_review == 1000


def test_a_selection_inside_the_budget_is_reviewed_whole(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify that would_review is capped by matched count when below evaluation cap."""
    harness.fetcher._matched = 12
    _install(harness, monkeypatch, data_evaluation_cap=1000)

    assert asyncio.run(_preview(harness)).would_review == 12


def test_five_is_what_the_preview_asks_for(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify that precheck and sampling queries limit results to EXAMPLE_COUNT."""
    _install(harness, monkeypatch)

    asyncio.run(_preview(harness))

    assert harness.fetcher.limits == [
        preview.EXAMPLE_COUNT,
        preview.EXAMPLE_COUNT,
    ]


def test_each_example_carries_what_makes_it_recognizable(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify trajectory ID, case view path, turn count, and first user message."""
    _install(harness, monkeypatch)

    example = asyncio.run(_preview(harness)).examples[0]

    assert example.trajectory_id == "s1"
    assert (
        example.case_view_path
        == f"/investigations/preview/cases/{example.trajectory_id}"
    )
    assert example.turn_count == 1
    assert example.first_user_message == "Book me a flight to Zurich."


def test_a_long_opening_message_is_cut_rather_than_carried_whole(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify that long opening user messages are truncated with an ellipsis."""
    harness.fetcher._cases = [_case("s1", text="x" * 900)]
    _install(harness, monkeypatch)

    message = asyncio.run(_preview(harness)).examples[0].first_user_message

    assert len(message) <= preview.FIRST_MESSAGE_CHARS + 3
    assert message.endswith("...")


def test_the_payload_says_the_five_are_examples_not_the_sample(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verifies that the preview note warns of resampling when matches exceed example limits."""
    harness.fetcher._cases = [
        _case(f"s{index}") for index in range(preview.EXAMPLE_COUNT)
    ]
    harness.fetcher._matched = 40
    _install(harness, monkeypatch)

    payload = asyncio.run(_preview(harness)).to_payload()

    assert payload["note"] == preview.EXAMPLES_NOTE
    assert "examples of what the selector matched" in payload["note"]


def test_the_payload_drops_the_resampling_caveat_when_nothing_was_left_out(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verifies that the resampling warning is omitted when all matches fit within preview and budget."""
    harness.fetcher._cases = [_case("s1"), _case("s2")]
    harness.fetcher._matched = 2
    _install(harness, monkeypatch)

    payload = asyncio.run(_preview(harness)).to_payload()

    assert payload["matched"] == 2
    assert payload["note"] == preview.WHOLE_MATCH_NOTE


def test_the_payload_keeps_the_caveat_when_the_budget_will_sample_the_match(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verifies that the resampling warning is retained when the evaluation budget is smaller than matches."""
    harness.fetcher._cases = [_case("s1"), _case("s2"), _case("s3")]
    harness.fetcher._matched = 3
    _install(harness, monkeypatch, data_evaluation_cap=2)

    payload = asyncio.run(_preview(harness)).to_payload()

    assert payload["would_review"] == 2
    assert payload["note"] == preview.EXAMPLES_NOTE


def test_the_payload_says_so_when_the_selector_matched_nothing(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verifies that the preview note reflects zero matched conversations."""
    harness.fetcher._cases = []
    harness.fetcher._matched = 0
    _install(harness, monkeypatch)

    payload = asyncio.run(_preview(harness)).to_payload()

    assert payload["examples"] == []
    assert payload["note"] == preview.NO_MATCH_NOTE


def test_a_source_without_otel_ids_yields_no_trace_link(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify that sources without OpenTelemetry trace IDs omit the trace URL."""
    harness.fetcher._trace_ids = ()
    _install(harness, monkeypatch, telemetry_ingestion_source="big_query")

    result = asyncio.run(_preview(harness))

    assert result.examples[0].trace_url is None
    assert "trace_url" not in result.to_payload()["examples"][0]


def test_a_source_with_otel_ids_links_to_the_trace(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify that sources with OpenTelemetry trace IDs generate a Cloud Trace URL."""
    _install(harness, monkeypatch, telemetry_ingestion_source="cloud_ops")

    trace_url = asyncio.run(_preview(harness)).examples[0].trace_url

    assert trace_url is not None
    assert "tid=trace-1" in trace_url


# --- what the preview writes, and what it must not ------------------------- #


def test_the_previewed_conversations_are_archived(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify that all ingested preview conversations are archived in the store."""
    harness.fetcher._cases = [_case(f"s{index}") for index in range(5)]
    _install(harness, monkeypatch)

    asyncio.run(_preview(harness))

    assert [a.payload.trajectory_id for a in harness.store.archives] == [
        "s0",
        "s1",
        "s2",
        "s3",
        "s4",
    ]


def test_a_preview_writes_no_index_rows(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify that preview execution does not record trajectory index rows."""
    _install(harness, monkeypatch)

    asyncio.run(_preview(harness))

    assert harness.store.index_writes == []


def test_a_conversation_ingestion_gave_up_on_is_neither_archived_nor_shown(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify that uningested rows are omitted from archives and preview examples."""
    fetcher = harness.fetcher
    _install(harness, monkeypatch)

    def fetch_page() -> Page:
        fetcher.recorder.add(
            trajectory_id="s-dropped",
            session_id="s-dropped",
            trace_ids=(),
            status=IngestStatus.NOT_INGESTED,
            case=None,
        )
        fetcher.recorder.flush()
        return Page(
            dataset=EvaluationDataset(eval_cases=[]), next_page_token=None
        )

    fetcher.on_fetch_page = fetch_page

    result = asyncio.run(_preview(harness))

    assert result.examples == ()
    assert harness.store.archives == []


def test_the_preview_never_claims_the_agent(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify that preview execution does not acquire the investigation lock."""
    _install(harness, monkeypatch)
    claims: list[str] = []

    class RecordingLock:
        def acquire(self, agent_name: str, run_id: str) -> str | None:
            claims.append(agent_name)
            return None

        def release(self, agent_name: str, run_id: str) -> None:
            claims.append(agent_name)

    monkeypatch.setattr(
        locking, "lock_factory", lambda _config: RecordingLock()
    )

    asyncio.run(_preview(harness))

    assert claims == []


def test_the_blocking_work_runs_off_the_event_loop(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify that synchronous fetch and archive operations run in a worker thread."""
    _install(harness, monkeypatch)

    async def run() -> list[str]:
        await _preview(harness)
        return harness.store.thread_names

    thread_names = asyncio.run(run())

    assert thread_names
    assert threading.current_thread().name not in thread_names


def test_the_real_factory_builds_a_capped_fetcher_that_reports_to_the_preview() -> (
    None
):
    """Verify that default_fetcher_factory configures recorder, SQL, and timeout."""
    config = make_config(
        observed_agent_name=_AGENT,
        project_id=_PROJECT,
        telemetry_dataset="agent_analytics",
    )
    collector = preview._ArchiveCollector(agent_name=_AGENT)

    with mock.patch(
        "ambient_quality_agent.tools.ingestion.base.bigquery.Client"
    ) as client:
        fetcher = preview._create_default_fetcher(
            config, _SELECTOR, recorder=collector
        )

    assert fetcher._recorder is collector
    assert fetcher._selector_sql == _SELECTOR
    assert fetcher._job_timeout_ms == preview.JOB_TIMEOUT_MS
    assert client.called


# --- the three refusals ----------------------------------------------------- #


def test_a_lexically_invalid_selector_queries_nothing(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify that lexical validation failures prevent fetcher execution."""
    _install(harness, monkeypatch)

    result = asyncio.run(
        _preview(harness, selector_sql="SELECT 1 AS target_id")
    )

    assert not result.is_valid
    assert result.rejection is not None
    assert result.rejection.reason is selector.RejectionReason.UNBOUNDED_WINDOW
    assert harness.fetcher.calls == []


def test_a_selector_the_dry_run_refuses_queries_nothing_beyond_the_precheck(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify that precheck rejections halt execution before counting or fetching."""
    rejection = selector.Rejection(
        reason=selector.RejectionReason.UNAUTHORIZED_TABLE,
        explanation="The selector may only read the telemetry table.",
    )
    harness.fetcher._precheck = selector.PrecheckResult(rejection=rejection)
    _install(harness, monkeypatch)

    result = asyncio.run(_preview(harness))

    assert result.rejection is rejection
    assert harness.fetcher.calls == ["precheck"]
    assert harness.store.archives == []


def test_a_rejection_reaches_the_caller_as_data_it_can_act_on(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify that rejections return structured payload data without raising exceptions."""
    _install(harness, monkeypatch)

    payload = asyncio.run(
        _preview(harness, selector_sql="SELECT 1 AS target_id")
    ).to_payload()

    assert payload["rejected"] is True
    assert payload["reason"] == selector.RejectionReason.UNBOUNDED_WINDOW.value
    assert payload["explanation"]
    assert payload["examples"] == []


def test_an_eval_service_deployment_is_refused(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify that non-session-review analysis modes raise a ValueError."""
    _install(harness, monkeypatch, quality_analysis_mode="eval_service")

    with pytest.raises(ValueError, match="session_review"):
        asyncio.run(_preview(harness))

    assert harness.fetcher.calls == []


def test_an_empty_selector_is_a_caller_mistake(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify that empty selectors raise a ValueError."""
    _install(harness, monkeypatch)

    with pytest.raises(ValueError, match="empty selector"):
        asyncio.run(_preview(harness, selector_sql="  \n "))


def test_half_a_window_is_refused(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify that specifying only one window boundary raises a ValueError."""
    _install(harness, monkeypatch)

    with pytest.raises(ValueError, match="both window_start and window_end"):
        asyncio.run(_preview(harness, window_end=""))


def test_no_window_previews_the_configured_lookback(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify that omitting window bounds uses the configured lookback window."""
    _install(harness, monkeypatch, data_lookback_window=7)

    assert asyncio.run(
        _preview(harness, window_start="", window_end="")
    ).is_valid


# --- the wall-clock cap ------------------------------------------------------ #


def test_the_wall_clock_cap_returns_the_documented_shape(
    harness: Harness, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify that timeouts return a valid preview payload with timed_out set to True."""
    _install(harness, monkeypatch)
    monkeypatch.setattr(preview, "DEADLINE_SECONDS", 0.01)

    def slow_count() -> int:
        threading.Event().wait(1.0)
        return 1

    harness.fetcher.on_count_scanned = slow_count

    result = asyncio.run(_preview(harness))

    assert result.timed_out is True
    assert result.is_valid
    assert result.matched == 0
    assert result.examples == ()
    assert result.to_payload()["note"] == preview.TIMEOUT_NOTE


# --- where the archive goes ---------------------------------------------------- #


def test_a_standalone_preview_archives_in_process(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """In standalone mode a preview's conversations go to the in-process store.

    The preview archives what it tried through `store_factory`, a storage
    provider. Standalone binds it to the same `InMemoryStore` the sweep writes
    to. Constructing a BigQuery client raises here, so a provider left on the
    BigQuery binding fails this test.
    """
    from ambient_quality_agent import backends
    from google.cloud import bigquery

    def no_bigquery(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("a standalone preview reached BigQuery")

    monkeypatch.setattr(bigquery, "Client", no_bigquery)
    fetcher = FakePreviewFetcher(cases=[_case("s1")], matched=1)

    def fetcher_factory(
        config: Any, selector_sql: str, *, recorder: Any
    ) -> Any:
        del config, selector_sql
        fetcher.recorder = recorder
        return fetcher

    monkeypatch.setattr(preview, "fetcher_factory", fetcher_factory)
    harness = Harness(
        fetcher=fetcher, store=FakePayloadStore(), context=StateContext()
    )
    _install(harness, monkeypatch)

    with backends.override_providers(standalone=True) as store:
        result = asyncio.run(_preview(harness))

    assert store is not None
    assert [example.trajectory_id for example in result.examples] == ["s1"]
    assert list(store.archives) == ["s1"]
