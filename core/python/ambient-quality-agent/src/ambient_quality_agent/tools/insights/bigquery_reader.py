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

"""Read-only queries over the durable insight tables, agent-scoped.

The deployed implementation of `reader.InsightReader`, which is where the
behavior a reader owes its callers is written down.

The counterpart to `InsightStore`: `InsightStore` owns the sweep's write path
(cleanup, matching, persistence, auto-resolve); `InsightReader` owns the
harness's read path (list, detail, occurrence history). They are deliberately
separate classes -- the reader needs no match model, and keeping the paths apart
means a read can never reach the write machinery. The two share the same two
tables but nothing else.

The reader also reads `ROOT_CAUSES_TABLE`, which the sweep never writes: a
diagnosis is recorded by a chat turn through `RootCauseStore`, and surfaces here
as a per-insight marker and as the records themselves.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
from typing import TYPE_CHECKING, Any

from ambient_quality_agent.tools.insights.models import (
    ClusterVerification,
    InsightOccurrence,
    InsightStatus,
    InsightView,
    OccurrenceState,
    ProposedEdit,
    RootCause,
    RubricExample,
)
from ambient_quality_agent.tools.insights.reader import InsightOrder
from ambient_quality_agent.tools.insights.root_cause_store import (
    ROOT_CAUSES_TABLE,
)
from google.api_core.exceptions import NotFound
from google.cloud import bigquery
from pydantic import ValidationError

if TYPE_CHECKING:
    from collections.abc import Sequence

logger = logging.getLogger(__name__)

_INSIGHTS_TABLE = "insights"
_OCCURRENCES_TABLE = "insight_occurrences"

_OWNER_ID = "COALESCE(m.merged_into_insight_id, o.insight_id)"
"""The insight an occurrence's evidence rolls up into, under the aliases
`_build_owner_join` introduces.

Deduplication moves no occurrence -- the source keeps every sighting it was
found under and gains a pointer -- so the fold happens here, on read. One
``COALESCE`` is the whole resolution, and that is only correct because
`InsightStore.merge_insight` flattens chains as it writes them: a pointer always
names a row whose own pointer is NULL. The two are one design, not two.
"""

_ROOT_CAUSE_OWNER_ID = "COALESCE(rm.merged_into_insight_id, rc.insight_id)"
"""The insight a diagnosis counts for, under the aliases
`_build_root_cause_owner_join` introduces.

`_OWNER_ID`'s rule applied to the other kind of evidence an insight carries.
Deduplication is a claim that two insights are one defect, so a diagnosis of the
duplicate is a diagnosis of the target, and a target that absorbed a diagnosed
insight's sightings has to absorb its diagnosis with them -- otherwise the
counts say one thing and the badge says another. The single ``COALESCE`` is
correct here for the same reason it is there.
"""


_ORDER_CLAUSES: dict[InsightOrder, str] = {
    InsightOrder.RECENT: "i.updated_at DESC, i.insight_id",
    # BigQuery sorts NULL below every value, so DESC puts the insights with no
    # occurrence to sum last -- where an impact ranking wants them.
    InsightOrder.IMPACT: "SUM(owned.trace_count) DESC, i.insight_id",
}
"""``ORDER BY`` body per order, each ending in ``insight_id`` so that equal rows
keep one arrangement across pages and no insight is skipped or repeated."""


class BigQueryInsightReader:
    """Reads one agent's insights and occurrences from BigQuery.

    The list view aggregates occurrence stats but omits the heavy `rubrics`
    payload; the detail view carries it. Every query is scoped to
    ``agent_name`` -- insights are never read across agents.
    """

    def __init__(
        self,
        *,
        client: bigquery.Client,
        project_id: str,
        dataset: str,
        agent_name: str,
    ) -> None:
        """Bind the reader to one agent's dataset.

        Args:
            client: Shared BigQuery client (its location fixes the job region).
            project_id: GCP project owning the dataset, for table references.
            dataset: BigQuery dataset holding the two insight tables.
            agent_name: The observed agent this reader is scoped to; every query
                filters on it.
        """
        self._client = client
        self._project_id = project_id
        self._dataset = dataset
        self._agent_name = agent_name
        self._root_causes_present: bool | None = None

    def _build_table_ref(self, table: str) -> str:
        """Fully-qualified ``project.dataset.table`` reference.

        Args:
            table: Target table name.

        Returns:
            Fully-qualified table identifier.
        """
        return f"{self._project_id}.{self._dataset}.{table}"

    def _run(self, sql: str, params: list[Any]) -> bigquery.table.RowIterator:
        """Run a parameterized statement to completion and return its rows.

        Args:
            sql: Query statement to execute.
            params: Parameters to bind to the query.

        Returns:
            Row iterator of query results.
        """
        job_config = bigquery.QueryJobConfig(query_parameters=params)
        return self._client.query(sql, job_config=job_config).result()

    def _has_root_causes_table(self) -> bool:
        """Checks if `ROOT_CAUSES_TABLE` exists, caching the result.

        Prevents query failures against datasets that lack the root causes table
        by degrading root-cause fields to defaults.

        Returns:
            True if `ROOT_CAUSES_TABLE` exists, False otherwise.
        """
        if self._root_causes_present is None:
            try:
                self._client.get_table(self._build_table_ref(ROOT_CAUSES_TABLE))
                self._root_causes_present = True
            except NotFound:
                self._root_causes_present = False
        return self._root_causes_present

    def _build_root_cause_marker(self) -> str:
        """Builds a SELECT expression yielding ``has_root_cause`` for each insight.

        Uses a correlated scalar subquery, resolved through
        `_ROOT_CAUSE_OWNER_ID` so a diagnosis of a deduplicated insight marks
        the target that absorbed it, to avoid altering occurrence count
        aggregations in the outer GROUP BY.

        Returns:
            SQL expression evaluating to a boolean column.
        """
        if not self._has_root_causes_table():
            # Debug level because callers build a reader per request, so a
            # polling dashboard would otherwise repeat this on every poll.
            logger.debug(
                "insights: %s is absent; reading insights without root causes",
                ROOT_CAUSES_TABLE,
            )
            return "FALSE AS has_root_cause"
        return (
            "(SELECT COUNT(*) > 0 FROM "  # noqa: S608 - trusted identifiers and fixed SQL; values are parameterized
            f"`{self._build_table_ref(ROOT_CAUSES_TABLE)}` rc "
            f"{self._build_root_cause_owner_join()} "
            f"WHERE {_ROOT_CAUSE_OWNER_ID} = i.insight_id) AS has_root_cause"
        )

    def _build_root_cause_owner_join(self) -> str:
        """Join that puts `_ROOT_CAUSE_OWNER_ID` in reach of a query aliased ``rc``.

        `ROOT_CAUSES_TABLE` carries no ``agent_name``, so this matches on the id
        alone -- a uuid4 hex, and every read of it is already scoped to one
        agent by the query around it.

        Returns:
            LEFT JOIN SQL clause string.
        """
        return (
            f"LEFT JOIN `{self._build_table_ref(_INSIGHTS_TABLE)}` rm "
            "ON rm.insight_id = rc.insight_id"
        )

    def _build_owner_join(self) -> str:
        """Join that puts `_OWNER_ID` in reach of an occurrences query aliased ``o``.

        The small side of the join is three STRING columns off `insights`, which
        holds one row per distinct defect, so BigQuery broadcasts it.

        Returns:
            LEFT JOIN SQL clause string.
        """
        return (
            f"LEFT JOIN `{self._build_table_ref(_INSIGHTS_TABLE)}` m\n"
            "  ON m.insight_id = o.insight_id AND m.agent_name = o.agent_name"
        )

    def _build_owned_occurrences_cte(self) -> str:
        """``WITH`` clause naming every occurrence beside the insight it counts for.

        The aggregating reads join this rather than the occurrences table, so a
        target's totals include the sightings of everything deduplicated into
        it. Scoped to the agent here, so the joins above it need not repeat it.

        Returns:
            SQL common table expression defining ``owned``.
        """
        return f"""
WITH owned AS (
  SELECT o.occurrence_id, o.agent_name, o.run_id, o.created_at, o.trace_count,
         o.trajectory_ids, {_OWNER_ID} AS owner_id
  FROM `{self._build_table_ref(_OCCURRENCES_TABLE)}` o
  {self._build_owner_join()}
  WHERE o.agent_name = @agent_name
)"""  # noqa: S608 - trusted identifiers and fixed SQL; values are parameterized

    def list_insights(
        self,
        *,
        statuses: Sequence[InsightStatus] | None = None,
        run_id: str | None = None,
        limit: int,
        offset: int,
        window_start: str | None = None,
        window_end: str | None = None,
        has_root_cause: bool | None = None,
        order_by: InsightOrder = InsightOrder.RECENT,
        day: str | None = None,
    ) -> tuple[list[InsightView], int]:
        """Pages through this agent's insights in the order ``order_by`` names.

        Args:
            statuses: Optional lifecycle statuses to include (defaults to all).
            run_id: Optional sweep ID filter.
            limit: Maximum number of insights to return.
            offset: Number of insights to skip.
            window_start: Optional ISO timestamp lower bound on ``updated_at``.
            window_end: Optional ISO timestamp upper bound on ``updated_at``.
            has_root_cause: Optional filter for diagnosed (True) or undiagnosed
                (False) insights. Defaults to None (no filter).
            order_by: How to rank the insights before paging (defaults to last
                seen first).
            day: Optional UTC day, ``YYYY-MM-DD``: keep only the insights found
                or resolved on it, as `InsightReader.list_insights` defines.

        Returns:
            Tuple of (insights_list, total_matching_count).

        Raises:
            NotFound: If ``has_root_cause`` is specified but the root-causes
                table does not exist in the dataset.
        """
        if has_root_cause is not None and not self._has_root_causes_table():
            raise NotFound(
                f"{self._build_table_ref(ROOT_CAUSES_TABLE)} does not exist, so insights "
                "cannot be filtered on whether they have a root cause"
            )
        where, params = self._build_insight_filters(
            statuses, run_id, window_start, window_end, has_root_cause, day=day
        )
        count_rows = list(
            self._run(
                f"SELECT COUNT(*) AS total "  # noqa: S608 - trusted identifiers and fixed SQL; values are parameterized
                f"FROM `{self._build_table_ref(_INSIGHTS_TABLE)}` i WHERE {where}",
                params,
            )
        )
        total = int(count_rows[0]["total"]) if count_rows else 0
        page_params = [
            *params,
            bigquery.ScalarQueryParameter("limit", "INT64", limit),
            bigquery.ScalarQueryParameter("offset", "INT64", offset),
        ]
        # ARRAY_AGG(... LIMIT 1)[SAFE_OFFSET(0)] pulls the most recent
        # occurrence's run_id inside the GROUP BY; IGNORE NULLS copes with the
        # LEFT JOIN's unmatched (occurrence-less) insights.
        sql = f"""{self._build_owned_occurrences_cte()}
SELECT
  i.insight_id, i.agent_name, i.label, i.status, i.created_at, i.updated_at,
  i.resolved_at, i.dismissed_at, i.merged_into_insight_id, i.merged_at,
  COUNT(owned.occurrence_id) AS occurrence_count,
  SUM(owned.trace_count) AS trace_count,
  MAX(owned.created_at) AS last_run_at,
  ARRAY_AGG(owned.run_id IGNORE NULLS ORDER BY owned.created_at DESC LIMIT 1)[
    SAFE_OFFSET(0)] AS last_run_id,
  {self._build_root_cause_marker()}
FROM `{self._build_table_ref(_INSIGHTS_TABLE)}` i
LEFT JOIN owned
  ON owned.owner_id = i.insight_id AND owned.agent_name = i.agent_name
WHERE {where}
GROUP BY i.insight_id, i.agent_name, i.label, i.status, i.created_at,
  i.updated_at, i.resolved_at, i.dismissed_at, i.merged_into_insight_id,
  i.merged_at
ORDER BY {_ORDER_CLAUSES[order_by]}
LIMIT @limit OFFSET @offset
"""  # noqa: S608 - trusted identifiers and fixed SQL; values are parameterized
        rows = self._run(sql, page_params)
        return [_row_to_view(row) for row in rows], total

    def count_affected_conversations(
        self,
        *,
        statuses: Sequence[InsightStatus] | None = None,
        run_id: str | None = None,
        window_start: str | None = None,
        window_end: str | None = None,
        has_root_cause: bool | None = None,
        day: str | None = None,
    ) -> int:
        """Distinct conversations behind every insight the filters match.

        The filters `list_insights` takes, over the whole matching set rather
        than one page: a header that sums a page states a fraction of the
        deployment as its total. `InvestigationStore.sum_counters` is the
        equivalent on the investigations side.

        Distinct matters because `trace_count` counts a conversation again for
        every sweep that saw it and every insight it matches. The join is INNER,
        unlike the list's, so an insight no sweep has seen brings no NULL array
        to unnest.

        Args:
            statuses: Optional lifecycle statuses to filter on.
            run_id: Optional sweep ID filter.
            window_start: Optional ISO timestamp lower bound on ``updated_at``.
            window_end: Optional ISO timestamp upper bound on ``updated_at``.
            has_root_cause: Optional filter for diagnosed (True) or undiagnosed
                (False) insights.
            day: Optional UTC day string (YYYY-MM-DD).

        Returns:
            Number of distinct conversations matching the filter criteria.

        Raises:
            NotFound: If ``has_root_cause`` is specified but the root-causes
                table does not exist in the dataset.
        """
        if has_root_cause is not None and not self._has_root_causes_table():
            raise NotFound(
                f"{self._build_table_ref(ROOT_CAUSES_TABLE)} does not exist, so insights "
                "cannot be filtered on whether they have a root cause"
            )
        where, params = self._build_insight_filters(
            statuses, run_id, window_start, window_end, has_root_cause, day=day
        )
        sql = f"""{self._build_owned_occurrences_cte()}
SELECT COUNT(DISTINCT trajectory_id) AS conversations
FROM `{self._build_table_ref(_INSIGHTS_TABLE)}` i
JOIN owned
  ON owned.owner_id = i.insight_id AND owned.agent_name = i.agent_name
CROSS JOIN UNNEST(owned.trajectory_ids) AS trajectory_id
WHERE {where}
"""  # noqa: S608 - trusted identifiers and fixed SQL; values are parameterized
        # Always one row, and COUNT of nothing is 0 rather than NULL.
        return int(next(iter(self._run(sql, params)))["conversations"])

    def get_insight(self, insight_id: str) -> InsightView | None:
        """Return one insight with its occurrence stats, or ``None`` if absent.

        Same aggregation as `list_insights` narrowed to a single id, so the
        detail view leads with the same summary the list showed.

        Deliberately without the list's hiding filter: a dismissed or
        deduplicated insight still resolves here, because a bookmarked link
        should not start returning 404 the moment somebody tidies the list. A
        deduplicated one reads as zero sightings -- its evidence now counts for
        the target -- with `merged_into_insight_id` saying where it went.

        Args:
            insight_id: Unique insight identifier.

        Returns:
            Insight view with occurrence statistics, or None if absent.
        """
        params = [
            bigquery.ScalarQueryParameter(
                "agent_name", "STRING", self._agent_name
            ),
            bigquery.ScalarQueryParameter("insight_id", "STRING", insight_id),
        ]
        sql = f"""{self._build_owned_occurrences_cte()}
SELECT
  i.insight_id, i.agent_name, i.label, i.status, i.created_at, i.updated_at,
  i.resolved_at, i.dismissed_at, i.merged_into_insight_id, i.merged_at,
  COUNT(owned.occurrence_id) AS occurrence_count,
  SUM(owned.trace_count) AS trace_count,
  MAX(owned.created_at) AS last_run_at,
  ARRAY_AGG(owned.run_id IGNORE NULLS ORDER BY owned.created_at DESC LIMIT 1)[
    SAFE_OFFSET(0)] AS last_run_id,
  {self._build_root_cause_marker()}
FROM `{self._build_table_ref(_INSIGHTS_TABLE)}` i
LEFT JOIN owned
  ON owned.owner_id = i.insight_id AND owned.agent_name = i.agent_name
WHERE i.agent_name = @agent_name AND i.insight_id = @insight_id
GROUP BY i.insight_id, i.agent_name, i.label, i.status, i.created_at,
  i.updated_at, i.resolved_at, i.dismissed_at, i.merged_into_insight_id,
  i.merged_at
"""  # noqa: S608 - trusted identifiers and fixed SQL; values are parameterized
        rows = list(self._run(sql, params))
        return _row_to_view(rows[0]) if rows else None

    def get_insight_with_occurrences(
        self,
        *,
        insight_id: str,
        run_id: str | None = None,
        limit: int,
        offset: int,
    ) -> tuple[InsightView, list[InsightOccurrence], int] | None:
        """Return one insight merged with a page of its occurrence evidence.

        The detail read the harness consumes: the insight summary together with
        its occurrences (latest first). Returns
        ``(view, occurrences, total)``, or ``None`` when ``insight_id`` is
        unknown. ``run_id`` narrows the occurrences to a single sweep; otherwise
        they paginate by ``limit``/``offset``.

        Args:
            insight_id: Unique insight identifier.
            run_id: Optional sweep ID filter for occurrences.
            limit: Maximum occurrences to return.
            offset: Number of occurrences to skip.

        Returns:
            Tuple of (view, occurrences, total) or None if insight_id is unknown.
        """
        view = self.get_insight(insight_id)
        if view is None:
            return None
        occurrences, total = self.list_occurrences(
            insight_id=insight_id, run_id=run_id, limit=limit, offset=offset
        )
        return view, occurrences, total

    def list_occurrences(
        self,
        *,
        insight_id: str | None = None,
        run_id: str | None = None,
        occurrence_id: str | None = None,
        limit: int,
        offset: int,
    ) -> tuple[list[InsightOccurrence], int]:
        """Pages an insight's occurrences newest first, including rubric payloads.

        Ordered by ``created_at`` descending (most recent sweep first).

        ``insight_id`` selects by *owner*, the same resolution `get_insight`
        aggregates over, so a target's detail page shows the evidence its counts
        claim -- including the sightings of everything deduplicated into it. The
        other way round holds too, and is the point of doing it here rather than
        only in the aggregation: a deduplicated insight shows no evidence,
        because it has none of its own any more, rather than listing sightings
        its own header counts as zero.

        Args:
            insight_id: Insight identifier to list occurrences for. Optional when
                globally unique ``occurrence_id`` is provided.
            run_id: Optional sweep ID filter.
            occurrence_id: Optional sighting ID filter.
            limit: Maximum occurrences to return.
            offset: Number of occurrences to skip.

        Returns:
            Tuple of ``(occurrences, total_count)``.

        Raises:
            ValueError: If neither ``insight_id`` nor ``occurrence_id`` is provided
                to bound the query.
        """
        if not insight_id and not occurrence_id:
            raise ValueError(
                "list_occurrences needs an insight_id or an occurrence_id"
            )
        # An occurrence naming no insight records a candidate the verification
        # pass did not confirm -- rejected, or never judged at all -- so it is
        # evidence about the sweep, not a sighting of a tracked issue. An
        # `insight_id` excludes those rows on its own, since no id compares
        # equal to NULL, but a lookup by `occurrence_id` alone would hand one
        # back; the explicit clause is what makes the rule hold either way.
        clauses = ["o.agent_name = @agent_name", "o.insight_id IS NOT NULL"]
        params = [
            bigquery.ScalarQueryParameter(
                "agent_name", "STRING", self._agent_name
            ),
        ]
        if insight_id:
            clauses.append(f"{_OWNER_ID} = @insight_id")
            params.append(
                bigquery.ScalarQueryParameter(
                    "insight_id", "STRING", insight_id
                )
            )
        if run_id:
            clauses.append("o.run_id = @run_id")
            params.append(
                bigquery.ScalarQueryParameter("run_id", "STRING", run_id)
            )
        if occurrence_id:
            clauses.append("o.occurrence_id = @occurrence_id")
            params.append(
                bigquery.ScalarQueryParameter(
                    "occurrence_id", "STRING", occurrence_id
                )
            )
        source = f"FROM `{self._build_table_ref(_OCCURRENCES_TABLE)}` o\n{self._build_owner_join()}"
        where = " AND ".join(clauses)
        count_rows = list(
            self._run(
                f"SELECT COUNT(*) AS total\n{source}\nWHERE {where}", params
            )
        )
        total = int(count_rows[0]["total"]) if count_rows else 0
        page_params = [
            *params,
            bigquery.ScalarQueryParameter("limit", "INT64", limit),
            bigquery.ScalarQueryParameter("offset", "INT64", offset),
        ]
        sql = f"""
SELECT o.occurrence_id, o.insight_id, o.occurrence_state, o.run_id, o.created_at,
       o.agent_name, o.agent_revision, o.analysis_mode, o.label, o.item_count,
       o.trace_count, o.trajectory_ids, o.analyses, o.rubrics
{source}
WHERE {where}
ORDER BY o.created_at DESC, o.occurrence_id
LIMIT @limit OFFSET @offset
"""
        rows = self._run(sql, page_params)
        return [_row_to_occurrence(row) for row in rows], total

    def list_root_causes(
        self, insight_id: str, *, history: bool = False
    ) -> list[RootCause]:
        """Retrieves root-cause records for one insight, ordered newest first.

        Resolved through `_ROOT_CAUSE_OWNER_ID`, so this answers with the
        diagnoses of everything deduplicated into ``insight_id`` as well as its
        own -- and a deduplicated insight answers with none, its diagnoses now
        counting for the target. The same fold `get_insight` applies to
        sightings, so a card's records and its counts describe one defect.
        Each record still carries the ``insight_id`` it was written against.

        Args:
            insight_id: Unique insight identifier.
            history: If True, returns all historical revisions rather than only
                the latest record per occurrence.

        Returns:
            List of `RootCause` records, or an empty list if the table is absent.
        """
        if not self._has_root_causes_table():
            return []
        params = [
            bigquery.ScalarQueryParameter("insight_id", "STRING", insight_id),
        ]
        # Deduplicate to latest record per occurrence; root_cause_id breaks timestamp ties.
        newest_per_occurrence = (
            ""
            if history
            else (
                "QUALIFY ROW_NUMBER() OVER (\n"
                "  PARTITION BY rc.occurrence_id "
                "ORDER BY rc.created_at DESC, rc.root_cause_id) = 1\n"
            )
        )
        sql = f"""
SELECT rc.root_cause_id, rc.insight_id, rc.occurrence_id, rc.agent_revision,
       rc.summary, rc.edits, rc.created_at
FROM `{self._build_table_ref(ROOT_CAUSES_TABLE)}` rc
{self._build_root_cause_owner_join()}
WHERE {_ROOT_CAUSE_OWNER_ID} = @insight_id
{newest_per_occurrence}ORDER BY rc.created_at DESC, rc.root_cause_id
"""  # noqa: S608 - trusted identifiers and fixed SQL; values are parameterized
        return [_row_to_root_cause(row) for row in self._run(sql, params)]

    def _build_insight_filters(
        self,
        statuses: Sequence[InsightStatus] | None,
        run_id: str | None,
        window_start: str | None = None,
        window_end: str | None = None,
        has_root_cause: bool | None = None,
        *,
        day: str | None = None,
    ) -> tuple[str, list[Any]]:
        """Build the shared ``WHERE`` clause + params for the insight list/count.

        Aliases the insights table ``i`` so the fragment drops into both the
        count query and the aggregating page query unchanged.

        The window filters on ``updated_at`` -- the *last sighting*. "Insights
        in the last week" means the issues live that week, which is what a
        reader scanning the list is asking; filtering on ``created_at`` would
        instead answer "issues first found that week" and hide a defect that has
        recurred daily for a month. Resolved insights therefore leave the list
        once their final sighting falls out of the period, which is also what
        makes the per-day resolved series line up with it.

        The two operator judgements are filtered unconditionally rather than
        offered as a filter: the dashboard's Dismiss and Merge buttons only read
        as working if the row they acted on stops coming back, and the client
        re-reads this list rather than hiding anything itself.

        Args:
            statuses: Optional lifecycle statuses to filter on.
            run_id: Optional sweep ID filter.
            window_start: Optional ISO timestamp lower bound on ``updated_at``.
            window_end: Optional ISO timestamp upper bound on ``updated_at``.
            has_root_cause: Optional filter for root-cause presence.
            day: Optional UTC day string (YYYY-MM-DD).

        Returns:
            Tuple of (where_clause_sql, query_parameters).
        """
        clauses = [
            "i.agent_name = @agent_name",
            "i.dismissed_at IS NULL",
            "i.merged_into_insight_id IS NULL",
        ]
        params: list[Any] = [
            bigquery.ScalarQueryParameter(
                "agent_name", "STRING", self._agent_name
            )
        ]
        if day:
            # With a day the statuses name changes rather than states, so they
            # are consumed here and not also applied as a status filter.
            clauses.append(_build_changed_on_day_clause(statuses))
            params.append(
                bigquery.ScalarQueryParameter(
                    "day", "DATE", dt.date.fromisoformat(day)
                )
            )
        elif statuses:
            clauses.append("i.status IN UNNEST(@statuses)")
            params.append(
                bigquery.ArrayQueryParameter(
                    "statuses", "STRING", [s.value for s in statuses]
                )
            )
        if run_id:
            # By owner, like the counts are: a sweep that found a defect under a
            # label an operator has since called a duplicate still found it, and
            # this view would otherwise list neither of the two insights.
            clauses.append(
                "EXISTS (SELECT 1 FROM "  # noqa: S608 - trusted identifiers and fixed SQL; values are parameterized
                f"`{self._build_table_ref(_OCCURRENCES_TABLE)}` o "
                f"{self._build_owner_join()} "
                f"WHERE {_OWNER_ID} = i.insight_id "
                "AND o.agent_name = @agent_name AND o.run_id = @run_id)"
            )
            params.append(
                bigquery.ScalarQueryParameter("run_id", "STRING", run_id)
            )
        if has_root_cause is not None:
            # No agent predicate: `insight_id` is a uuid4 hex, globally unique,
            # and the outer query is already scoped to this agent.
            existence = "EXISTS" if has_root_cause else "NOT EXISTS"
            clauses.append(
                f"{existence} (SELECT 1 FROM "  # noqa: S608 - trusted identifiers and fixed SQL; values are parameterized
                f"`{self._build_table_ref(ROOT_CAUSES_TABLE)}` rc "
                f"{self._build_root_cause_owner_join()} "
                f"WHERE {_ROOT_CAUSE_OWNER_ID} = i.insight_id)"
            )
        if window_start:
            clauses.append("i.updated_at >= @window_start")
            params.append(
                bigquery.ScalarQueryParameter(
                    "window_start", "TIMESTAMP", window_start
                )
            )
        if window_end:
            clauses.append("i.updated_at <= @window_end")
            params.append(
                bigquery.ScalarQueryParameter(
                    "window_end", "TIMESTAMP", window_end
                )
            )
        return " AND ".join(clauses), params


_CHANGE_ON_DAY: dict[InsightStatus, str] = {
    # Not quite the Home chart's day: the chart counts a new insight on its
    # creating investigation's window_end day, while this uses the insight's
    # created_at wall clock. A run that correlates after midnight UTC, a retry
    # or a backfill can file it one day later here than in the chart; aligning
    # the two is a separate change.
    InsightStatus.NEW: "DATE(i.created_at) = @day",
    InsightStatus.RESOLVED: "DATE(i.resolved_at) = @day",
}
"""The change each status names once the list is narrowed to a day. RECURRING
has no entry: a re-sighting moves ``updated_at``, which every later sighting
moves again, so no day stays stamped with it. ``DATE`` of a ``TIMESTAMP`` is
its UTC day, the day the chart buckets by."""


def _build_changed_on_day_clause(
    statuses: Sequence[InsightStatus] | None,
) -> str:
    """``WHERE`` term keeping the insights that made one of ``statuses``' changes
    on ``@day``; every change when ``statuses`` is empty.

    Args:
        statuses: Sequence of statuses to check transitions for.

    Returns:
        SQL expression string to include in the WHERE clause.
    """
    wanted = statuses or list(_CHANGE_ON_DAY)
    changes = [_CHANGE_ON_DAY[s] for s in wanted if s in _CHANGE_ON_DAY]
    return f"({' OR '.join(changes) or 'FALSE'})"


def _row_to_view(row: Any) -> InsightView:
    """Map an aggregated insights row to an `InsightView`.

    Args:
        row: Row mapping returned by BigQuery.

    Returns:
        Constructed InsightView instance.
    """
    return InsightView(
        insight_id=row["insight_id"],
        agent_name=row["agent_name"],
        label=row["label"],
        status=InsightStatus(row["status"]),
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        resolved_at=row["resolved_at"],
        dismissed_at=row["dismissed_at"],
        merged_into_insight_id=row["merged_into_insight_id"],
        merged_at=row["merged_at"],
        occurrence_count=int(row["occurrence_count"] or 0),
        trace_count=int(row["trace_count"] or 0),
        last_run_id=row["last_run_id"],
        last_run_at=row["last_run_at"],
        has_root_cause=bool(row["has_root_cause"]),
    )


def _row_to_root_cause(row: Any) -> RootCause:
    """Maps a BigQuery row to a `RootCause` instance.

    Args:
        row: BigQuery row mapping containing root-cause fields.

    Returns:
        Instantiated `RootCause` model.
    """
    return RootCause(
        root_cause_id=row["root_cause_id"],
        insight_id=row["insight_id"],
        occurrence_id=row["occurrence_id"],
        # The column is nullable while the model field is not: a null and an
        # empty string both mean the single unnamed revision.
        agent_revision=row["agent_revision"] or "",
        summary=row["summary"],
        edits=_parse_edits(row["edits"]),
        created_at=row["created_at"],
    )


def _parse_edits(raw: Any) -> list[ProposedEdit]:
    """Parses stored JSON edit definitions into `ProposedEdit` models.

    Handles both stringified JSON and pre-parsed objects from BigQuery. Malformed
    entries are omitted to preserve the root-cause record summary.

    Args:
        raw: JSON string or pre-parsed list of edit dictionaries.

    Returns:
        List of validated `ProposedEdit` objects.
    """
    if not raw:
        return []
    try:
        data = json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, ValueError) as exc:
        logger.warning("insights: unparseable stored edits payload: %s", exc)
        return []
    edits: list[ProposedEdit] = []
    for item in data or []:
        try:
            edits.append(ProposedEdit.model_validate(item))
        except ValidationError as exc:
            logger.warning(
                "insights: skipping a malformed proposed edit: %s", exc
            )
    return edits


def _row_to_occurrence(row: Any) -> InsightOccurrence:
    """Map an occurrences row (with its JSON `rubrics`) to an `InsightOccurrence`.

    Args:
        row: Row mapping returned by BigQuery.

    Returns:
        Constructed InsightOccurrence instance.
    """
    return InsightOccurrence(
        occurrence_id=row["occurrence_id"],
        insight_id=row["insight_id"],
        # Rows without an occurrence state default to TRACKED since every
        # returned row names an insight.
        occurrence_state=OccurrenceState(
            row.get("occurrence_state") or OccurrenceState.TRACKED
        ),
        run_id=row["run_id"],
        created_at=row["created_at"],
        agent_name=row["agent_name"],
        # A NULL column and an empty string both mean the single unnamed
        # revision, which the model represents as the empty string.
        agent_revision=row["agent_revision"] or "",
        analysis_mode=row.get("analysis_mode") or "",
        label=row["label"],
        item_count=int(row["item_count"] or 0),
        trace_count=int(row["trace_count"] or 0),
        # Missing trajectory IDs default to an empty list, representing no
        # named sessions.
        trajectory_ids=list(row["trajectory_ids"] or []),
        analyses=_parse_analyses(row.get("analyses")),
        rubrics=_parse_rubrics(row["rubrics"]),
    )


def _parse_analyses(raw: Any) -> dict[str, ClusterVerification]:
    """Rebuild the `analyses` map from its stored JSON column.

    Defensive in the same way as `_parse_rubrics`: the value reads back as a
    string or an already-parsed object depending on client version, and one
    malformed entry is skipped rather than failing the read, because a result a
    reader cannot parse is worth less than the occurrence carrying it.

    Args:
        raw: Raw JSON string or parsed object from BigQuery.

    Returns:
        Mapping of analysis step name to ClusterVerification objects.
    """
    if not raw:
        return {}
    try:
        data = json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, ValueError) as exc:
        logger.warning("insights: unparseable stored analyses payload: %s", exc)
        return {}
    results: dict[str, ClusterVerification] = {}
    for name, item in (data or {}).items():
        try:
            results[str(name)] = ClusterVerification.model_validate(item)
        except ValidationError as exc:
            logger.warning(
                "insights: skipping malformed analysis %r: %s", name, exc
            )
    return results


def _parse_rubrics(raw: Any) -> list[RubricExample]:
    """Rebuild the `RubricExample` list from a stored `rubrics` JSON column.

    The value was written with ``json.dumps`` into a JSON column; depending on
    the BigQuery client version it reads back either as a raw string or as an
    already-parsed object, so handle both. A single malformed entry is skipped
    with a warning rather than failing the whole read -- degraded evidence, not
    an error (mirrors the clustering path's defensive posture).

    Args:
        raw: Raw JSON string or parsed object from BigQuery.

    Returns:
        List of parsed RubricExample objects.
    """
    if not raw:
        return []
    try:
        data = json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, ValueError) as exc:
        logger.warning("insights: unparseable stored rubrics payload: %s", exc)
        return []
    examples: list[RubricExample] = []
    for item in data or []:
        try:
            examples.append(RubricExample.model_validate(item))
        except Exception as exc:
            logger.warning(
                "insights: skipping a malformed stored rubric example: %s", exc
            )
    return examples
