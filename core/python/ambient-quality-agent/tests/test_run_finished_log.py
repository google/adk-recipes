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

"""The entry a log-based alert policy selects on when a run finishes."""

import asyncio
import json
import logging
import pathlib
import re

import pytest
from ambient_quality_agent.core.investigation import job_execution, model

from .conftest import StateContext


def _record(
    agent: str = "flight_booker", run_id: str = "abc123"
) -> model.InvestigationRecord:
    """Creates an investigation record fixture for finished log tests.

    Args:
        agent: Observed agent name.
        run_id: Investigation run identifier.

    Returns:
        Configured InvestigationRecord instance.
    """
    return model.InvestigationRecord(run_id=run_id, observed_agent_name=agent)


def _emitted(caplog: pytest.LogCaptureFixture) -> dict[str, object]:
    for line in caplog.messages:
        if line.lstrip().startswith("{"):
            payload = json.loads(line)
            if payload.get("event") == "aqa_investigation_finished":
                return payload
    raise AssertionError(f"no finished event in {caplog.messages}")


def test_carries_what_an_alert_needs(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO)
    job_execution._log_run_finished(
        _record(),
        {
            "status": "done",
            "counters": {"traces_eval_failed": 2},
            "summary": {"multi_turn_pages_evaluated": 7},
        },
    )

    payload = _emitted(caplog)
    assert payload["run_id"] == "abc123"
    assert payload["agent"] == "flight_booker"
    assert payload["status"] == "done"
    assert payload["traces_eval_failed"] == 2
    assert payload["pages"] == 7


def test_reports_a_failure_with_its_reason(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO)
    job_execution._log_run_finished(
        _record(), {"status": "failed", "error": "boom"}
    )

    payload = _emitted(caplog)
    assert payload["status"] == "failed"
    assert payload["error"] == "boom"


def test_a_broken_announcement_is_swallowed_and_logged(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A run that finished must not be reported failed because saying so broke."""
    caplog.set_level(logging.INFO)

    job_execution._log_run_finished(None, {"status": "done"})  # type: ignore[arg-type]

    assert "could not log run" in caplog.text


def test_truncates_an_error_before_it_reaches_the_log(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO)
    job_execution._log_run_finished(
        _record(), {"status": "failed", "error": "x" * 500}
    )

    assert len(_emitted(caplog)["error"]) == 201  # type: ignore[arg-type]


def test_counts_both_kinds_of_page(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO)
    job_execution._log_run_finished(
        _record(),
        {
            "status": "done",
            "summary": {
                "multi_turn_pages_evaluated": 4,
                "single_turn_pages_evaluated": 6,
            },
        },
    )

    assert _emitted(caplog)["pages"] == 10


# The keys the alert policy labels on (terraform/modules/aqa/notifications.tf).
# Named here so a change to the emitted shape fails instead of going quiet.
_POLICY_KEYS = (
    "event",
    "run_id",
    "status",
    "insights_created",
    "insights_recurring",
    "traces_evaluated",
    "traces_eval_failed",
    "traces_eval_errored",
    "error",
)

_NOTIFICATIONS_TF = (
    pathlib.Path(__file__).parent.parent
    / "terraform"
    / "modules"
    / "aqa"
    / "notifications.tf"
)


def test_the_policy_reads_no_field_this_never_writes(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Read the keys out of the policy rather than trusting the list above.

    `_POLICY_KEYS` is a copy, so it holds only the Python side honest. A field
    renamed in the Terraform selects nothing, and a filter that selects nothing
    looks exactly like a period with no failures.
    """
    caplog.set_level(logging.INFO)
    job_execution._log_run_finished(
        _record(), {"status": "failed", "error": "boom", "summary": {}}
    )
    payload = _emitted(caplog)

    read_by_policy = set(
        re.findall(
            r"jsonPayload\.([A-Za-z_][A-Za-z0-9_]*)",
            _NOTIFICATIONS_TF.read_text(),
        )
    )
    assert read_by_policy, (
        "no jsonPayload reference found; has the policy moved?"
    )

    missing = sorted(read_by_policy - payload.keys())
    assert not missing, (
        f"the policy reads {missing}, which the agent never writes"
    )


def test_the_alert_policy_can_read_what_is_emitted(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO)
    job_execution._log_run_finished(
        _record(run_id="r-42"),
        {
            "status": "failed",
            "error": "boom",
            "counters": {"traces_eval_failed": 3},
        },
    )
    payload = _emitted(caplog)

    missing = [k for k in _POLICY_KEYS if k not in payload]
    assert not missing, f"policy selects on {missing}, which is absent"
    assert payload["run_id"] == "r-42"
    assert payload["traces_eval_failed"] == 3


def test_a_run_that_never_started_is_announced(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Submission failing is the silence the alarm exists to catch."""
    caplog.set_level(logging.INFO)
    job_execution._log_run_finished(
        _record(run_id="never"),
        {"status": "failed", "error": "failed: RuntimeError: submit blew up"},
    )

    payload = _emitted(caplog)
    assert payload["run_id"] == "never"
    assert payload["status"] == "failed"
    assert payload["pages"] == 0


def test_status_serialises_as_a_bare_value(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """An enum would reach the mail as `RunStatus.DONE`."""
    caplog.set_level(logging.INFO)
    job_execution._log_run_finished(
        _record(),
        {"status": model.RunStatus.DONE, "counters": {"traces_eval_failed": 0}},
    )

    assert '"status": "done"' in next(
        m for m in caplog.messages if "aqa_investigation_finished" in m
    )


def test_a_failed_run_still_carries_countable_numbers(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A failure records no counters, and the mail interpolates them anyway."""
    caplog.set_level(logging.INFO)
    job_execution._log_run_finished(
        _record(), {"status": "failed", "error": "boom"}
    )

    assert _emitted(caplog)["traces_eval_failed"] == 0


def test_a_run_whose_record_is_gone_is_announced(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The record can be missing from state, and that ending has no other trace.

    Nothing downstream runs for it: there is no graph, no terminal status
    written, and no job object. Without an entry it is silence.
    """
    caplog.set_level(logging.INFO)

    result = asyncio.run(
        job_execution.execute_investigation("vanished", StateContext({}))
    )

    assert result["status"] == "failed"
    payload = _emitted(caplog)
    assert payload["run_id"] == "vanished"
    assert payload["status"] == "failed"


def test_a_crash_outside_the_graph_is_still_announced(
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An IAM regression on the jobs bucket makes claiming the marker raise.

    This path is outside the graph's own handler. A failure there leaves no
    terminal status and no entry, which is the silence the alarm watches for.
    """
    caplog.set_level(logging.INFO)

    def _boom(bucket: str, name: str) -> bool:
        raise PermissionError("storage.objects.create denied")

    monkeypatch.setattr(job_execution.gcs, "claim_once", _boom)

    state: dict[str, object] = {}

    async def _drive() -> str:
        record = await model.create_run(
            state,
            model.InvestigationRecord(
                status=model.RunStatus.PENDING,
                effective_config={"jobs_gcs_bucket": "b"},
                idempotency_key="k",
            ),
        )
        with pytest.raises(PermissionError):
            await job_execution.execute_investigation(
                record.run_id, StateContext(state)
            )
        stored = await model.get_run(state, record.run_id)
        assert stored.status == model.RunStatus.FAILED
        return record.run_id

    run_id = asyncio.run(_drive())

    payload = _emitted(caplog)
    assert payload["run_id"] == run_id
    assert payload["status"] == "failed"
    assert "PermissionError" in str(payload["error"])


def test_carries_the_insight_counts_the_rule_reads(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A sweep that only re-saw known problems must not read as a finding."""
    caplog.set_level(logging.INFO)
    job_execution._log_run_finished(
        _record(),
        {
            "status": model.RunStatus.DONE,
            "counters": {
                "traces_evaluated": 9,
                "traces_eval_failed": 7,
                "insights_created": 0,
                "insights_recurring": 3,
            },
        },
    )

    payload = _emitted(caplog)
    assert payload["insights_created"] == 0
    assert payload["insights_recurring"] == 3
    assert payload["traces_eval_failed"] == 7
    assert payload["traces_evaluated"] == 9
    assert payload["traces_eval_errored"] == 0


def test_a_run_that_went_well_carries_an_empty_reason(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A null reaches the mail as the literal `NULL_VALUE`, on every clean run."""
    caplog.set_level(logging.INFO)
    job_execution._log_run_finished(
        _record(),
        {"status": model.RunStatus.DONE, "counters": {"insights_created": 1}},
    )

    assert _emitted(caplog)["error"] == ""


# Driven through the real `_extract_counters`, so these fail if the pipeline stops
# producing a counter the rule reads. A filter cannot fail that way.


def _counters_from_state(**state: object) -> dict[str, object]:
    return job_execution._extract_counters(dict(state))


def test_a_newly_found_defect_is_worth_an_email() -> None:
    counters = _counters_from_state(
        counters={"insights_created": 2, "insights_recurring": 0}
    )
    notify, reason = job_execution._should_notify(
        model.RunStatus.DONE, counters
    )
    assert notify, "a new defect must reach a human"
    assert "2" in reason


def test_a_defect_already_tracked_is_not() -> None:
    counters = _counters_from_state(
        counters={"insights_created": 0, "insights_recurring": 3}
    )
    notify, _ = job_execution._should_notify(model.RunStatus.DONE, counters)
    assert not notify, "a tracked defect would resend on every sweep"


def test_a_clean_run_is_not() -> None:
    counters = _counters_from_state(counters={"traces_eval_passed": 5})
    assert not job_execution._should_notify(model.RunStatus.DONE, counters)[0]


def test_failures_that_grouped_into_nothing_are() -> None:
    counters = _counters_from_state(
        counters={"traces_eval_passed": 1, "traces_eval_failed": 4}
    )
    notify, reason = job_execution._should_notify(
        model.RunStatus.DONE, counters
    )
    assert notify, (
        "traces failed and nothing was grouped: it would go unreported"
    )
    assert "group" in reason


def test_a_trace_the_evaluator_could_not_judge_is_worth_an_email() -> None:
    """Case status ranks ERRORED above FAILED, so a trace that both
    failed a metric and hit an API error counts only as errored.

    Reading `traces_eval_failed` alone therefore misses a whole sweep of
    them -- a run measuring nothing at all would read as a quiet, healthy one.
    """
    counters = _counters_from_state(
        counters={"traces_evaluated": 5, "traces_eval_errored": 5}
    )
    notify, reason = job_execution._should_notify(
        model.RunStatus.DONE, counters
    )
    assert notify, "a sweep that judged nothing must not pass for a clean one"
    assert "5" in reason and "judge" in reason


def test_an_eval_error_reports_even_when_the_defect_is_already_tracked() -> (
    None
):
    """The `recurring` guard exists so a tracked defect does not resend every
    sweep. It says nothing about the evaluator failing, which is ours to fix."""
    counters = _counters_from_state(
        counters={"insights_recurring": 2, "traces_eval_errored": 1}
    )
    notify, reason = job_execution._should_notify(
        model.RunStatus.DONE, counters
    )
    assert notify
    assert "judge" in reason


def test_a_defect_outranks_an_eval_error_in_the_reason() -> None:
    """Both reach the body as counts; the reason leads with the one that sends
    the reader to the observed agent rather than to our own logs."""
    counters = _counters_from_state(
        counters={"insights_created": 2, "traces_eval_errored": 3}
    )
    _, reason = job_execution._should_notify(model.RunStatus.DONE, counters)
    assert reason == "2 newly found problem(s)"


def test_a_failed_run_is() -> None:
    notify, reason = job_execution._should_notify(model.RunStatus.FAILED, {})
    assert notify and "failed" in reason


def test_a_duplicate_trigger_is_not() -> None:
    notify, _ = job_execution._should_notify(model.RunStatus.SKIPPED, {})
    assert not notify, "the idempotency guard working is not a fault"


def test_the_entry_carries_the_decision_and_the_body(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO)
    job_execution._log_run_finished(
        _record(),
        {"status": model.RunStatus.DONE, "counters": {"insights_created": 1}},
    )
    payload = _emitted(caplog)
    assert payload["should_notify"] is True
    assert payload["notify_reason"] == "1 newly found problem(s)"


def test_a_quiet_run_carries_no_reason(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO)
    job_execution._log_run_finished(_record(), {"status": model.RunStatus.DONE})
    payload = _emitted(caplog)
    assert payload["should_notify"] is False
    assert payload["notify_reason"] == ""


def test_the_entry_carries_the_reason_the_email_leads_with() -> None:
    _, reason = job_execution._should_notify(
        model.RunStatus.DONE, {"insights_created": 2}
    )
    assert reason == "2 newly found problem(s)"


def test_the_policy_filter_reads_only_the_decision() -> None:
    """The rule is in Python, so the filter must not re-implement any of it."""
    filter_text = _NOTIFICATIONS_TF.read_text()
    condition = filter_text[filter_text.index("filter = <<-EOT") :]
    condition = condition[: condition.index("EOT", 20)]
    for leaked in (
        "insights_created",
        "insights_recurring",
        "traces_eval_failed",
        "traces_eval_errored",
    ):
        assert leaked not in condition, (
            f"{leaked} is the rule leaking back into HCL"
        )
