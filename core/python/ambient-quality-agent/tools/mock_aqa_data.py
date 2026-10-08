#!/usr/bin/env python3
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

"""Generate a deployment's worth of AQA data, for the dashboard to draw.

A live deployment accumulates one sweep per ambient tick, so a dashboard
visualization can only be developed against a deployment that has been running
for weeks -- ours has two investigations and one failure, which is not enough to
tell a chart that works from a chart that happens to look right. This script
writes the same payloads a real deployment would return, for as many weeks as
you ask for, in about a second.

What comes out is a single JSON file, `tools/mock_aqa_server.py` serves it, and
the dashboard cannot tell the difference: every record is built as the agent's
own model and serialized by the agent's own presenter (`format_run`,
`InsightView`, `InsightOccurrence`), so the wire shapes are not a copy of the
real ones -- they are the real ones. A model change breaks this script rather
than quietly producing a fixture that lies.

Usage:

    uv run --frozen python tools/mock_aqa_data.py --out scratch/mock_aqa.json

What the generated deployment looks like
----------------------------------------
An ambient AQA watching one observed agent on a cadence (`--runs-per-day`) for
`--days`, so the sweeps tile the history as tumbling windows the way
`derive_window_and_budget` lays them out. Traffic follows a working week and a
working day; quality does not hold still:

* a handful of *chronic* defects recur at a low rate throughout,
* a **regression** lands `--incident-days-ago` days before the end -- the
  failure rate jumps and three defects appear that were never seen before,
* two early defects stop occurring and age past `insights_auto_resolve_days`
  into `RESOLVED`,
* the last sweep is still `running`, one sweep `failed` outright, and one was
  `skipped` as a duplicate trigger.

That is the shape a chart has to survive: a baseline to read a spike against,
counters that move together (a spike in failures is a spike in rubrics and in
new insights), and runs that carry no counters at all because they never got
that far.

Every figure is derived from one seeded `random.Random`, so a given
`--seed` + `--days` + `--runs-per-day` always yields the same deployment; only
the timestamps move, because they are anchored to `--anchor` (now, by default)
so "the past 7 days" is always the 7 days before you look.
"""

from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import hashlib
import json
import os
import pathlib
import random
import sys
import uuid
from typing import Any

# Defaults for the env vars `config.load()` requires, set before the agent
# imports below resolve `config`. `setdefault` means an exported value wins.
# Nothing here reaches Google Cloud -- the values only have to be well-formed
# enough for the frozen `Config` to validate, since the generated `show_config`
# payload is that config.
# Disable `.env` loading from the agent package so the run uses only
# explicitly exported environment variables and the defaults below.
os.environ.setdefault("PYTHON_DOTENV_DISABLED", "1")
os.environ.setdefault("AQA_OBSERVED_AGENT_NAME", "travel_desk_agent")
os.environ.setdefault("GOOGLE_CLOUD_PROJECT", "mock-aqa-project")
os.environ.setdefault("GOOGLE_CLOUD_LOCATION", "us-central1")
os.environ.setdefault("AQA_JOBS_GCS_BUCKET", "mock-aqa-project-aqua-jobs")
os.environ.setdefault("AQA_AMBIENT_CADENCE_SECONDS", "21600")

from agentplatform._genai.types.evals import (
    AgentEvent,
    ConversationTurn,
)
from ambient_quality_agent.config import Config, load
from ambient_quality_agent.core.investigation.model import format_run
from ambient_quality_agent.tools.insights.findings import Finding
from ambient_quality_agent.tools.insights.models import (
    VERIFICATION_ANALYSIS_KEY,
    ClusterVerification,
    Insight,
    InsightOccurrence,
    InsightStatus,
    InsightView,
    OccurrenceState,
    ProposedEdit,
    RootCause,
    RubricExample,
)
from ambient_quality_agent.tools.investigations.models import (
    InvestigationCounters,
    InvestigationEvent,
    InvestigationRecord,
    RunStatus,
)
from ambient_quality_agent.tools.observed_agent_config.effective_config import (
    serialize_config,
)

DATASET_VERSION = 3
"""Bumped when the file layout changes, so the server can refuse an old one."""


# --------------------------------------------------------------------------- #
# The observed agent's defects                                                 #
# --------------------------------------------------------------------------- #
# One entry per root cause the imaginary agent suffers from, phrased as the
# clustering model would label it. A defect is an *insight*: every sweep that
# sees it again adds an occurrence rather than minting a second issue, which is
# what makes the recurring/new split on the dashboard mean anything.
#
# The wordings vary per occurrence on purpose. A rubric that always reads the
# same makes a chart of "distinct issues" trivially right, and hides the case
# the real pipeline actually has to handle.


@dataclasses.dataclass(frozen=True)
class Defect:
    """A root cause, its insight label, and how a failing rubric reports it."""

    key: str
    """Stable identity across sweeps; the mock's stand-in for the same-issue
    judge in `insights.matching`."""

    label: str
    """The insight label, as `cluster_and_label` would write it."""

    expected: tuple[str, ...]
    """Rubric property descriptions -- what the judge required."""

    actual: tuple[str, ...]
    """Judge reasonings for failing it. ``{case}`` interpolates the case id."""

    prompts: tuple[str, ...]
    """User turns that provoke it, for the evidence traces."""

    weight: float = 1.0
    """Relative likelihood of showing up in any given sweep."""

    starts_days_ago: float | None = None
    """First day it can occur, counting back from the anchor. ``None`` = always
    (a chronic defect); a value marks a regression that landed mid-history."""

    ends_days_ago: float | None = None
    """Last day it occurs. Set for a defect that was fixed, so its insight goes
    stale and auto-resolves."""

    guaranteed: bool = False
    """Found by the newest completed sweep and no other, rather than sampled by
    weight. Set on the just-appeared defect so the fixture reliably holds an
    insight in each lifecycle state -- a `NEW` one included, which is the state
    a weighted draw is least likely to leave behind."""


DEFECTS: tuple[Defect, ...] = (
    # --- chronic -------------------------------------------------------- #
    Defect(
        key="file_expense_invalid_category",
        label="calls file_expense with a category outside the accepted set",
        expected=(
            "file_expense must be called with one of the accepted category values.",
            "The expense category has to come from the tool's documented enum.",
            "Every file_expense call must carry a valid category argument.",
        ),
        actual=(
            "file_expense was called with category='Taxi' for {case}; the tool "
            "rejected it and the agent reported success anyway.",
            "The call for {case} passed category='travel', which is not one of "
            "Airfare, Lodging, Meals or Ground.",
            "category='Flight' on the file_expense call ({case}); the tool raised "
            "and the error was never surfaced to the user.",
        ),
        prompts=(
            "I took a taxi from the airport to the hotel, 42 euros. Can you expense it?",
            "Please file my flight to Zurich, 310 CHF.",
            "Expense my airport parking, $28.",
        ),
        weight=3.0,
    ),
    Defect(
        key="policy_lookup_skipped",
        label="answers travel-policy questions without calling lookup_travel_policy",
        expected=(
            "The agent must call lookup_travel_policy before quoting a limit.",
            "Policy answers have to be grounded in a lookup_travel_policy call.",
            "Any per-diem figure must come from the policy tool, not the model.",
        ),
        actual=(
            "The agent quoted a $75 per-diem for {case} from memory; no tool call "
            "appears in the trajectory.",
            "No lookup_travel_policy call was made while answering {case}.",
            "The trajectory for {case} contains only a model response asserting the "
            "limit.",
        ),
        prompts=(
            "What's the meal allowance for a trip to Tokyo?",
            "Am I allowed business class to São Paulo at my level?",
            "How much can I spend on a hotel in Munich?",
        ),
        weight=2.5,
    ),
    Defect(
        key="employee_id_hallucinated",
        label="invents an employee id instead of asking for one",
        expected=(
            "get_employee_profile must be called with the id the user supplied.",
            "The agent has to ask for an employee id it was not given.",
            "No identifier may be fabricated to satisfy a tool argument.",
        ),
        actual=(
            "get_employee_profile was called with employee_id='e-0001' for {case}, "
            "an id the user never mentioned.",
            "The agent guessed an employee id in {case} rather than asking.",
            "employee_id was filled in with a placeholder on the call for {case}.",
        ),
        prompts=(
            "Book me a hotel in Dublin for next Tuesday.",
            "What's my travel grade?",
            "Can you check whether I'm allowed to rent a car?",
        ),
        weight=1.5,
    ),
    Defect(
        key="transfer_loop",
        label="bounces the conversation between the root and expense agents",
        expected=(
            "A transfer must move the conversation forward, not return it.",
            "The agent should not hand back to an agent that just handed to it.",
            "Delegation has to terminate in an answer to the user.",
        ),
        actual=(
            "transfer_to_agent was called four times in {case} with no tool doing "
            "any work in between.",
            "The trajectory for {case} alternates between the root and the expense "
            "sub-agent until the turn ends.",
            "The conversation in {case} was transferred back and forth and the user "
            "was never answered.",
        ),
        prompts=(
            "I need to expense a hotel and also change my flight.",
            "Can you sort out both my visa question and my dinner receipt?",
        ),
        weight=1.0,
    ),
    # --- fixed early on, so its insight ages into RESOLVED ---------------- #
    Defect(
        key="currency_not_converted",
        label="files foreign-currency expenses without converting the amount",
        expected=(
            "An expense in a foreign currency must be converted before filing.",
            "The amount and currency arguments have to agree with the receipt.",
        ),
        actual=(
            "The receipt in {case} was in JPY but file_expense was called with "
            "currency='USD' and the unconverted amount.",
            "No conversion happened for {case}; 12,000 was filed as dollars.",
        ),
        prompts=(
            "Here's my dinner receipt from Tokyo, 12000 yen.",
            "Expense this taxi in Seoul, 24000 won.",
        ),
        weight=1.5,
        ends_days_ago=17.0,
    ),
    Defect(
        key="silent_tool_error",
        label="reports success after a tool call raised",
        expected=(
            "A failed tool call must be surfaced to the user.",
            "The agent may not claim an action succeeded when the tool errored.",
        ),
        actual=(
            "The file_expense call in {case} raised, and the agent replied 'All done!'.",
            "The tool error in {case} never reached the user.",
        ),
        prompts=(
            "File my Lisbon hotel, 180 euros.",
            "Please submit my mileage claim for last week.",
        ),
        weight=1.2,
        ends_days_ago=15.0,
    ),
    # --- arriving over the history, so new insights are not all on day one - #
    # A month-old deployment does not mint every issue in its first sweep:
    # defects appear as the observed agent changes. These stagger the series a
    # "new insights per day" chart is meant to show.
    Defect(
        key="attaches_wrong_receipt",
        label="attaches the previous receipt to a new expense",
        expected=(
            "The receipt filed must be the one referenced in this turn.",
            "Each expense has to carry its own attachment.",
        ),
        actual=(
            "The receipt from an earlier turn was reused for {case}.",
            "file_expense in {case} referenced the prior attachment id.",
        ),
        prompts=(
            "Here's the second receipt -- the dinner one.",
            "And this one is the train ticket, 46 euros.",
        ),
        weight=1.4,
        starts_days_ago=21.0,
    ),
    Defect(
        key="timezone_off_by_one",
        label="books travel one day off when the request crosses a timezone",
        expected=(
            "Dates must be interpreted in the traveller's home timezone.",
            "The booking date has to match the date the user asked for.",
        ),
        actual=(
            "The user asked for the 3rd in {case}; the booking landed on the 2nd.",
            "The date in {case} was shifted by a day against the stated timezone.",
        ),
        prompts=(
            "Fly me to Sydney arriving on the 3rd.",
            "I need to be in Bangalore by Monday morning.",
        ),
        weight=1.3,
        starts_days_ago=16.0,
    ),
    Defect(
        key="policy_answer_stale",
        label="quotes a superseded per-diem after the policy tool returned the new one",
        expected=(
            "The answer must reflect the value the policy tool returned.",
            "A tool result overrides anything the model believed beforehand.",
        ),
        actual=(
            "lookup_travel_policy returned $95 in {case}; the reply said $75.",
            "The tool result in {case} was fetched and then ignored.",
        ),
        prompts=(
            "What's the per-diem for New York these days?",
            "Has the meal allowance changed for the US?",
        ),
        weight=1.6,
        starts_days_ago=12.0,
    ),
    Defect(
        key="asks_for_known_field",
        label="asks the user for an employee id it already fetched",
        expected=(
            "The agent must not re-ask for information already in the session.",
            "Known values have to be reused across turns.",
        ),
        actual=(
            "The profile was fetched in turn 1 of {case}; turn 3 asks for the id again.",
            "The agent re-prompted for a value it had already received ({case}).",
        ),
        prompts=(
            "It's e-4417. Now book me a hotel in Porto.",
            "My id is e-2210 -- can you also check my grade?",
        ),
        weight=1.1,
        starts_days_ago=9.0,
    ),
    Defect(
        key="over_eager_escalation",
        label="hands off to a human for questions the policy tool can answer",
        expected=(
            "Escalation is a last resort, after the tools have been tried.",
            "The agent should answer what its tools cover.",
        ),
        actual=(
            "{case} was escalated without any lookup_travel_policy call.",
            "The agent escalated {case} on the first turn.",
        ),
        prompts=(
            "Is breakfast covered on domestic trips?",
            "Do I need approval for a rental car?",
        ),
        weight=1.2,
        starts_days_ago=6.0,
    ),
    Defect(
        key="rounds_amount_down",
        label="rounds expense amounts to whole units before filing",
        expected=(
            "The filed amount must match the receipt to the cent.",
            "Amounts may not be rounded.",
        ),
        actual=(
            "The receipt in {case} read 84.60 and 84 was filed.",
            "file_expense was called with a truncated amount for {case}.",
        ),
        prompts=(
            "Expense this lunch, 84.60.",
            "Taxi was 31.25, please file it.",
        ),
        weight=1.5,
        starts_days_ago=2.0,
    ),
    # --- the regression: three defects that arrive together --------------- #
    Defect(
        key="regression_dropped_destination",
        label="drops the destination when chaining policy lookup into a booking",
        expected=(
            "The destination the user gave must be carried into every downstream call.",
            "Arguments established earlier in the conversation may not be lost.",
        ),
        actual=(
            "lookup_travel_policy was called with destination='' in {case} after "
            "the user said Copenhagen.",
            "The destination from turn 1 of {case} is absent from the turn-3 call.",
            "The booking in {case} was attempted with no destination at all.",
        ),
        prompts=(
            "I'm going to Copenhagen in March -- what's the policy, and can you book it?",
            "Trip to Nairobi next month; check the rules then book the hotel.",
        ),
        weight=4.0,
        starts_days_ago=3.0,
    ),
    Defect(
        key="regression_truncated_answer",
        label="stops mid-sentence when the policy answer is long",
        expected=(
            "The response must be a complete answer.",
            "A policy summary has to state the limit it was asked for.",
        ),
        actual=(
            "The reply in {case} ends after 'The per-diem for' with nothing "
            "following it.",
            "The final response in {case} is truncated mid-sentence.",
        ),
        prompts=(
            "Summarise the whole travel policy for senior engineers going to the US.",
            "What are all the rules for international travel at my level?",
        ),
        weight=2.5,
        starts_days_ago=3.0,
    ),
    Defect(
        key="regression_ignores_correction",
        label="ignores a correction the user makes in a later turn",
        expected=(
            "A user correction must override the earlier value.",
            "The agent has to act on the most recent instruction.",
        ),
        actual=(
            "The user changed the date to the 14th in {case}; the booking used the 9th.",
            "The correction in turn 2 of {case} had no effect on the tool call.",
        ),
        prompts=(
            "Book Vienna for the 9th -- sorry, make that the 14th.",
            "Two nights in Oslo. Actually three.",
        ),
        weight=2.0,
        starts_days_ago=3.0,
    ),
    # --- first seen in the newest sweep, so it is still NEW ---------------- #
    Defect(
        key="fresh_duplicate_booking",
        label="books the same hotel twice when the user confirms",
        expected=(
            "A confirmation must not re-issue the booking call.",
            "The agent has to distinguish confirming from re-requesting.",
        ),
        actual=(
            "'Yes please' in {case} produced a second identical booking call.",
            "The booking in {case} was submitted twice within one turn.",
        ),
        prompts=("Yes please, book it.", "That one works -- go ahead."),
        guaranteed=True,
    ),
)


# --------------------------------------------------------------------------- #
# Scenario knobs                                                               #
# --------------------------------------------------------------------------- #


@dataclasses.dataclass(frozen=True)
class Scenario:
    """Everything that shapes the generated deployment."""

    days: int = 30
    runs_per_day: int = 4
    seed: int = 7
    anchor: dt.datetime = dataclasses.field(
        default_factory=lambda: dt.datetime.now(tz=dt.UTC)
    )
    incident_days_ago: float = 3.0
    """When the regression landed. Also when the three regression defects start,
    so `--incident-days-ago` moves both together."""

    baseline_failure_rate: float = 0.08
    """Share of evaluated traces that fail outside the incident."""

    incident_failure_rate: float = 0.31
    """Share during the incident -- a spike a bar chart should show plainly."""

    failure_rate: float | None = None
    """A flat failure share, overriding both rates above and the incident shape.

    For the degenerate deployments the dashboard also has to render: ``0.0`` is
    one where nothing ever fails, so nothing clusters and no insight is ever
    minted. A view that only works against a deployment with problems in it is
    a view nobody can trust on a healthy one."""

    verified_rate: float = 0.0
    """Share of sightings carrying a cluster-verification result.

    Zero by default because that is what a deployment produces: `verify_clusters`
    exists but nothing calls it, so no occurrence in BigQuery has an analysis and
    a mock that generated them would show a dashboard nobody can get. Raise it to
    see the diagnosed half of the insight pane, which is otherwise unreachable
    without deploying the pass first."""

    root_cause_rate: float = 0.0
    """Share of insights carrying a recorded root cause.

    Defaults to 0.0 to match initial deployments before RCA chat turns record
    diagnoses. Increase above 0.0 to simulate diagnosed insights and populate the
    root-cause panel.
    """

    drop_rate: float = 0.02
    """Share of sampled traces lost on the way in.

    ``1.0`` is the other degenerate deployment: everything is dropped before
    ingestion, so the funnel stops at its second bar and nothing downstream has
    a subject."""

    no_telemetry: bool = False
    """Whether the observed agent has exported any telemetry at all.

    The state a customer is in before their agent has served traffic, and the
    one b/563290003 was filed on: BigQuery infers the sink's ``labels`` STRUCT
    from the rows it holds, so a column no exported trace has carried does not
    exist and every sweep fails its first read with `_NO_TELEMETRY_ERROR`. It
    is not reachable from the other knobs -- those shape a pipeline that ran,
    and this is one that never started -- and it is the only scenario in which
    the dashboard has nothing but failures to draw."""

    traces_per_hour: float = 240.0
    """Peak weekday traffic. Scaled down by hour of day and at weekends."""

    source_published: bool = True
    """Whether a source snapshot has been published for the observed agent.

    True by default because a deployment publishes one at deploy time. ``False``
    is the other state the Source card draws -- nothing published, so findings
    cite no code -- which is otherwise reachable only by not deploying."""

    source_dir: str = ""
    """A real directory to summarize instead of a synthesized snapshot.

    Point it at any repository and the card describes that code, walked under
    the same per-file and whole-snapshot caps the publisher applies. A tree with
    one large generated file then reaches the amber "truncated" state, which a
    synthesized fixture never does."""

    @property
    def cadence_seconds(self) -> int:
        """Ambient cadence: the sweeps tile the day without overlapping."""
        return round(86400 / self.runs_per_day)


# --------------------------------------------------------------------------- #
# Traffic and funnel                                                           #
# --------------------------------------------------------------------------- #

_HOURLY_SHAPE = (
    # 00-05: overnight        06-11: morning ramp
    0.10, 0.07, 0.05, 0.05, 0.07, 0.12, 0.28, 0.55, 0.85, 1.00, 1.00, 0.92,
    # 12-17: lunch dip, afternoon   18-23: evening tail
    0.70, 0.88, 0.95, 0.90, 0.78, 0.55, 0.38, 0.28, 0.22, 0.18, 0.15, 0.12,
)  # fmt: skip
"""Share of peak traffic by hour of day (index = UTC hour)."""


def _compute_traffic_factor(when: dt.datetime) -> float:
    """Computes relative telemetry volume in the window ending at ``when``.

    Models diurnal patterns (morning ramp, lunch dip, evening tail) and weekly
    cycles (reduced weekend traffic).

    Args:
        when: Timestamp marking the end of the telemetry window.

    Returns:
        Scaling factor relative to peak traffic volume.
    """
    weekday = 1.0 if when.weekday() < 5 else 0.22
    return weekday * _HOURLY_SHAPE[when.hour]


def _build_funnel_counters(
    rng: random.Random,
    *,
    scanned: int,
    cap: int,
    metric_count: int,
    failure_rate: float,
    drop_rate: float,
    eval_error_rate: float,
    clustering_broke: bool,
) -> tuple[InvestigationCounters, int]:
    """Builds consistent investigation funnel counters across pipeline stages.

    Maintains arithmetic invariants across stages:
    1. sampled = min(scanned, cap) = ingested + partial + dropped
    2. evaluated = ingested + partial = passed + failed + errored
    3. rubrics partition into failed, errored, and unclustered subsets

    Args:
        rng: Seeded random number generator.
        scanned: Number of raw traces discovered in the window.
        cap: Maximum evaluation cap per metric.
        metric_count: Number of configured evaluation metrics.
        failure_rate: Expected probability of trace evaluation failure.
        drop_rate: Probability of trace ingestion failure.
        eval_error_rate: Probability of evaluation execution error.
        clustering_broke: Whether to simulate an unhandled clustering error.

    Returns:
        Tuple of `(InvestigationCounters, failed_rubrics_count)`.
    """
    sampled = min(scanned, cap)
    dropped = (
        sampled if drop_rate >= 1 else _sample_binomial(rng, sampled, drop_rate)
    )
    partial = _sample_binomial(rng, sampled - dropped, 0.05)
    ingested = sampled - dropped - partial
    evaluated = ingested + partial

    errored = _sample_binomial(rng, evaluated, eval_error_rate)
    failed = _sample_binomial(rng, evaluated - errored, failure_rate)
    passed = evaluated - errored - failed

    # Every (trace, metric) pair yields a handful of rubric verdicts, pass or
    # fail alike; this is the denominator the "rubrics lost" figures read against.
    rubrics = int(evaluated * metric_count * rng.uniform(3.4, 4.6))

    # Only failing traces contribute failed rubrics, and a failing trace usually
    # breaks more than one property.
    failed_rubrics = min(rubrics, int(failed * rng.uniform(1.6, 3.1)))
    # A clustering chunk that raised takes its whole chunk's rubrics with it --
    # rare, and worth having in the data because it is drawn as a warning.
    errored_rubrics = (
        _sample_binomial(rng, failed_rubrics, 0.35) if clustering_broke else 0
    )
    unclustered = _sample_binomial(rng, failed_rubrics - errored_rubrics, 0.04)

    counters = InvestigationCounters(
        traces_scanned=scanned,
        traces_ingested=ingested,
        traces_ingested_partial=partial,
        traces_ingested_failed=dropped,
        traces_evaluated=evaluated,
        traces_eval_passed=passed,
        traces_eval_failed=failed,
        traces_eval_errored=errored,
        rubrics_generated=rubrics,
        rubrics_errored=errored_rubrics,
        rubrics_unclustered=unclustered,
    )
    return counters, failed_rubrics


def _sample_binomial(
    rng: random.Random, trials: int, probability: float
) -> int:
    """Draws a binomial sample using repeated Bernoulli trials.

    Args:
        rng: Seeded random number generator.
        trials: Number of independent trials.
        probability: Success probability per trial (0.0 to 1.0).

    Returns:
        Number of successful outcomes.
    """
    if trials <= 0 or probability <= 0:
        return 0
    return sum(1 for _ in range(trials) if rng.random() < probability)


# --------------------------------------------------------------------------- #
# Building the deployment                                                      #
# --------------------------------------------------------------------------- #


_PUBLISHED_METRICS = (
    # name, kind, expects, threshold, failure share, imports a model client
    (
        "answers_in_celsius",
        "local",
        "The agent answers in Celsius.",
        1.0,
        0.05,
        False,
    ),
    ("cites_a_source", "local", "The agent cites a source.", 0.8, 0.18, True),
    (
        "reply_omits_pii",
        "remote",
        "The reply carries no personal data.",
        1.0,
        0.40,
        False,
    ),
    ("answer_is_polite", "judged", "The reply is polite.", 0.7, 0.12, False),
)
"""Mock custom metrics that produce pass, fail, and error counts."""

_UNRUN_METRICS = (
    (
        "bleu",
        "predefined",
        "it only parameterizes a metric computed elsewhere",
    ),
    (
        "schema_conformance",
        "refused",
        "it sets both custom_function and custom_function_file",
    ),
)
"""Mock declined and refused metrics included in metrics_detail."""


def _build_custom_metric_summary(
    counters: InvestigationCounters, bucket: str
) -> dict[str, Any]:
    """Builds mock metrics_by_name and metrics_detail summary maps.

    Args:
        counters: Investigation counters for the sweep.
        bucket: Configured Cloud Storage bucket name for custom metrics.

    Returns:
        Dictionary containing `metrics_by_name` and `metrics_detail` maps, or
        empty dictionary if no bucket is configured.
    """
    if not bucket:
        return {}
    judged = counters.traces_evaluated
    errored = counters.traces_eval_errored
    by_name: dict[str, dict[str, int]] = {}
    detail: dict[str, dict[str, Any]] = {}
    for (
        name,
        kind,
        expects,
        threshold,
        share,
        calls_model,
    ) in _PUBLISHED_METRICS:
        failed = round((judged - errored) * share)
        by_name[name] = {
            "passed": judged - errored - failed,
            "failed": failed,
            "errored": errored,
        }
        detail[name] = {
            "kind": kind,
            "expected": expects,
            "threshold": threshold,
            "model_modules": ["google.genai"] if calls_model else [],
        }
    for name, kind, why in _UNRUN_METRICS:
        detail[name] = {"kind": kind, "not_run": why}
    return {"metrics_by_name": by_name, "metrics_detail": detail}


def build_dataset(scenario: Scenario, config: Config) -> dict[str, Any]:
    """Generates a complete mock deployment dataset.

    Args:
        scenario: Deployment scenario parameters.
        config: Effective AQA configuration.

    Returns:
        Complete mock dataset dictionary containing runs, insights, occurrences,
        root causes, configuration, goal, memories, and source metadata.
    """
    rng = random.Random(scenario.seed)  # noqa: S311 - seeded for deterministic mock data
    cadence = scenario.cadence_seconds
    total_runs = scenario.days * scenario.runs_per_day
    metric_count = len(config.multi_turn_metrics) + len(
        config.single_turn_metrics
    )
    budget = (
        max(1, config.data_evaluation_cap // metric_count)
        if metric_count
        else 0
    )

    # Cadence-aligned tumbling buckets, exactly as `derive_window_and_budget`
    # lays them out for an ambient run: the newest window ends at the last
    # cadence boundary at or before the anchor.
    last_end = dt.datetime.fromtimestamp(
        (int(scenario.anchor.timestamp()) // cadence) * cadence, tz=dt.UTC
    )

    # A sweep that fails outright, and one whose trigger was redelivered. Both
    # end with no counters at all, which the dashboard has to render as "—"
    # rather than as a run that measured zero of everything.
    failed_index = total_runs - rng.randrange(9, 16)
    skipped_index = total_runs - rng.randrange(20, 30)
    # One sweep in the middle of the incident loses rubrics to a clustering error.
    broken_clustering_index = (
        total_runs - int(scenario.incident_days_ago * scenario.runs_per_day) + 1
    )
    # It has to land on a sweep that ran: the failed and skipped ones record no
    # counters, so a collision would drop the only `rubrics_errored` there is.
    while broken_clustering_index in (
        failed_index,
        skipped_index,
        total_runs - 1,
    ):
        broken_clustering_index -= 1
    # The newest sweep is still running, so the one before it is the newest that
    # produced findings -- and the only one that sees the `guaranteed` defect.
    newest_sweep_index = total_runs - 2

    runs: list[dict[str, Any]] = []
    occurrences: list[InsightOccurrence] = []
    insights: dict[str, Insight] = {}
    seen_traces: dict[str, int] = {}
    seen_occurrences: dict[str, int] = {}

    for index in range(total_runs):
        window_end = last_end - dt.timedelta(
            seconds=cadence * (total_runs - 1 - index)
        )
        window_start = window_end - dt.timedelta(seconds=cadence)
        days_ago = (scenario.anchor - window_end).total_seconds() / 86400

        record, clusterable = _build_run_record(
            rng,
            scenario=scenario,
            config=config,
            index=index,
            total_runs=total_runs,
            window_start=window_start,
            window_end=window_end,
            days_ago=days_ago,
            budget=budget,
            metric_count=metric_count,
            failed_index=failed_index,
            skipped_index=skipped_index,
            broken_clustering_index=broken_clustering_index,
        )

        # Findings: which defects this sweep saw, and how they land against the
        # insights earlier sweeps recorded. This is the only place an insight is
        # created, so the per-run `insights_created` and the insight table's
        # `created_at` histogram cannot disagree -- which matters, because the
        # bar chart can be drawn from either.
        if record.status is RunStatus.DONE:
            clusters = _pick_clusters_for_run(
                rng,
                scenario=scenario,
                days_ago=days_ago,
                counters=record.counters,
                rubrics_to_place=clusterable,
                is_newest_sweep=index == newest_sweep_index,
            )
            created = 0
            for defect, item_count, trace_count in clusters:
                is_new = defect.key not in insights
                insight_id = f"ins-{_compute_stable_id(defect.key)}"
                if is_new:
                    created += 1
                    insights[defect.key] = Insight(
                        insight_id=insight_id,
                        agent_name=config.observed_agent_name,
                        label=defect.label,
                        status=InsightStatus.NEW,
                        created_at=window_end,
                        updated_at=window_end,
                    )
                else:
                    stored = insights[defect.key]
                    insights[defect.key] = stored.model_copy(
                        update={
                            "updated_at": window_end,
                            "status": InsightStatus.RECURRING,
                        }
                    )
                seen_traces[defect.key] = (
                    seen_traces.get(defect.key, 0) + trace_count
                )
                seen_occurrences[defect.key] = (
                    seen_occurrences.get(defect.key, 0) + 1
                )
                occurrences.append(
                    _build_occurrence(
                        rng,
                        defect=defect,
                        insight_id=insight_id,
                        run_id=record.run_id,
                        created_at=window_end,
                        agent_name=config.observed_agent_name,
                        item_count=item_count,
                        trace_count=trace_count,
                        verified_rate=scenario.verified_rate,
                    )
                )
            record = record.model_copy(
                update={
                    "counters": record.counters.model_copy(
                        update={
                            "clusters_created": len(clusters),
                            "insights_created": created,
                            "insights_recurring": len(clusters) - created,
                        }
                    )
                }
            )
            record = record.model_copy(
                update={
                    "events": _build_progress_events(record, clusters, config)
                }
            )

        runs.append(format_run(record))

    root_causes = _build_root_causes(occurrences, rate=scenario.root_cause_rate)
    views = _build_insight_views(
        insights,
        occurrences=occurrences,
        occurrence_counts=seen_occurrences,
        trace_counts=seen_traces,
        anchor=scenario.anchor,
        auto_resolve_days=config.insights_auto_resolve_days,
        cadence_seconds=cadence,
        diagnosed={r.insight_id for r in root_causes},
    )

    return {
        "version": DATASET_VERSION,
        "generated_at": dt.datetime.now(tz=dt.UTC).isoformat(),
        "anchor": scenario.anchor.isoformat(),
        "scenario": {
            **{
                field.name: getattr(scenario, field.name)
                for field in dataclasses.fields(scenario)
                if field.name != "anchor"
            },
            "cadence_seconds": cadence,
        },
        "config": serialize_config(config),
        "runs": runs,
        "insights": [v.model_dump(mode="json") for v in views],
        "occurrences": [o.model_dump(mode="json") for o in occurrences],
        "root_causes": [r.model_dump(mode="json") for r in root_causes],
        # The two documents an operator maintains. In a deployment they are
        # objects in the jobs bucket; here they ride in the dataset so the mock
        # server can serve and mutate them.
        "goal": _GOAL,
        "memories": _build_memories(scenario.anchor),
        # The published source, as its manifest summarizes it. None when nothing
        # is published, which is the Source card's other state.
        "source": _build_source_snapshot(scenario),
    }


_NO_TELEMETRY_ERROR = (
    "400 Field name service_version does not exist in STRUCT<"
    "gen_ai_conversation_id STRING, gen_ai_agent_name STRING, "
    "gen_ai_input_messages_ref STRING, gen_ai_output_messages_ref STRING> "
    "at [28:24]; reason: invalidQuery, location: query "
    "Location: us-east1 Job ID: ec82f7d4-4840-4792-b96e-986cafe20481"
)
"""What a sweep of an agent with no exported telemetry fails with.

Quoted from b/563290003 rather than paraphrased: the dashboard decides whether
a failure is really an empty deployment by reading this text, so a fixture that
only resembles it would pass while the real message did not.
"""


def _build_run_record(
    rng: random.Random,
    *,
    scenario: Scenario,
    config: Config,
    index: int,
    total_runs: int,
    window_start: dt.datetime,
    window_end: dt.datetime,
    days_ago: float,
    budget: int,
    metric_count: int,
    failed_index: int,
    skipped_index: int,
    broken_clustering_index: int,
) -> tuple[InvestigationRecord, int]:
    """Builds a single investigation run record with lifecycle events and counters.

    Args:
        rng: Seeded random number generator.
        scenario: Deployment scenario parameters.
        config: Effective AQA configuration.
        index: Index of the run within the total generated run sequence.
        total_runs: Total count of runs being generated.
        window_start: Telemetry evaluation window start timestamp.
        window_end: Telemetry evaluation window end timestamp.
        days_ago: Elapsed days from scenario anchor to window end.
        budget: Evaluation trace budget per metric.
        metric_count: Number of configured evaluation metrics.
        failed_index: Run index selected to simulate a failed sweep.
        skipped_index: Run index selected to simulate a skipped sweep.
        broken_clustering_index: Run index selected to simulate a clustering failure.

    Returns:
        Tuple of `(InvestigationRecord, unallocated_failed_rubrics_count)`.
    """
    scanned = max(
        0,
        int(
            scenario.traces_per_hour
            * (scenario.cadence_seconds / 3600)
            * _compute_traffic_factor(window_end)
            * rng.uniform(0.75, 1.3)
        ),
    )
    # Submitted a moment after the window closes, as the ambient tick does.
    created_at = window_end + dt.timedelta(seconds=rng.uniform(4, 40))
    # Annotated because it is splatted into `InvestigationRecord` below: the
    # inferred value type is a union, and a checker cannot match that union back
    # up to the individual fields each key lands in.
    common: dict[str, Any] = {
        "run_id": uuid.UUID(int=rng.getrandbits(128)).hex[:8],
        "created_at": created_at.isoformat(),
        "updated_at": created_at.isoformat(),
        "observed_agent_name": config.observed_agent_name,
        "trigger_type": "scheduled",
        "window_start": window_start.isoformat(),
        "window_end": window_end.isoformat(),
        "budget_per_metric": budget,
        "metrics": {
            "MULTI_TURN": list(config.multi_turn_metrics),
            "SINGLE_TURN": list(config.single_turn_metrics),
        },
        "idempotency_key": f"{config.observed_agent_name}:{int(window_end.timestamp())}",
    }

    # Every sweep, including the newest: the read that fails is the first one,
    # so no run ever reaches a stage that could make this sweep differ from the
    # one before it. A column of identical failures is the whole scenario.
    if scenario.no_telemetry:
        finished = created_at + dt.timedelta(seconds=rng.uniform(40, 90))
        return InvestigationRecord(
            **common,
            status=RunStatus.FAILED,
            finished_at=finished.isoformat(),
            error=_NO_TELEMETRY_ERROR,
        ), 0

    # The newest sweep is still going: it has events but no summary and no
    # counters, which is what the detail panel's live progress view is for.
    if index == total_runs - 1:
        return InvestigationRecord(
            **common,
            status=RunStatus.RUNNING,
            events=[
                InvestigationEvent(
                    created_at=(
                        created_at + dt.timedelta(seconds=3)
                    ).isoformat(),
                    text=_render_init_event(
                        config, window_start, window_end, budget
                    ),
                    source="init",
                )
            ],
        ), 0

    if index == failed_index:
        finished = created_at + dt.timedelta(seconds=rng.uniform(20, 60))
        return InvestigationRecord(
            **common,
            status=RunStatus.FAILED,
            finished_at=finished.isoformat(),
            error=(
                "google.api_core.exceptions.ResourceExhausted: 429 Quota exceeded "
                "for aiplatform.googleapis.com/online_prediction_requests_per_"
                "base_model with base model: gemini-3.1-pro-preview."
            ),
        ), 0

    if index == skipped_index:
        # A redelivered trigger: it claimed no idempotency marker, so it exited
        # before the graph and recorded nothing.
        finished = created_at + dt.timedelta(seconds=rng.uniform(1, 4))
        redelivered: dict[str, Any] = {**common, "trigger_type": "task_fire"}
        return InvestigationRecord(
            **redelivered,
            status=RunStatus.SKIPPED,
            finished_at=finished.isoformat(),
        ), 0

    in_incident = days_ago <= scenario.incident_days_ago
    counters, failed_rubrics = _build_funnel_counters(
        rng,
        scanned=scanned,
        # The fetcher pulls at most `budget_per_metric` traces per scope, so
        # that -- not the raw evaluation cap -- is where sampling bites.
        cap=budget,
        metric_count=metric_count,
        # A flat override skips the jitter as well as the incident shape: asking
        # for a deployment where nothing fails and getting a few failures anyway
        # would defeat the point of the knob.
        failure_rate=(
            scenario.failure_rate
            if scenario.failure_rate is not None
            else (
                scenario.incident_failure_rate
                if in_incident
                else scenario.baseline_failure_rate
            )
            * rng.uniform(0.8, 1.2)
        ),
        drop_rate=scenario.drop_rate,
        eval_error_rate=0.012 if rng.random() > 0.15 else 0.06,
        clustering_broke=index == broken_clustering_index,
    )
    # Long enough to be worth the elapsed column, and longer when there is more
    # to evaluate.
    elapsed = 90 + counters.traces_evaluated * rng.uniform(0.7, 1.6)
    finished_at = created_at + dt.timedelta(seconds=elapsed)
    return InvestigationRecord(
        **common,
        status=RunStatus.DONE,
        finished_at=finished_at.isoformat(),
        counters=counters,
        summary={
            "observed_agent_name": config.observed_agent_name,
            "window_start": window_start.isoformat(),
            "window_end": window_end.isoformat(),
            "budget_per_metric": budget,
            "multi_turn_metrics": list(config.multi_turn_metrics),
            "single_turn_metrics": list(config.single_turn_metrics),
            "multi_turn_pages_evaluated": max(
                1, counters.traces_evaluated // 25
            ),
            "single_turn_pages_evaluated": 0,
            # Per-(case, metric) outcomes: every evaluated trace is judged by
            # every metric, so these are the trace tallies times the metrics.
            "metrics_passed": counters.traces_eval_passed * metric_count,
            "metrics_failed": counters.traces_eval_failed * metric_count,
            "metrics_errored": counters.traces_eval_errored * metric_count,
            **_build_custom_metric_summary(counters, config.metrics_gcs_bucket),
        },
    ), max(
        0,
        failed_rubrics
        - counters.rubrics_errored
        - counters.rubrics_unclustered,
    )


def _pick_clusters_for_run(
    rng: random.Random,
    *,
    scenario: Scenario,
    days_ago: float,
    counters: InvestigationCounters,
    rubrics_to_place: int,
    is_newest_sweep: bool,
) -> list[tuple[Defect, int, int]]:
    """Selects defects observed in a sweep and distributes failed rubrics among them.

    Args:
        rng: Seeded random number generator.
        scenario: Deployment scenario parameters.
        days_ago: Elapsed days from scenario anchor to sweep completion.
        counters: Investigation counters for the sweep.
        rubrics_to_place: Count of failed rubrics to allocate to clusters.
        is_newest_sweep: Whether this is the most recent completed sweep.

    Returns:
        List of tuples `(defect, item_count, trace_count)` for each cluster.
    """
    if rubrics_to_place <= 0 or counters.traces_eval_failed <= 0:
        return []

    active = [
        d for d in DEFECTS if _is_active(d, days_ago, scenario, is_newest_sweep)
    ]
    if not active:
        return []

    # More failing rubrics means more distinct issues behind them, but a sweep
    # never invents more clusters than it has rubrics to fill them with.
    wanted = min(len(active), rubrics_to_place, 1 + int(rubrics_to_place / 4))
    chosen: list[Defect] = [d for d in active if d.guaranteed][:wanted]
    pool = [d for d in active if d not in chosen]
    weights = [d.weight for d in pool]
    for _ in range(wanted - len(chosen)):
        pick = rng.choices(pool, weights=weights, k=1)[0]
        chosen.append(pick)
        position = pool.index(pick)
        pool.pop(position)
        weights.pop(position)

    shares = _split(rng, rubrics_to_place, len(chosen))
    clusters: list[tuple[Defect, int, int]] = []
    for defect, item_count in zip(chosen, shares, strict=True):
        if item_count <= 0:
            continue
        # A defect usually breaks a couple of rubrics in the same conversation,
        # so it spans fewer traces than it has rubrics -- and never more traces
        # than the sweep saw fail.
        trace_count = max(
            1,
            min(
                counters.traces_eval_failed,
                item_count,
                int(item_count / rng.uniform(1.0, 2.2)) or 1,
            ),
        )
        clusters.append((defect, item_count, trace_count))
    return clusters


def _is_active(
    defect: Defect, days_ago: float, scenario: Scenario, is_newest_sweep: bool
) -> bool:
    """Checks whether a defect is eligible to occur in a historical sweep window.

    Args:
        defect: Candidate defect definition.
        days_ago: Elapsed days from the anchor timestamp.
        scenario: Deployment scenario parameters.
        is_newest_sweep: Whether evaluating the newest completed sweep.

    Returns:
        True if the defect can be sampled for the sweep.
    """
    if defect.guaranteed:
        # Its whole purpose is to be a first sighting, so it belongs to exactly
        # one sweep -- pinning it by a day offset would make that depend on
        # where the anchor happens to fall inside the cadence.
        return is_newest_sweep
    starts = defect.starts_days_ago
    if starts is None and defect.key.startswith("regression_"):
        starts = scenario.incident_days_ago
    if starts is not None and days_ago > starts:
        return False  # the regression had not landed yet
    return not (
        defect.ends_days_ago is not None and days_ago < defect.ends_days_ago
    )


def _split(rng: random.Random, total: int, parts: int) -> list[int]:
    """Splits an integer total into positive integer shares, ordered descending.

    Args:
        rng: Seeded random number generator.
        total: Total value to partition.
        parts: Number of partitions to create.

    Returns:
        List of partition sizes summing to `total`.
    """
    if parts <= 1:
        return [total]
    cuts = sorted(
        rng.sample(range(1, max(2, total)), min(parts - 1, max(1, total - 1)))
    )
    shares, previous = [], 0
    for cut in cuts:
        shares.append(cut - previous)
        previous = cut
    shares.append(total - previous)
    shares += [0] * (parts - len(shares))
    return sorted(shares, reverse=True)


def _build_occurrence(
    rng: random.Random,
    *,
    defect: Defect,
    insight_id: str,
    run_id: str,
    created_at: dt.datetime,
    agent_name: str,
    item_count: int,
    trace_count: int,
    verified_rate: float,
) -> InsightOccurrence:
    """Builds an occurrence sighting with representative rubric examples.

    Args:
        rng: Seeded random number generator.
        defect: Observed defect definition.
        insight_id: Parent insight identifier.
        run_id: Investigation run identifier where sighting occurred.
        created_at: Timestamp when sighting was recorded.
        agent_name: Name of the observed agent.
        item_count: Total number of failed rubrics in this cluster.
        trace_count: Total number of distinct traces exhibiting this defect.
        verified_rate: Probability of generating cluster-verification analysis.

    Returns:
        Populated `InsightOccurrence` instance.
    """
    trajectory_ids = [
        f"case-{rng.randrange(16**6):06x}" for _ in range(trace_count)
    ]
    traces: dict[str, list[dict]] = {}
    examples = []
    for _ordinal in range(min(3, item_count)):
        case_id = rng.choice(trajectory_ids)
        if case_id not in traces:
            traces[case_id] = _build_trace(rng, defect, created_at)
        examples.append(
            RubricExample(
                rubric=Finding(
                    expected_behavior=rng.choice(defect.expected),
                    actual_behavior=rng.choice(defect.actual).format(
                        case=case_id
                    ),
                    session_id=case_id,
                ),
                eval_case_id=case_id,
                trace=traces[case_id],
            )
        )
    # Off a stream of its own, keyed to this sighting, so that `verified_rate`
    # changes which sightings carry an analysis and nothing else. Drawing from
    # `rng` would shift every value after it -- the runs and insights stayed
    # identical but a case id sampled into one run's event prose did not, which
    # is exactly the kind of difference that wastes an afternoon. One seed now
    # yields one deployment at any rate, so its diagnosed and undiagnosed
    # renderings can be put side by side.
    verified_rng = random.Random(f"{insight_id}:{run_id}:verification")  # noqa: S311 - seeded for deterministic mock data
    analyses = (
        {
            VERIFICATION_ANALYSIS_KEY: _build_verification(
                verified_rng,
                defect=defect,
                trace_count=trace_count,
                created_at=created_at,
            )
        }
        if verified_rng.random() < verified_rate
        else {}
    )
    return InsightOccurrence(
        occurrence_id=f"occ-{uuid.UUID(int=rng.getrandbits(128)).hex[:12]}",
        insight_id=insight_id,
        occurrence_state=OccurrenceState.TRACKED,
        run_id=run_id,
        created_at=created_at,
        agent_name=agent_name,
        label=defect.label,
        item_count=item_count,
        trace_count=trace_count,
        trajectory_ids=trajectory_ids,
        analyses=analyses,
        rubrics=examples,
    )


def _build_verification(
    rng: random.Random,
    *,
    defect: Defect,
    trace_count: int,
    created_at: dt.datetime,
) -> ClusterVerification:
    """Builds a cluster-verification analysis record for a defect sighting.

    Args:
        rng: Seeded random number generator.
        defect: Observed defect definition.
        trace_count: Number of conversation traces exhibiting the defect.
        created_at: Verification timestamp.

    Returns:
        Populated `ClusterVerification` instance.
    """
    return ClusterVerification(
        model="gemini-3.7-flash",
        valid=True,
        explanation=f"The agent {defect.label}. {rng.choice(defect.expected)}",
        rationale=(
            f"Read {trace_count} conversation(s) in this cluster; every one of "
            f"them showed the same departure, so it is the cluster's defect "
            f"rather than one conversation's."
        ),
        refined_label=defect.label,
        created_at=created_at,
    )


def _build_root_causes(
    occurrences: list[InsightOccurrence], *, rate: float
) -> list[RootCause]:
    """Generates synthetic root-cause records against the latest occurrence of sampled insights.

    Uses a deterministic PRNG seeded per insight ID to keep selection stable across runs.

    Args:
        occurrences: List of generated `InsightOccurrence` objects.
        rate: Probability (0.0 to 1.0) of generating a root cause for each insight.

    Returns:
        List of generated `RootCause` instances.
    """
    if rate <= 0:
        return []
    newest: dict[str, InsightOccurrence] = {}
    for occurrence in occurrences:
        current = newest.get(occurrence.insight_id)
        if current is None or occurrence.created_at > current.created_at:
            newest[occurrence.insight_id] = occurrence

    records = []
    for insight_id, occurrence in sorted(newest.items()):
        rng = random.Random(f"{insight_id}:root-cause")  # noqa: S311 - seeded for deterministic mock data
        if rng.random() >= rate:
            continue
        records.append(
            RootCause(
                root_cause_id=uuid.UUID(int=rng.getrandbits(128)).hex,
                insight_id=insight_id,
                occurrence_id=occurrence.occurrence_id,
                agent_revision=occurrence.agent_revision,
                summary=(
                    f"The agent {occurrence.label} because the instruction "
                    f"covering that step is stated as a preference rather than "
                    f"a constraint, so the model treats it as optional."
                ),
                edits=[
                    ProposedEdit(
                        path="app/agent.py",
                        start_line=42,
                        end_line=44,
                        before=(
                            "    instruction=(\n"
                            '        "Help the user with their request."\n'
                            "    ),\n"
                        ),
                        after=(
                            "    instruction=(\n"
                            '        "Help the user with their request. "\n'
                            f'        "You must not {occurrence.label}."\n'
                            "    ),\n"
                        ),
                        rationale="States the missing step as a constraint.",
                    )
                ],
                created_at=occurrence.created_at + dt.timedelta(hours=1),
            )
        )
    return records


def _build_trace(
    rng: random.Random, defect: Defect, when: dt.datetime
) -> list[dict]:
    """Builds a conversation turn trace exhibiting the specified defect.

    Args:
        rng: Seeded random number generator.
        defect: Defect definition providing provocative user prompts.
        when: Timestamp for the simulated turn events.

    Returns:
        List of serialized `ConversationTurn` dictionaries.
    """
    prompt = rng.choice(defect.prompts)
    turns = [
        ConversationTurn(
            turn_index=0,
            events=[
                AgentEvent(
                    author="user",
                    content={"role": "user", "parts": [{"text": prompt}]},
                    event_time=when,
                ),
                AgentEvent(
                    author="travel_desk_agent",
                    content={
                        "role": "model",
                        "parts": [{"text": "Let me check that for you."}],
                    },
                    event_time=when + dt.timedelta(seconds=1),
                ),
            ],
        ),
        ConversationTurn(
            turn_index=1,
            events=[
                AgentEvent(
                    author="travel_desk_agent",
                    content={
                        "role": "model",
                        "parts": [{"text": "All set -- anything else?"}],
                    },
                    event_time=when + dt.timedelta(seconds=4),
                )
            ],
        ),
    ]
    return [t.model_dump(mode="json", exclude_none=True) for t in turns]


def _build_insight_views(
    insights: dict[str, Insight],
    *,
    occurrences: list[InsightOccurrence],
    occurrence_counts: dict[str, int],
    trace_counts: dict[str, int],
    anchor: dt.datetime,
    auto_resolve_days: int,
    cadence_seconds: int,
    diagnosed: set[str] | None = None,
) -> list[InsightView]:
    """Assembles read-view `InsightView` objects from insights and occurrences.

    Auto-resolution is evaluated across historical windows. The sweep that detects
    the stale state sets ``resolved_at``, as `resolve_stale_insights` does,
    exercising recorded resolution dates.

    Args:
        insights: Map of defect keys to stored `Insight` objects.
        occurrences: List of all generated `InsightOccurrence` sightings.
        occurrence_counts: Total occurrence count per defect key.
        trace_counts: Total affected trace count per defect key.
        anchor: Reference timestamp for the dataset.
        auto_resolve_days: Stale duration threshold before auto-resolving an insight.
        cadence_seconds: Investigation cadence in seconds.
        diagnosed: Set of insight IDs that have recorded root causes.

    Returns:
        List of `InsightView` records sorted by `updated_at` descending.
    """
    last_run = {}
    for occurrence in occurrences:
        last_run[occurrence.insight_id] = (
            occurrence.run_id,
            occurrence.created_at,
        )

    views = []
    for key, insight in insights.items():
        stale_days = (anchor - insight.updated_at).total_seconds() / 86400
        status = insight.status
        resolved_at = None
        if auto_resolve_days and stale_days > auto_resolve_days:
            status = InsightStatus.RESOLVED
            # The first sweep after the issue went stale is the one that would
            # have resolved it, so that tick is the date recorded -- not the
            # instant the window elapsed, which no sweep was running at.
            went_stale = insight.updated_at + dt.timedelta(
                days=auto_resolve_days
            )
            resolved_at = _compute_next_sweep_after(
                went_stale, anchor, cadence_seconds
            )
        run_id, run_at = last_run.get(insight.insight_id, (None, None))
        views.append(
            InsightView(
                **insight.model_dump(exclude={"status", "resolved_at"}),
                status=status,
                resolved_at=resolved_at,
                occurrence_count=occurrence_counts.get(key, 0),
                trace_count=trace_counts.get(key, 0),
                last_run_id=run_id,
                last_run_at=run_at,
                has_root_cause=insight.insight_id in (diagnosed or frozenset()),
            )
        )
    # Newest sighting first, the order a triage list is read in.
    return sorted(views, key=lambda v: v.updated_at, reverse=True)


def _compute_next_sweep_after(
    when: dt.datetime, anchor: dt.datetime, cadence_seconds: int
) -> dt.datetime:
    """Computes the first cadence boundary at or after ``when``, capped at ``anchor``.

    Args:
        when: Timestamp to align forward to the next cadence tick.
        anchor: Reference timestamp bounding the maximum time.
        cadence_seconds: Cadence interval in seconds.

    Returns:
        Aligned cadence boundary timestamp.
    """
    tick = int(when.timestamp())
    aligned = -(-tick // cadence_seconds) * cadence_seconds
    return min(dt.datetime.fromtimestamp(aligned, tz=dt.UTC), anchor)


# --------------------------------------------------------------------------- #
# Progress events                                                              #
# --------------------------------------------------------------------------- #


def _render_init_event(
    config: Config,
    window_start: dt.datetime,
    window_end: dt.datetime,
    budget: int,
) -> str:
    """Renders the opening initialization event message in markdown.

    Args:
        config: Effective AQA configuration.
        window_start: Window start timestamp.
        window_end: Window end timestamp.
        budget: Evaluation budget per metric.

    Returns:
        Markdown-formatted initialization event text.
    """
    multi = ", ".join(f"`{m}`" for m in config.multi_turn_metrics) or "_none_"
    single = ", ".join(f"`{m}`" for m in config.single_turn_metrics) or "_none_"
    return (
        f"## Ambient Quality Agent\n\n"
        f"Investigating **`{config.observed_agent_name}`**.\n\n"
        f"- Window: `{window_start.isoformat()}` → `{window_end.isoformat()}`\n"
        f"- Budget per metric: `{budget}`\n"
        f"- Multi-turn metrics: {multi}\n"
        f"- Single-turn metrics: {single}\n"
    )


def _build_progress_events(
    record: InvestigationRecord,
    clusters: list[tuple[Defect, int, int]],
    config: Config,
) -> list[InvestigationEvent]:
    """Builds progress log events simulating pipeline stage completion.

    Args:
        record: Completed investigation run record.
        clusters: Allocated defect clusters for the sweep.
        config: Effective AQA configuration.

    Returns:
        List of chronological `InvestigationEvent` instances.
    """
    counters = record.counters
    started = dt.datetime.fromisoformat(record.created_at)
    texts: list[tuple[str, str]] = [
        (
            "init",
            _render_init_event(
                config,
                dt.datetime.fromisoformat(
                    record.window_start or record.created_at
                ),
                dt.datetime.fromisoformat(
                    record.window_end or record.created_at
                ),
                record.budget_per_metric,
            ),
        ),
        (
            "eval_multi_turn",
            f"**MULTI_TURN evaluation:** {counters.traces_evaluated} case(s) "
            f"evaluated.\n",
        ),
    ]
    if not config.single_turn_metrics:
        texts.append(
            (
                "eval_single_turn",
                "**SINGLE_TURN evaluation:** _Skipped (no metrics configured or "
                "zero budget)._\n",
            )
        )
    texts.append(
        ("insight_correlation", _render_correlation_summary(counters, clusters))
    )

    return [
        InvestigationEvent(
            created_at=(started + dt.timedelta(seconds=6 + 24 * i)).isoformat(),
            text=text,
            source=source,
        )
        for i, (source, text) in enumerate(texts)
    ]


def _render_correlation_summary(
    counters: InvestigationCounters, clusters: list[tuple[Defect, int, int]]
) -> str:
    """Renders the insight correlation progress summary in markdown.

    Args:
        counters: Investigation counters for the sweep.
        clusters: Allocated defect clusters with counts.

    Returns:
        Markdown summary string.
    """
    if not clusters:
        return "**Insights:** no failed rubrics to correlate.\n"
    total_rubrics = (
        sum(item for _, item, _ in clusters)
        + counters.rubrics_errored
        + counters.rubrics_unclustered
    )
    header = (
        f"**Insights:** {total_rubrics} failed rubric(s) grouped into "
        f"{len(clusters)} candidate issue(s): {counters.insights_created} new, "
        f"{counters.insights_recurring} recurring.\n"
    )
    skipped = ""
    if counters.rubrics_errored or counters.rubrics_unclustered:
        skipped = (
            f"**Insights -- skipped rubric(s):** {counters.rubrics_errored} errored, "
            f"{counters.rubrics_unclustered} unclustered (of {total_rubrics} total); "
            f"see the logs for details.\n"
        )
    lines = "".join(
        f"- _{defect.label}_ ({'new' if i < counters.insights_created else 'recurring'}): "
        f"{item_count} rubric(s) across {trace_count} trace(s); cases: "
        f"case-{_compute_stable_case_id(defect.key, item_count)}\n"
        for i, (defect, item_count, trace_count) in enumerate(clusters)
    )
    return header + skipped + lines


def _compute_stable_id(key: str) -> str:
    """Generates a deterministic 12-character hex ID derived from a defect key.

    Args:
        key: Defect identifier string.

    Returns:
        12-character hex string.
    """
    return uuid.uuid5(uuid.NAMESPACE_OID, key).hex[:12]


def _compute_stable_case_id(key: str, item_count: int) -> str:
    """Generates a deterministic 6-character hex case ID stable across processes.

    Args:
        key: Defect identifier string.
        item_count: Count of failed rubrics.

    Returns:
        6-character hex string.
    """
    return uuid.uuid5(uuid.NAMESPACE_OID, f"{key}:{item_count}").hex[:6]


# --------------------------------------------------------------------------- #
# CLI                                                                          #
# --------------------------------------------------------------------------- #


_GOAL = """The travel desk agent books travel and files expenses for employees,
from a chat request. It must use its tools for anything factual -- policy,
prices, employee records -- and never answer those from memory.

A turn is a defect when the agent invents a fact a tool could have told it,
calls a tool with a value outside its documented set, or reports success for a
call that failed."""


def _build_memories(anchor: dt.datetime) -> list[dict[str, Any]]:
    """Builds synthetic memories anchored to the reference timestamp.

    Args:
        anchor: Reference timestamp for the dataset.

    Returns:
        List of memory dictionaries, oldest first.
    """
    seeded = [
        (
            12,
            "chat-4f2a91c0",
            "The travel agent's system prompt is in "
            "`travel_agent/prompts/system.md`; the tool descriptions are in "
            "`travel_agent/tools.py`.",
        ),
        (
            7,
            "chat-9b7e03d1",
            "Failed bookings are the `agent_events` rows whose `event_type` is "
            "`TOOL_ERROR` and whose tool is `book_flight`.",
        ),
        (
            2,
            "chat-9b7e03d1",
            "Expense categories are case-sensitive: `Flight` is rejected where "
            "`Airfare` is accepted.",
        ),
    ]
    out = []
    for days_ago, source, text in seeded:
        when = anchor - dt.timedelta(days=days_ago)
        out.append(
            {
                # The derivation `documents.memories.compute_memory_id` uses,
                # so an id the dashboard deletes by matches what a real
                # deployment would have stored it under.
                "id": hashlib.sha256(text.strip().encode()).hexdigest()[:12],
                "text": text,
                "source": source,
                "created_at": when.isoformat(),
            }
        )
    return out


MAX_FILE_BYTES = 1024 * 1024
MAX_SNAPSHOT_BYTES = 50 * 1024 * 1024
"""The publisher's caps. Hand-copies of `source_code.snapshot`, held level by
`tools/test_mock_aqa.py` -- a summary generated under different caps would put
the card's amber "truncated" note at a size a deployment never reaches."""

_SOURCE_IGNORED = (
    ".git",
    "node_modules",
    "__pycache__",
    ".venv",
    "dist",
    "build",
)
"""Directories `--source-dir` skips. Not the publisher's `.gcloudignore` reader:
the point is a summary of real code, not a second implementation of packaging."""


def _build_source_snapshot(scenario: Scenario) -> dict[str, Any] | None:
    """Builds the manifest summary dictionary for the Source card.

    Args:
        scenario: Deployment scenario parameters.

    Returns:
        Manifest summary dictionary, or None if source publishing is disabled.
    """
    if not scenario.source_published:
        return None
    rng = random.Random(f"{scenario.seed}:source")  # noqa: S311 - seeded for deterministic mock data
    # Sparse and non-contiguous, as Agent Runtime's revision ids are and
    # as the bucket's retention rule leaves them.
    kept = rng.randint(3, 6)
    newest = rng.randint(8, 40)
    published = scenario.anchor - dt.timedelta(hours=rng.uniform(2, 50))
    summary = {
        "available": True,
        "revision": str(newest),
        "revision_count": kept,
        "created_at": published.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "agent_directory": "app",
        "file_count": rng.randint(90, 240),
        "total_bytes": rng.randint(400_000, 3_500_000),
        "truncated_files": 0,
        "omitted_files": 0,
    }
    if scenario.source_dir:
        summary.update(_walk_source(pathlib.Path(scenario.source_dir)))
    return summary


def _walk_source(root: pathlib.Path) -> dict[str, Any]:
    """Scans a local repository directory applying size and truncation caps.

    Args:
        root: Root directory path of the codebase to inspect.

    Returns:
        Dictionary containing file counts, byte totals, and truncation metrics.
    """
    total = truncated = omitted = count = 0
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.is_symlink():
            continue
        if any(
            part in _SOURCE_IGNORED or part.startswith(".")
            for part in path.parts
        ):
            continue
        try:
            size = path.stat().st_size
        except OSError:
            omitted += 1
            continue
        if total >= MAX_SNAPSHOT_BYTES:
            omitted += 1
            continue
        if size > MAX_FILE_BYTES:
            size = MAX_FILE_BYTES
            truncated += 1
        total += size
        count += 1
    return {
        "agent_directory": root.name,
        "file_count": count,
        "total_bytes": total,
        "truncated_files": truncated,
        "omitted_files": omitted,
    }


def _render_diagnosed_note(occurrences: list[dict]) -> str:
    """Formats a diagnostic summary note indicating how many sightings were verified.

    Args:
        occurrences: List of occurrence dictionaries.

    Returns:
        Formatted summary clause, or an empty string if none are diagnosed.
    """
    diagnosed = sum(1 for o in occurrences if o.get("analyses"))
    return f", {diagnosed} of them diagnosed" if diagnosed else ""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Generate a mock AQA deployment for the dashboard to draw."
    )
    parser.add_argument(
        "--out",
        default="scratch/mock_aqa.json",
        help="where to write the dataset (default: %(default)s).",
    )
    parser.add_argument(
        "--days", type=int, default=30, help="history depth in days."
    )
    parser.add_argument(
        "--runs-per-day",
        type=int,
        default=4,
        help="ambient sweeps per day; also fixes the cadence (default: %(default)s).",
    )
    parser.add_argument("--seed", type=int, default=7, help="RNG seed.")
    parser.add_argument(
        "--agent",
        default=os.environ["AQA_OBSERVED_AGENT_NAME"],
        help="observed agent name (default: %(default)s).",
    )
    parser.add_argument(
        "--anchor",
        default="",
        help=(
            "ISO timestamp the history ends at; default now, so the last 7 days "
            "are always the 7 days before you look."
        ),
    )
    parser.add_argument(
        "--incident-days-ago",
        type=float,
        default=3.0,
        help="when the seeded regression landed (default: %(default)s).",
    )
    # The two knobs that generate a *degenerate* deployment, which the dashboard
    # has to render as well as a busy one -- and which the real deployment will
    # not produce on demand.
    parser.add_argument(
        "--failure-rate",
        type=float,
        default=None,
        help=(
            "flat share of evaluated traces that fail, overriding the baseline "
            "and the incident spike; 0 generates a deployment with no failures, "
            "no clusters and no insights (default: the incident shape)."
        ),
    )
    parser.add_argument(
        "--verified-rate",
        type=float,
        default=0.0,
        help=(
            "share of sightings carrying a cluster-verification result, which "
            "is what the dashboard renders as a diagnosis; 0 matches a real "
            "deployment, where nothing calls the pass yet (default: %(default)s)."
        ),
    )
    parser.add_argument(
        "--root-cause-rate",
        type=float,
        default=0.0,
        help=(
            "share of insights carrying a recorded root cause; 0 matches a "
            "fresh deployment, where no RCA turn has run yet "
            "(default: %(default)s)."
        ),
    )
    parser.add_argument(
        "--drop-rate",
        type=float,
        default=0.02,
        help=(
            "share of sampled traces lost before ingestion; 1 generates a "
            "deployment where nothing is ever ingested (default: %(default)s)."
        ),
    )
    parser.add_argument(
        "--source-dir",
        default="",
        help=(
            "a real directory for the Source card to summarize -- its file "
            "count and size, and anything the publisher's caps would truncate "
            "or omit (default: a synthesized snapshot)."
        ),
    )
    parser.add_argument(
        "--no-source",
        action="store_true",
        help=(
            "generate a deployment that has published no source snapshot, "
            "which is the Source card's amber state."
        ),
    )
    parser.add_argument(
        "--no-telemetry",
        action="store_true",
        help=(
            "generate a deployment whose agent has exported no telemetry, so "
            "every sweep fails its first read and nothing downstream runs -- "
            "the state a new customer starts in (b/563290003)."
        ),
    )
    parser.add_argument(
        "--compact",
        action="store_true",
        help="write minified JSON (smaller file).",
    )
    args = parser.parse_args(argv)

    if args.days < 1 or args.runs_per_day < 1:
        parser.error("--days and --runs-per-day must both be >= 1")
    if args.source_dir and not pathlib.Path(args.source_dir).is_dir():
        parser.error(f"--source-dir is not a directory: {args.source_dir}")
    for name, rate in (
        ("--failure-rate", args.failure_rate),
        ("--drop-rate", args.drop_rate),
    ):
        if rate is not None and not 0 <= rate <= 1:
            parser.error(f"{name} must be between 0 and 1")

    anchor = (
        dt.datetime.fromisoformat(args.anchor).astimezone(dt.UTC)
        if args.anchor
        else dt.datetime.now(tz=dt.UTC)
    )
    scenario = Scenario(
        days=args.days,
        runs_per_day=args.runs_per_day,
        seed=args.seed,
        anchor=anchor,
        incident_days_ago=args.incident_days_ago,
        failure_rate=args.failure_rate,
        verified_rate=args.verified_rate,
        root_cause_rate=args.root_cause_rate,
        drop_rate=args.drop_rate,
        source_published=not args.no_source,
        source_dir=args.source_dir,
        no_telemetry=args.no_telemetry,
    )
    # `load()` re-reads the env, so --agent takes effect without the caller
    # having to export anything.
    os.environ["AQA_OBSERVED_AGENT_NAME"] = args.agent
    dataset = build_dataset(
        scenario,
        dataclasses.replace(
            load(), metrics_gcs_bucket="mock-aqa-project-aqua-metrics"
        ),
    )

    out = pathlib.Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(
        json.dumps(
            dataset,
            separators=(",", ":") if args.compact else None,
            indent=None if args.compact else 1,
        ),
        encoding="utf-8",
    )

    totals = _sum_counters(dataset["runs"])
    print(
        f"Wrote {out} ({out.stat().st_size / 1024:.0f} KiB)\n"
        f"  {len(dataset['runs'])} investigations over {args.days} day(s), "
        f"{args.runs_per_day}/day\n"
        f"  {totals['traces_scanned']:,} traces scanned, "
        f"{totals['traces_evaluated']:,} evaluated, "
        f"{totals['traces_eval_failed']:,} failed\n"
        f"  {len(dataset['insights'])} insights, "
        f"{len(dataset['occurrences'])} occurrences"
        f"{_render_diagnosed_note(dataset['occurrences'])}, "
        f"{len(dataset['root_causes'])} root causes\n"
        f"Serve it:  python tools/mock_aqa_server.py --dataset {out}",
        file=sys.stderr,
    )
    return 0


def _sum_counters(runs: list[dict[str, Any]]) -> dict[str, int]:
    """Sums counter values across runs matching `InvestigationStore.sum_counters`.

    Args:
        runs: List of serialized run dictionaries.

    Returns:
        Dictionary mapping counter field names to aggregate totals.
    """
    totals = dict.fromkeys(InvestigationCounters.model_fields, 0)
    for run in runs:
        for name, value in (run.get("counters") or {}).items():
            totals[name] = totals.get(name, 0) + int(value or 0)
    return totals


if __name__ == "__main__":
    raise SystemExit(main())
