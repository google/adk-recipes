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

"""Which trajectories an investigation reviews, and how to count them back up.

An investigation reviews at most its budget of trajectories (the ``@limit`` an
ingestion query binds) out of the candidates in its window: every trajectory
of the observed agent in the window, or every trajectory a selector matched.
Under `SamplingMethod.WEIGHTED`, a trajectory that shows signs of a defect is
picked more often than one that does not, every candidate keeps a chance of
being picked, and every picked trajectory carries its probability of being
picked, ``p``. A figure about all traffic counts each reviewed trajectory as
``1 / p`` trajectories, whose sum estimates a count without bias. The sweep
scales those counts so that they sum to the candidates
(`_sweep.compute_trajectories_represented`), which removes the noise of a
weighted sample's random threshold at the cost of a small bias. An estimated
rate divides one sum by another, so it carries a small bias that shrinks as
the budget grows. Under `SamplingMethod.UNIFORM`, every candidate has the
same chance, ``p = min(1, budget / candidates)``.

Wiring::

    BigQueryFetcher, CloudOpsFetcher, CloudLoggingFetcher
        │ candidates SQL, measures SQL (per source)
        ▼
    build_weighted_target_ctes():
        Candidates ──┐
                     ├─► Scored ─► Keyed ─► Prioritized ─► targets
        Measures ────┘
    build_uniform_target_cte():
        Candidates ─► targets
        │
        ▼ "sampling" column on every ingestion row
    BigQueryJobFetcher
        └─ _map_page() ── parse_sampling_record() ──► Page.sampling

**1. The weight.** Every candidate starts at a weight of 1, and each risk
signal adds to it::

    added  = 4·has_error + 2·has_abnormal_finish + 1·has_tool_reported_error
             + 1·busy_turn_tail + 1·cost_tail + 1·latency_tail
    weight = 1 + added / max(1, average of added over the candidates)

- ``has_error``: the trajectory recorded an error, such as a tool error, a
  model error, an agent error or a run error.
- ``has_abnormal_finish``: a model call stopped for a reason other than a
  normal stop or a call to tools (`NORMAL_FINISH_REASONS`), such as running
  out of tokens, a safety block or a malformed function call. Such a call
  returns normally, so it records no error.
- ``has_tool_reported_error``: a tool returned a result that reports a
  failure: a non-empty ``error``, ``errors``, ``error_code`` or ``errorCode``
  field, ``is_error`` or ``isError`` true, or ``success`` false. Such a call
  does not raise, so it records no error event; each fetcher says how its
  source shows one. Cloud Logging records no tool calls, so there it scores
  0.
- ``busy_turn_tail``: the tail score of the most calls to one tool in any one
  turn, not counting ADK's ``transfer_to_agent``, which hands the turn to
  another agent rather than doing its work. Cloud Logging records no tool
  calls, so there it scores the most model calls in any one turn.
- ``cost_tail``: the tail score of the trajectory's total tokens.
- ``latency_tail``: the tail score of the duration of the trajectory's
  slowest turn. The slowest turn, not the sum of all turns: a sum grows with
  the number of turns and calls, which the busy-turn and cost signals already
  measure, while one long stall is something the user waited through.

Each fetcher defines these properties on its own source's fields, next to its
measures SQL. A signal whose field the source does not record evaluates to 0,
so a missing field never lowers a weight. Each picked trajectory keeps its measures,
their percentiles and its tail scores (`SamplingSignals`), so how it ranked
can be read without the cutoff that turned the ranks into weights.

**2. Why the weight adds to a base of 1.** The base keeps every trajectory
eligible, however clean it looks, and keeps part of the budget spread evenly
over the window, so defects that leave no signal are still found.

**3. Why these coefficients.** Each coefficient reflects how strongly its
signal predicts a defect worth reporting. The evidence is one day of a
production customer-service agent's traffic: 16,657 sessions whose reviewer
findings a verifier confirmed or rejected against the agent's code and
procedures. The busy-turn, cost and latency tails and a tool-reported error
each marked sessions with a confirmed high-severity defect about twice as
often as the others (1.8, 1.9, 1.75 and 2.0 times), so they share one
coefficient. At 1, replays of 1,000-session samples from that day reviewed
13% more sessions with a high-severity defect than uniform sampling, and the
estimated rates kept about 90% (any defect) and 97% (high-severity defects)
of the precision of a uniform sample. At 2, they reviewed 22% more such
sessions at 79% and 88% of the precision, and longer ones: the busy-turn,
cost and latency signals favor long sessions, so review prompts were 28%
larger than under uniform sampling, against 16% at 1.

- ``has_error`` (`ERROR_COEFFICIENT`, 4): an error is direct evidence that
  something failed. Provisional: those sessions recorded three errors, and
  failed sessions were rarer among them than in the agent's other traffic.
- ``has_abnormal_finish`` (`ABNORMAL_FINISH_COEFFICIENT`, 2): a model call
  that stopped early, was blocked or returned a malformed call usually
  indicates a problem, but some have benign causes, such as a long answer
  cut at the token limit. Provisional: that agent's plugin version records
  no finish reason.
- ``has_tool_reported_error``, ``busy_turn_tail``, ``cost_tail`` and
  ``latency_tail`` (`TOOL_REPORTED_ERROR_COEFFICIENT`,
  `BUSY_TURN_COEFFICIENT`, `COST_COEFFICIENT`, `LATENCY_COEFFICIENT`, 1
  each): measured as above. Fewer than a third of the sessions with one of
  these signals had a high-severity defect, and within sessions that already
  had another of them, each added less (1.1 to 1.4 times).

**4. Tail scores.** A tail score ranks a trajectory among the candidates by
one measure, such as total tokens. ``PERCENT_RANK()`` turns the rank into a
percentile from 0 (lowest) to 1 (highest), and the score is
``max(0, (percentile - 0.9) / (1 - 0.9))``: 0 up to the 90th percentile
(`TAIL_CUTOFF`), rising linearly to 1 at the top. Ranks rather than raw values
make the score work the same for an agent that uses 2,000 tokens a
conversation and one that uses 200,000, and keep one extreme outlier from
dominating every weight. Because ranks are relative to the candidates, a
change that raises every trajectory's cost equally changes no weight:
weighting decides which trajectories to review, it does not detect a shift
across the whole window. The hinge leaves the bulk of the window at 0, where
being above average carries little evidence of a problem; a score that rose
with every percentile would spread the added weight over most of the window
and dilute the top. Tied trajectories get the same percentile, so a measure
that is a small whole number, like calls to one tool, scores as a step. A
trajectory without the measure scores 0, and the others are ranked among
themselves, so a measure that most candidates lack does not put every one
that records it in the tail. Ranking takes memory that grows with the
candidates, so above `WEIGHTED_CANDIDATE_LIMIT` a fetcher samples uniformly.

The cutoff decides how many trajectories get a tail boost whatever the agent
does: with it at the 90th percentile, up to a tenth of the candidates get a
cost boost and up to a tenth a latency boost in every window (ties and missing
values give fewer), while an error boost goes only where an error happened. A
boost given to many trajectories moves a large part of the budget even when
each boost is small. On the agent of section 3, with the cutoff at the 90th
percentile the cost and latency boosts direct about 84 of every 1,000
reviews, and errors, tool-reported ones included, about 59; at the 80th
percentile, 152 against 54. Cutoffs of 0.8, 0.9 and 0.95 found about as many
high-severity defects there at equal precision, so the cutoff is the one
that reads most simply: the top tenth.

**5. The cap on the added weight.** The budget is fixed, so a trajectory with
no signal is picked at about ``1 / (average weight)`` of its uniform chance.
Without a limit that loss would grow with how common the signals are, and a
very common signal is also weaker evidence, being likely to mark one known
defect many times. When the candidates' average ``added`` is above 1
(`ADDED_WEIGHT_AVERAGE_LIMIT`), every ``added`` is divided by that average, so
that it averages 1. The average weight is then at most 2, so a trajectory with
no signal keeps at least half of its uniform chance; the order of trajectories
by weight and the ratios of their added weights do not change; and an agent
with typical signal rates, whose average ``added`` is well below 1, is
unaffected. A window without any signal has an average of 0, and every
weight is 1.

**6. Priority sampling.** Each candidate gets a random key ``u`` in (0, 1] and
a priority ``weight / u``. The query keeps the ``budget`` highest priorities
and takes the next highest, the (budget + 1)-th, as the threshold ``τ``. A
kept trajectory's probability of being picked, given ``τ``, is
``p = min(1, weight / τ)``, and ``p = 1`` when the candidates fit in the
budget. Summing ``1 / p`` over the sample estimates a count over all
candidates without bias (Duffield, Lund and Thorup, "Priority sampling for
estimation of arbitrary subset sums", 2007). A weight reads directly as a
relative chance: weight 5 is about five times as likely to be picked as weight
1, up to certainty. Efraimidis-Spirakis sampling would also allow an exact
estimate, but its chances grow more slowly than the weight, which makes a
weight harder to read.

``u`` comes from ``FARM_FINGERPRINT`` of the seed and the trajectory id,
reduced to 53 bits, rather than from ``RAND()``. An investigation's seed is its
id and the job's task attempt (`WorkflowState.sampling_seed`), and a preview
draws a fresh one. The same seed and candidates give the same sample, so a
run's sample can be drawn again when debugging;
ties in priority fall back to the id for the same reason. And BigQuery may
evaluate a common table expression once for each reference to it, where a
``RAND()`` key would differ between references and a hash does not.

**7. A worked example.** The figures below hold while the budget is small
compared with the candidates; take 10,000 candidates and a budget of 1,000.
Say 2% of them record an error and none has another signal. The average
``added`` is 0.08, below the cap's limit, so an error trajectory weighs 5 and
every other weighs 1. The average weight is 1.08, so an error trajectory is
picked at about 5 / 1.08, 4.6 times its uniform chance: about 9% of the sample
has an error, against 2% under uniform sampling, and every other trajectory
keeps about 1 / 1.08, 0.93 of its uniform chance. Raise `ERROR_COEFFICIENT` to
8 and an error weighs 9: errors fill about 16% of the sample, and the others
keep 0.86 of their uniform chance. If instead half the candidates record an
error, the average ``added`` is 2, and the cap halves every added weight: an
error trajectory weighs 3, errors fill about 75% of the sample, and the others
keep half of their uniform chance. Without the cap they would weigh 5, fill
about 83%, and the others would keep a third. Lowering `TAIL_CUTOFF` to 0.8
would give a cost boost and a latency boost to a fifth of the candidates each
instead of a tenth, taking that budget from trajectories with no signal.
"""

from __future__ import annotations

import enum
from collections.abc import Mapping
from typing import Any

from ambient_quality_agent.tools.ingestion.models import SamplingRecord
from ambient_quality_agent.tools.trajectories.models import SamplingSignals
from google.cloud import bigquery


class SamplingMethod(enum.StrEnum):
    """How an investigation picks the trajectories it reviews."""

    WEIGHTED = "weighted"
    """Trajectories with risk signals are picked more often; see the module
    docstring."""

    UNIFORM = "uniform"
    """Every candidate has the same chance."""


ERROR_COEFFICIENT = 4.0
"""Weight added for a trajectory that recorded an error. Provisional."""

ABNORMAL_FINISH_COEFFICIENT = 2.0
"""Weight added for a model call that stopped abnormally. Provisional."""

TOOL_REPORTED_ERROR_COEFFICIENT = 1.0
"""Weight added for a tool result that reports a failure. Measured on one
agent's labeled sessions; see the module docstring."""

BUSY_TURN_COEFFICIENT = 1.0
"""Weight added at the top of the busy-turn tail. Measured on one agent's
labeled sessions; see the module docstring."""

COST_COEFFICIENT = 1.0
"""Weight added at the top of the cost tail. Measured on one agent's labeled
sessions; see the module docstring."""

LATENCY_COEFFICIENT = 1.0
"""Weight added at the top of the latency tail. Measured on one agent's
labeled sessions; see the module docstring."""

TAIL_CUTOFF = 0.9
"""Percentile at which a tail score starts to rise above 0. See the module
docstring."""

ADDED_WEIGHT_AVERAGE_LIMIT = 1.0
"""Highest average added weight over the candidates before every added weight
is scaled down to it."""

WEIGHTED_CANDIDATE_LIMIT = 5_000_000
"""Most candidates a weighted sample ranks. Above it a fetcher samples
uniformly: ranking every candidate in one query ran out of memory between 10
and 15 million candidates with 32-character ids in a test, and passed twice
at 10 million."""

SAMPLING_COLUMN = "sampling"
"""Column of every ingestion row holding how the trajectory was picked: a
STRUCT of ``inclusion_probability`` and, under weighted sampling, ``weight``
and ``signals``."""

NORMAL_FINISH_REASONS = (
    "stop",
    "end_turn",
    "stop_sequence",
    "tool_call",
    "tool_calls",
    "tool_use",
    "function_call",
)
"""Finish reasons, compared in lowercase, of a model call that ended normally:
Gemini's ``STOP`` and the values OpenTelemetry, OpenAI and Anthropic use for a
completed answer or a hand-off to tools. Any other recorded reason is an
abnormal finish."""

MEASURE_COLUMNS = (
    "has_error",
    "has_abnormal_finish",
    "has_tool_reported_error",
    "busy_turn_calls",
    "total_tokens",
    "slowest_turn_ms",
)
"""Columns every source's measures SQL projects, besides the id column, and
which the weighted sample reads:

- ``has_error`` (BOOL): the trajectory recorded an error.
- ``has_abnormal_finish`` (BOOL): a model call stopped abnormally.
- ``has_tool_reported_error`` (BOOL): a tool reported a failure in its
  result; FALSE from a source that records no tool calls.
- ``busy_turn_calls`` (INT64): the most calls to one tool in one turn; from a
  source that records no tool calls, the most model calls in one turn. NULL
  without any call.
- ``total_tokens`` (INT64): tokens over the trajectory's model calls, NULL when
  none recorded its usage.
- ``slowest_turn_ms`` (INT64): duration of the slowest turn in milliseconds.
"""


def build_abnormal_finish_sql(reason: str) -> str:
    """Builds the test for a finish reason outside `NORMAL_FINISH_REASONS`.

    Args:
        reason: A STRING expression holding one finish reason.

    Returns:
        A BOOL expression, NULL where `reason` is NULL.
    """
    normal = ", ".join(f"'{value}'" for value in NORMAL_FINISH_REASONS)
    return f"LOWER({reason}) NOT IN ({normal})"


def _build_percentile_sql(column: str) -> str:
    """Builds the percentile of a measure among the candidates.

    Args:
        column: The measure, qualified by its alias.

    Returns:
        A FLOAT64 expression from 0 to 1 among the candidates that record the
        measure, NULL where the measure is NULL.
    """
    # PERCENT_RANK() sorts NULL first; a partition of its own keeps the
    # missing values from pushing the recorded ones up. Partitioned, the ranks
    # also fit in memory for at least twice as many candidates: unpartitioned,
    # a test ran out at 5 million, where this passes at 10 million.
    return (
        f"IF({column} IS NULL, NULL, PERCENT_RANK() OVER "
        f"(PARTITION BY {column} IS NULL ORDER BY {column}))"
    )


def _build_tail_sql(percentile: str) -> str:
    """Builds the tail score of a percentile.

    Args:
        percentile: A percentile from `_build_percentile_sql`.

    Returns:
        A FLOAT64 expression from 0 to 1, and 0 where the percentile is NULL.
    """
    cutoff = repr(TAIL_CUTOFF)
    # The divisor is computed as the reference computes it, so the top of a
    # tail scores exactly 1 in both.
    return (
        f"IF({percentile} IS NULL, 0, GREATEST(0, "
        f"({percentile} - {cutoff}) / (1 - {cutoff})))"
    )


def build_weighted_target_ctes(
    *, candidates: str, measures: str, id_column: str, target_cte: str
) -> str:
    """Builds the common table expressions that draw a weighted sample.

    Each referenced only once, so `candidates` -- which may hold a selector
    that calls a model for every row -- runs once.

    Args:
        candidates: Subquery projecting the distinct candidate ids as
            `id_column`, binding at most `@agent_name`, `@window_start` and
            `@window_end`.
        measures: Subquery projecting `id_column` and `MEASURE_COLUMNS`, at
            most one row per id. A candidate it has no row for scores as one
            without signals.
        id_column: Name of the id column, a STRING.
        target_cte: Name of the last expression, which holds the sample.

    Returns:
        The expressions, comma-separated and without ``WITH``. The last one,
        `target_cte`, projects `id_column` and `SAMPLING_COLUMN` for each of
        at most ``@limit`` picked trajectories; the SQL binds ``@limit``,
        ``@limit_plus_one`` and ``@sampling_seed``.
    """
    added = " + ".join(
        (
            f"{ERROR_COEFFICIENT!r} * IF(has_error, 1, 0)",
            f"{ABNORMAL_FINISH_COEFFICIENT!r} * IF(has_abnormal_finish, 1, 0)",
            f"{TOOL_REPORTED_ERROR_COEFFICIENT!r} * IF(has_tool_reported_error, 1, 0)",
            f"{BUSY_TURN_COEFFICIENT!r} * busy_turn_tail",
            f"{COST_COEFFICIENT!r} * cost_tail",
            f"{LATENCY_COEFFICIENT!r} * latency_tail",
        )
    )
    return f"""Candidates AS (
{candidates}
),
Measures AS (
{measures}
),
Scored AS (
  SELECT
    *,
    1 + added / GREATEST({ADDED_WEIGHT_AVERAGE_LIMIT!r}, AVG(added) OVER ()) AS weight
  FROM (
    SELECT *, {added} AS added
    FROM (
      SELECT
        *,
        {_build_tail_sql("busy_turn_percentile")} AS busy_turn_tail,
        {_build_tail_sql("cost_percentile")} AS cost_tail,
        {_build_tail_sql("latency_percentile")} AS latency_tail
      FROM (
        SELECT
          c.{id_column},
          COALESCE(m.has_error, FALSE) AS has_error,
          COALESCE(m.has_abnormal_finish, FALSE) AS has_abnormal_finish,
          COALESCE(m.has_tool_reported_error, FALSE) AS has_tool_reported_error,
          m.busy_turn_calls,
          m.total_tokens,
          m.slowest_turn_ms,
          {_build_percentile_sql("m.busy_turn_calls")} AS busy_turn_percentile,
          {_build_percentile_sql("m.total_tokens")} AS cost_percentile,
          {_build_percentile_sql("m.slowest_turn_ms")} AS latency_percentile
        FROM Candidates AS c
        LEFT JOIN Measures AS m USING ({id_column})
      )
    )
  )
),
Keyed AS (
  -- A hash reduced to 53 bits, which a FLOAT64 holds exactly, mapped to (0, 1].
  SELECT
    *,
    (ABS(MOD(FARM_FINGERPRINT(CONCAT(@sampling_seed, ':', CAST({id_column} AS STRING))),
             {1 << 53})) + 1) / {1 << 53} AS u
  FROM Scored
),
Prioritized AS (
  SELECT *, weight / u AS priority
  FROM Keyed
  ORDER BY priority DESC, {id_column}
  LIMIT @limit_plus_one
),
{target_cte} AS (
  SELECT
    {id_column},
    STRUCT(
      weight,
      IF(threshold IS NULL, 1.0, LEAST(1.0, weight / threshold)) AS inclusion_probability,
      STRUCT(
        has_error, has_abnormal_finish, has_tool_reported_error,
        busy_turn_calls, total_tokens, slowest_turn_ms,
        busy_turn_percentile, cost_percentile, latency_percentile,
        busy_turn_tail, cost_tail, latency_tail
      ) AS signals
    ) AS {SAMPLING_COLUMN}
  FROM (
    SELECT
      *,
      ROW_NUMBER() OVER (ORDER BY priority DESC, {id_column}) AS sample_rank,
      -- Analytic functions see all the rows Prioritized kept, so with more
      -- candidates than the budget the lowest priority is the threshold.
      IF(COUNT(*) OVER () > @limit, MIN(priority) OVER (), NULL) AS threshold
    FROM Prioritized
  )
  WHERE sample_rank <= @limit
)"""  # noqa: S608 - may embed agent selector SQL, gated by precheck_selector's table guard and dry-run allowlist


def build_uniform_target_cte(
    *, candidates: str, id_column: str, target_cte: str
) -> str:
    """Builds the common table expression that draws a uniform sample.

    Args:
        candidates: Subquery projecting the distinct candidate ids as
            `id_column`.
        id_column: Name of the id column.
        target_cte: Name of the expression.

    Returns:
        The expression, without ``WITH``. It projects `id_column` and
        `SAMPLING_COLUMN` for each of at most ``@limit`` picked trajectories,
        and binds ``@limit``.
    """
    return f"""{target_cte} AS (
  SELECT
    {id_column},
    -- Analytic functions run before ORDER BY and LIMIT, so this counts every
    -- candidate.
    STRUCT(LEAST(1.0, @limit / COUNT(*) OVER ()) AS inclusion_probability) AS {SAMPLING_COLUMN}
  FROM (
{candidates}
  )
  ORDER BY RAND()
  LIMIT @limit
)"""  # noqa: S608 - may embed agent selector SQL, gated by precheck_selector's table guard and dry-run allowlist


def build_sampling_parameters(
    limit: int, seed: str
) -> list[bigquery.ScalarQueryParameter]:
    """Builds the query parameters a weighted sample binds beyond ``@limit``.

    Args:
        limit: The budget.
        seed: Seed of the random keys.

    Returns:
        ``@limit_plus_one`` and ``@sampling_seed``.
    """
    return [
        bigquery.ScalarQueryParameter("limit_plus_one", "INT64", limit + 1),
        bigquery.ScalarQueryParameter("sampling_seed", "STRING", seed),
    ]


def parse_sampling_record(value: Any) -> SamplingRecord | None:
    """Reads how a trajectory was picked from the `SAMPLING_COLUMN` of its row.

    Args:
        value: The column's value: a mapping, or anything else for a row
            without it.

    Returns:
        The record, or None when the value holds no probability in (0, 1],
        the only ones a count can be scaled by.
    """
    if not isinstance(value, Mapping):
        return None
    probability = value.get("inclusion_probability")
    if probability is None or not 0 < float(probability) <= 1:
        return None
    weight = value.get("weight")
    signals = value.get("signals")
    return SamplingRecord(
        inclusion_probability=float(probability),
        weight=None if weight is None else float(weight),
        signals=SamplingSignals.model_validate(dict(signals))
        if isinstance(signals, Mapping)
        else None,
    )
