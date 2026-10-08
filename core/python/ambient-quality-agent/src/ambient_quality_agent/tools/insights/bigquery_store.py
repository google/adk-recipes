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

"""BigQuery persistence for durable insights, agent-scoped."""

from __future__ import annotations

import datetime as dt
import json
import logging
from typing import TYPE_CHECKING, Any

from ambient_quality_agent.core.labels import build_request_labels
from ambient_quality_agent.tools.insights import matching
from ambient_quality_agent.tools.insights.models import (
    Insight,
    InsightOccurrence,
    InsightStatus,
)
from google.cloud import bigquery

if TYPE_CHECKING:
    from collections.abc import Callable, Iterable, Mapping, Sequence

    from ambient_quality_agent.tools.insights.clustering import Cluster

logger = logging.getLogger(__name__)

_INSIGHTS_TABLE = "insights"
_OCCURRENCES_TABLE = "insight_occurrences"

_SCHEMA_CHECK_TIMEOUT_SECONDS = 10
"""Cap on the boot-time schema read: a wedged metadata call must not hold up
the first request over a check that only logs."""


class BigQueryInsightStore:
    """Reads and writes one agent's insights and occurrences in BigQuery."""

    def __init__(
        self,
        *,
        client: bigquery.Client,
        project_id: str,
        dataset: str,
        agent_name: str,
        match_call: Callable[[str], str] | None = None,
    ) -> None:
        """Bind the store to one agent's dataset.

        Args:
            client: Shared BigQuery client, created once by the caller and reused
                across fetchers/stores (its location fixes the job region).
            project_id: GCP project owning the dataset, for table references.
            dataset: BigQuery dataset holding the two insight tables.
            agent_name: The observed agent this store is scoped to; every query
                filters on it.
            match_call: Prompt-to-text callable the same-issue judge runs on;
                `matching.call_match_model` when omitted.
        """
        self._client = client
        self._project_id = project_id
        self._dataset = dataset
        self._agent_name = agent_name
        self._match_call = match_call

    def _build_table_ref(self, table: str) -> str:
        """Fully-qualified ``project.dataset.table`` reference.

        Args:
            table: Target table name.

        Returns:
            Fully-qualified table identifier.
        """
        return f"{self._project_id}.{self._dataset}.{table}"

    def list_missing_columns(self) -> dict[str, list[str] | None]:
        """Columns `Insight` and `InsightOccurrence` declare that the tables lack.

        One entry per table, so one unreachable table does not hide whether the
        other has drifted. `None` reads as `InvestigationStore.list_missing_columns`
        documents it.

        Returns:
            Mapping of table name to missing column names, or None if unreadable.
        """
        return {
            _INSIGHTS_TABLE: self._list_missing_columns_in(
                _INSIGHTS_TABLE, Insight.model_fields
            ),
            _OCCURRENCES_TABLE: self._list_missing_columns_in(
                _OCCURRENCES_TABLE, InsightOccurrence.model_fields
            ),
        }

    def _list_missing_columns_in(
        self, table: str, declared: Iterable[str]
    ) -> list[str] | None:
        try:
            bq_table = self._client.get_table(
                self._build_table_ref(table),
                timeout=_SCHEMA_CHECK_TIMEOUT_SECONDS,
            )
        except Exception:
            logger.warning(
                "could not read %s's schema; skipping the check",
                table,
                exc_info=True,
            )
            return None
        have = {field.name for field in bq_table.schema}
        return [name for name in declared if name not in have]

    def _start_query_job(self, sql: str, params: list[Any]) -> Any:
        """Start a parameterized statement and return its job.

        Jobs carry AQA's billing label, which is what attributes the bytes they
        scan to AQA rather than to whoever owns the project.

        Args:
            sql: Query statement to execute.
            params: Parameters to bind to the query.

        Returns:
            BigQuery QueryJob instance.
        """
        job_config = bigquery.QueryJobConfig(
            query_parameters=params, labels=build_request_labels()
        )
        return self._client.query(sql, job_config=job_config)

    def _run(self, sql: str, params: list[Any]) -> bigquery.table.RowIterator:
        """Run a parameterized statement to completion and return its rows.

        Args:
            sql: Query statement to execute.
            params: Parameters to bind to the query.

        Returns:
            RowIterator over result rows.
        """
        return self._start_query_job(sql, params).result()

    def _run_dml(self, sql: str, params: list[Any]) -> int:
        """Run a DML statement and return how many rows it changed.

        The count is how the operator actions below tell "nothing matched" from
        "done": a statement that names an unknown insight is not an error to
        BigQuery, it simply touches nothing.

        Args:
            sql: DML statement to execute.
            params: Parameters to bind to the statement.

        Returns:
            Number of rows affected by the statement.
        """
        job = self._start_query_job(sql, params)
        job.result()
        return int(job.num_dml_affected_rows or 0)

    def delete_failed_insights(self, run_id: str) -> None:
        """Erase a prior attempt of ``run_id`` so a retry starts clean.

        Order of operations matters because the orphan check reads the post-delete
        occurrence set:

        1. Delete occurrences matching ``run_id`` and agent.
        2. Delete insights left with no remaining occurrences.

        Args:
            run_id: Identifier of the run whose prior attempt is cleared.
        """
        params = [
            bigquery.ScalarQueryParameter("run_id", "STRING", run_id),
            bigquery.ScalarQueryParameter(
                "agent_name", "STRING", self._agent_name
            ),
        ]
        self._run(
            f"DELETE FROM `{self._build_table_ref(_OCCURRENCES_TABLE)}` "  # noqa: S608 - trusted identifiers and fixed SQL; values are parameterized
            "WHERE run_id = @run_id AND agent_name = @agent_name",
            params,
        )
        self._run(
            f"DELETE FROM `{self._build_table_ref(_INSIGHTS_TABLE)}` i "  # noqa: S608 - trusted identifiers and fixed SQL; values are parameterized
            "WHERE i.agent_name = @agent_name "
            "AND NOT EXISTS (SELECT 1 FROM "
            f"`{self._build_table_ref(_OCCURRENCES_TABLE)}` o "
            "WHERE o.insight_id = i.insight_id)",
            params,
        )

    def find_existing_insights(
        self, candidates: Sequence[Cluster]
    ) -> dict[int, str | None]:
        """Map each candidate to the existing insight the judge deems the same.

        Two steps:

        1. Read this agent's open insights, most recently updated first.
        2. Hand them to `matching.match_candidates`, which chooses among them.

        The judge is a `generateContent` call, so the query carries no AI.

        What the query selects:

        - Candidates are keyed by position, because labels are free text and
          could collide.
        - Resolved insights are skipped, so a recurrence after resolution mints
          a new insight instead of reopening the old one.
        - A deduplicated insight is skipped, so the next sweep attaches the
          recurrence to the target an operator chose, not to the row they hid.
        - A dismissed one is deliberately still matched. It stays hidden either
          way, and skipping it would mint a fresh insight every sweep for
          exactly the defect somebody asked not to see.

        Args:
            candidates: Candidate clusters from the current sweep.

        Returns:
            ``{candidate index: matched insight id or None}``. Absent keys and
            ``None`` values both mean no match.

        Raises:
            Exception: Whatever the judge raised, if it answered for no
                candidate at all -- see `matching.match_candidates`.
        """
        if not candidates:
            return {}

        # `insight_id` breaks ties in `updated_at`, and ties are routine: a
        # sweep stamps one timestamp across every insight it touched. The order
        # decides which batch a label lands in, and the first batch to answer
        # wins.
        sql = f"""
SELECT insight_id, label
FROM `{self._build_table_ref(_INSIGHTS_TABLE)}`
WHERE agent_name = @agent_name
  AND status != '{InsightStatus.RESOLVED.value}'
  AND merged_into_insight_id IS NULL
ORDER BY updated_at DESC, insight_id
"""  # noqa: S608 - trusted identifiers and fixed SQL; values are parameterized
        rows = list(
            self._run(
                sql,
                [
                    bigquery.ScalarQueryParameter(
                        "agent_name", "STRING", self._agent_name
                    )
                ],
            )
        )
        if not rows:
            return dict.fromkeys(range(len(candidates)))

        hits = matching.match_candidates(
            [c.label for c in candidates],
            [row["label"] for row in rows],
            model_call=self._match_call,
        )
        return {
            index: (rows[hit]["insight_id"] if hit is not None else None)
            for index, hit in hits.items()
        }

    def save_investigation_result(
        self,
        new_insights: Sequence[Insight],
        seen_insight_ids: Sequence[str],
        recurring_insight_ids: Sequence[str],
        occurrences: Sequence[InsightOccurrence],
        now: dt.datetime,
        *,
        relabelled: Mapping[str, str] | None = None,
    ) -> None:
        """Write a sweep's correlation result: mint, append, date, mark.

        A sweep establishes two separate facts about an insight it already had.
        ``seen_insight_ids`` is every insight a candidate matched, and dating
        those to ``now`` is what leaves `resolve_stale_insights` measuring from
        the last sighting. ``recurring_insight_ids`` is the subset a verdict
        confirmed, and only those change status, because an absent verdict is
        not a recurrence. The second set is contained in the first.

        Both facts land in one statement, so no window exists in which an
        insight is dated to this sweep while its status still says it was never
        seen again. The inserts stay separate -- BigQuery has no cross-statement
        rollback for separate jobs, so `delete_failed_insights` (not a
        transaction) is what makes a retry safe.

        Args:
            new_insights: Newly minted insights to insert.
            seen_insight_ids: Insight IDs matched by current candidates.
            recurring_insight_ids: Subset of seen insight IDs confirmed by verdict.
            occurrences: Occurrences to append for this sweep.
            now: Current timestamp to date sightings and updates.
            relabelled: Optional map of insight ID to updated label.
        """
        if new_insights:
            self._insert_insights(new_insights)
        if occurrences:
            self._insert_occurrences(occurrences)
        if seen_insight_ids:
            self._mark_seen(
                seen_insight_ids, recurring_insight_ids, relabelled or {}, now
            )

    def _insert_insights(self, insights: Sequence[Insight]) -> None:
        rows = [
            {
                "insight_id": i.insight_id,
                "agent_name": i.agent_name,
                "label": i.label,
                "status": i.status.value,
                "created_at": i.created_at.isoformat(),
                "updated_at": i.updated_at.isoformat(),
                # A newly minted insight is open, so this is normally null. It
                # is written regardless, so every declared column is present on
                # the row rather than left to the load job's defaults.
                "resolved_at": i.resolved_at.isoformat()
                if i.resolved_at
                else None,
                # The three operator judgements, for the same reason. A sweep
                # never makes one, so a minted row carries all three null.
                "dismissed_at": i.dismissed_at.isoformat()
                if i.dismissed_at
                else None,
                "merged_into_insight_id": i.merged_into_insight_id,
                "merged_at": i.merged_at.isoformat() if i.merged_at else None,
            }
            for i in insights
        ]
        self._insert_rows(_INSIGHTS_TABLE, rows)

    def _insert_occurrences(
        self, occurrences: Sequence[InsightOccurrence]
    ) -> None:
        rows = [
            {
                "occurrence_id": o.occurrence_id,
                "insight_id": o.insight_id,
                "occurrence_state": o.occurrence_state.value,
                "run_id": o.run_id,
                "created_at": o.created_at.isoformat(),
                "agent_name": o.agent_name,
                "agent_revision": o.agent_revision,
                "analysis_mode": o.analysis_mode,
                "label": o.label,
                "item_count": o.item_count,
                "trace_count": o.trace_count,
                # BigQuery has no null array: a REPEATED column is written
                # empty, never null, or the load job rejects the row.
                "trajectory_ids": list(o.trajectory_ids),
                "analyses": json.dumps(
                    {
                        k: v.model_dump(mode="json")
                        for k, v in o.analyses.items()
                    },
                    ensure_ascii=False,
                ),
                "rubrics": json.dumps(
                    [r.model_dump(mode="json") for r in o.rubrics],
                    ensure_ascii=False,
                ),
            }
            for o in occurrences
        ]
        self._insert_rows(_OCCURRENCES_TABLE, rows)

    def _insert_rows(self, table: str, rows: list[dict[str, Any]]) -> None:
        """Load rows via a load job rather than the streaming API, so they are
        immediately queryable by the same run (streamed rows sit in a buffer that
        DML cannot touch for minutes).

        Args:
            table: Target table name.
            rows: Row dictionaries to load.
        """
        errors = self._client.load_table_from_json(
            rows,
            self._build_table_ref(table),
            job_config=bigquery.LoadJobConfig(
                write_disposition=bigquery.WriteDisposition.WRITE_APPEND,
                create_disposition=bigquery.CreateDisposition.CREATE_NEVER,
                autodetect=False,
            ),
        ).result()
        del errors  # result() raises on failure; nothing to inspect on success.

    def _mark_seen(
        self,
        seen_insight_ids: Sequence[str],
        recurring_insight_ids: Sequence[str],
        relabelled: Mapping[str, str],
        now: dt.datetime,
    ) -> None:
        """Date every insight this sweep saw, advance the confirmed subset, and
        take the verdict's rename where it proposed one.

        ``updated_at`` is the last sighting -- the column `resolve_stale_insights`
        measures the staleness window from -- and an occurrence does not move it,
        occurrences being a separate table. Status advances only for an insight a
        verdict confirmed: being seen is not a verdict.

        The label is the recurrence key `find_existing_insights` judges against,
        so replacing it replaces what later sweeps match on. That is the point:
        the stored label is otherwise whatever clustering called the defect the
        first time it appeared, and verification reads the trajectories that
        label was guessed from. Renaming the row keeps its ``insight_id``, so the
        history behind it is not split.

        One statement writes all three, so no sighting is recorded without the
        status and the name that go with it.

        Args:
            seen_insight_ids: Insight IDs observed in this sweep.
            recurring_insight_ids: Subset of observed insights confirmed recurring.
            relabelled: Mapping of insight ID to proposed label replacements.
            now: Sighting timestamp.
        """
        params: list[Any] = [
            bigquery.ArrayQueryParameter(
                "ids", "STRING", list(seen_insight_ids)
            ),
            bigquery.ArrayQueryParameter(
                "recurring_ids", "STRING", list(recurring_insight_ids)
            ),
            bigquery.ScalarQueryParameter("now", "TIMESTAMP", now),
            bigquery.ScalarQueryParameter(
                "agent_name", "STRING", self._agent_name
            ),
        ]
        # No renames, no clause and no parameter: an array of structs carries its
        # element type in the request, and an empty one is a typeless array the
        # statement would have to describe for nothing.
        relabel_clause = ""
        if relabelled:
            params.append(
                bigquery.ArrayQueryParameter(
                    "renames",
                    bigquery.StructQueryParameterType(
                        bigquery.ScalarQueryParameterType(
                            "STRING", name="insight_id"
                        ),
                        bigquery.ScalarQueryParameterType(
                            "STRING", name="label"
                        ),
                    ),
                    [
                        bigquery.StructQueryParameter(
                            None,
                            bigquery.ScalarQueryParameter(
                                "insight_id", "STRING", insight_id
                            ),
                            bigquery.ScalarQueryParameter(
                                "label", "STRING", label
                            ),
                        )
                        for insight_id, label in sorted(relabelled.items())
                    ],
                )
            )
            relabel_clause = (
                "label = COALESCE((SELECT r.label FROM UNNEST(@renames) r "
                "WHERE r.insight_id = i.insight_id), i.label)"
            )
        assignments = [
            "updated_at = @now",
            f"status = IF(i.insight_id IN UNNEST(@recurring_ids), "
            f"'{InsightStatus.RECURRING.value}', i.status)",
        ]
        if relabel_clause:
            assignments.append(relabel_clause)
        self._run(
            f"UPDATE `{self._build_table_ref(_INSIGHTS_TABLE)}` i "  # noqa: S608 - trusted identifiers and fixed SQL; values are parameterized
            f"SET {', '.join(assignments)} "
            "WHERE i.insight_id IN UNNEST(@ids) AND i.agent_name = @agent_name",
            params,
        )

    # --- operator judgements ------------------------------------------------ #
    # Two actions a human takes on the dashboard, as opposed to everything above,
    # which a sweep does. Both are one UPDATE over one table, and both report
    # whether they found the insight rather than raising: the ids come off a
    # request, so an unknown one is an answer, not a fault.

    def dismiss_insight(self, insight_id: str) -> bool:
        """Hide one insight from the list. Returns whether it exists.

        Idempotent, and deliberately so: the dashboard's button is one click
        with no undo, and a double click should not read as a failure.
        ``COALESCE`` is what makes it idempotent without losing the first
        judgement's time -- the row is still matched, so the caller can tell
        "already dismissed" from "no such insight", but the timestamp stands.

        Args:
            insight_id: Unique insight identifier.

        Returns:
            True if an insight was matched and dismissed, False otherwise.
        """
        affected = self._run_dml(
            f"UPDATE `{self._build_table_ref(_INSIGHTS_TABLE)}` "  # noqa: S608 - trusted identifiers and fixed SQL; values are parameterized
            "SET dismissed_at = COALESCE(dismissed_at, @now) "
            "WHERE agent_name = @agent_name AND insight_id = @insight_id",
            [
                bigquery.ScalarQueryParameter(
                    "now", "TIMESTAMP", _read_utc_clock()
                ),
                bigquery.ScalarQueryParameter(
                    "agent_name", "STRING", self._agent_name
                ),
                bigquery.ScalarQueryParameter(
                    "insight_id", "STRING", insight_id
                ),
            ],
        )
        return affected > 0

    def merge_insight(self, *, insight_id: str, target_insight_id: str) -> bool:
        """Record ``insight_id`` as a duplicate of ``target_insight_id``.

        Neither insight is better than the other; which is which is decided by
        the order an operator clicked. So the source keeps its occurrences and
        gains a pointer, and the read folds them into the target's counts.

        One statement over one table, which is what makes it atomic however many
        rows it moves -- and it moves more than one. ``merged_into_insight_id =
        @insight_id`` takes everything already pointing at the source along,
        flattening a chain as it is written: A into B then B into C leaves both
        A and B pointing at C. That is the invariant the read depends on, since
        a single ``COALESCE`` resolves a pointer of depth one and nothing
        deeper.

        The ``EXISTS`` is the one refusal, expressed inside the statement rather
        than as a read followed by a write: the dashboard fires N-1 of these
        concurrently at one target, so a separate check could be true when it
        ran and false when the update landed. Returns whether anything moved --
        false when the source is unknown, or when the target is unknown or is
        itself a duplicate.

        Args:
            insight_id: Identifier of the duplicate insight to fold.
            target_insight_id: Identifier of the canonical target insight.

        Returns:
            True if the merge succeeded, False if either insight is unknown
            or the target is already merged.
        """
        affected = self._run_dml(
            f"UPDATE `{self._build_table_ref(_INSIGHTS_TABLE)}` "  # noqa: S608 - trusted identifiers and fixed SQL; values are parameterized
            "SET merged_into_insight_id = @target_insight_id, merged_at = @now "
            "WHERE agent_name = @agent_name "
            "AND (insight_id = @insight_id OR merged_into_insight_id = @insight_id) "
            "AND EXISTS (SELECT 1 FROM "
            f"`{self._build_table_ref(_INSIGHTS_TABLE)}` t "
            "WHERE t.agent_name = @agent_name "
            "AND t.insight_id = @target_insight_id "
            "AND t.merged_into_insight_id IS NULL)",
            [
                bigquery.ScalarQueryParameter(
                    "now", "TIMESTAMP", _read_utc_clock()
                ),
                bigquery.ScalarQueryParameter(
                    "agent_name", "STRING", self._agent_name
                ),
                bigquery.ScalarQueryParameter(
                    "insight_id", "STRING", insight_id
                ),
                bigquery.ScalarQueryParameter(
                    "target_insight_id", "STRING", target_insight_id
                ),
            ],
        )
        return affected > 0

    def resolve_stale_insights(
        self, now: dt.datetime, window_days: int
    ) -> None:
        """Resolve insights unseen for ``window_days``; ``0`` disables it.

        A zero window is the documented "auto-resolve off" setting, so it must
        touch nothing rather than resolve everything with a 0-day cutoff.

        Stamps ``resolved_at`` alongside the status, because when an issue went
        away is a fact about it that nothing else records. It has to be its own
        column: ``updated_at`` is the last sighting and is exactly what the
        staleness predicate below reads, so writing the resolution time there
        would move the cutoff this query just measured. The ``status !=
        RESOLVED`` guard keeps a later sweep from restamping a date it already
        wrote.

        Args:
            now: Current timestamp marking resolution.
            window_days: Days of inactivity before an insight is resolved. 0
                disables resolution.
        """
        if window_days <= 0:
            return
        params = [
            bigquery.ScalarQueryParameter("now", "TIMESTAMP", now),
            bigquery.ScalarQueryParameter("days", "INT64", window_days),
            bigquery.ScalarQueryParameter(
                "agent_name", "STRING", self._agent_name
            ),
        ]
        self._run(
            f"UPDATE `{self._build_table_ref(_INSIGHTS_TABLE)}` "  # noqa: S608 - trusted identifiers and fixed SQL; values are parameterized
            f"SET status = '{InsightStatus.RESOLVED.value}', resolved_at = @now "
            f"WHERE agent_name = @agent_name "
            f"AND status != '{InsightStatus.RESOLVED.value}' "
            "AND updated_at < TIMESTAMP_SUB(@now, INTERVAL @days DAY)",
            params,
        )


def _read_utc_clock() -> dt.datetime:
    return dt.datetime.now(dt.UTC)
