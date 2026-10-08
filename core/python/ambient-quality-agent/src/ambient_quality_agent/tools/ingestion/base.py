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

"""Common interface for telemetry ingestion fetchers.

Wiring::

    _sweep, _common, preview
        │ precheck_selector(), count_scanned(), count_by_agent(),
        │ submit_query(), fetch_page()
        ▼
    BaseFetcher (ABC) ◄── implements ── TraceFileFetcher
        ▲ implements
        │
        │   observed_agent_config.access_check
        │     │ load_table(), count_scanned(), get_payload_ref()
        │     ▼
    BigQueryJobFetcher ─┬─► BigQuery: query jobs, dry runs, result pages
        ▲ subclass      ├─► selector: wrap(), find_table_violation()
        │               ├─► TrajectoryRecorder: add(), flush()
        │               └─► MemoryGuard: should_stop()
        │
    BigQueryFetcher    CloudOpsFetcher, CloudLoggingFetcher
                           │
                           ▼
                       TraceAgentDataConverter
                           │ reads gs:// refs
                           ▼
                       CachingGcsReader ──► GcsClientReader ──► GCS
"""

from __future__ import annotations

import abc
import dataclasses
import datetime as dt
import logging
import re
from collections.abc import Callable
from typing import Any, ClassVar
from urllib.parse import urlparse

from agentplatform._genai.types import EvalCase, EvaluationDataset
from ambient_quality_agent.core._memory import MemoryGuard
from ambient_quality_agent.core.labels import build_request_labels
from ambient_quality_agent.tools.evaluation.models import MetricType
from ambient_quality_agent.tools.ingestion import selector
from ambient_quality_agent.tools.ingestion.models import (
    IngestionCounts,
    MappedRow,
    Page,
)
from ambient_quality_agent.tools.trajectories.models import IngestStatus
from ambient_quality_agent.tools.trajectories.recorder import (
    NullTrajectoryRecorder,
    TrajectoryRecorder,
)
from google.api_core import exceptions as api_exceptions
from google.cloud import bigquery, storage

logger = logging.getLogger(__name__)

# Resolves a ``gs://`` URI to its text content.
GcsReader = Callable[[str], str]

AGENT_COUNT_LIMIT = 20
"""Most agent names `BaseFetcher.count_by_agent` returns. Its answer is quoted
in an error message, where a shared telemetry table could otherwise list
hundreds."""

# Cache file content for tool definitions and system instructions as the
# filename is a hash of the content - no need to re-download the same
# content for every mapped row.
_CACHEABLE_REF_SUFFIXES = (
    "_tool.definitions.jsonl",
    "_system_instruction.jsonl",
)


class GcsClientReader:
    """Reads ``gs://`` URIs through one reused, injected `storage.Client`."""

    def __init__(self, client: storage.Client | None = None) -> None:
        self._client = client or storage.Client()

    def __call__(self, gcs_uri: str) -> str:
        parsed = urlparse(gcs_uri)
        if parsed.scheme != "gs" or not parsed.netloc:
            raise ValueError(f"Expected a gs:// URI, got {gcs_uri!r}.")
        blob = self._client.bucket(parsed.netloc).blob(parsed.path.lstrip("/"))
        return blob.download_as_text()


class CachingGcsReader:
    """Reads and caches content-addressed GCS offload payloads by URI."""

    def __init__(self, reader: GcsReader | None = None) -> None:
        self._reader = reader if reader is not None else GcsClientReader()
        self._cache: dict[str, str] = {}

    def __call__(self, gcs_uri: str) -> str:
        if not gcs_uri.endswith(_CACHEABLE_REF_SUFFIXES):
            return self._reader(gcs_uri)
        cached = self._cache.get(gcs_uri)
        if cached is None:
            cached = self._reader(gcs_uri)
            self._cache[gcs_uri] = cached
        return cached


class BaseFetcher(abc.ABC):
    """Abstract telemetry fetcher."""

    counts: IngestionCounts
    """What this fetcher scanned, ingested and dropped over its whole scope."""

    @property
    @abc.abstractmethod
    def has_selector(self) -> bool:
        """Whether custom SQL filtering is configured for this fetcher.

        Determines if `precheck_selector` runs; fetchers without a selector
        use default sampling and require no validation.
        """
        raise NotImplementedError

    @abc.abstractmethod
    def precheck_selector(
        self,
        agent_name: str,
        start: dt.datetime,
        end: dt.datetime,
        limit: int = 100,
    ) -> selector.PrecheckResult:
        """Validates the configured selector without executing the query.

        Verifies query syntax, projected columns, and table access (see
        `ambient_quality_agent.tools.ingestion.selector`). A lexical guard
        requires the selector to read the source's selector table by name and
        name no other table of its footprint; dry runs of both the standalone
        selector and the wrapped ingestion query must then reference exactly
        that footprint in the canonical project.

        Args:
            agent_name: Name of the target agent to bind in the query.
            start: Inclusive start timestamp of the ingestion window.
            end: Inclusive end timestamp of the ingestion window.
            limit: Maximum number of sessions to evaluate.

        Returns:
            Precheck outcome containing rejection details if validation fails.

        Raises:
            ValueError: If this fetcher has no selector configured.
        """
        raise NotImplementedError

    @abc.abstractmethod
    def count_scanned(
        self,
        agent_name: str,
        metric_type: MetricType,
        start: dt.datetime,
        end: dt.datetime,
    ) -> int:
        """Count the traces the window holds, before any sampling.

        The same selection `submit_query` makes, minus its ``ORDER BY RAND()``
        and ``LIMIT`` -- so the answer is the population the run's budget
        sampled from, and the denominator its ingestion counts are read
        against. Records the result on `counts` as well as returning it.

        Args:
            agent_name: Name of the observed agent.
            metric_type: Evaluation scope, which fixes the unit counted --
                sessions for ``MULTI_TURN``, traces/invocations for
                ``SINGLE_TURN``.
            start: Inclusive start of the time window.
            end: Inclusive end of the time window.

        Returns:
            How many traces the window holds for that agent and scope.
        """
        raise NotImplementedError

    @abc.abstractmethod
    def count_by_agent(
        self,
        metric_type: MetricType,
        start: dt.datetime,
        end: dt.datetime,
    ) -> dict[str, int]:
        """Count the traces the window holds for each agent name in it.

        The unit `count_scanned` counts, grouped by agent name instead of
        filtered on one, and with no selector applied. It answers the question
        an empty window raises: which names the telemetry does record, when the
        configured one matched nothing.

        Args:
            metric_type: Evaluation scope, which fixes the unit counted, as in
                `count_scanned`.
            start: Inclusive start of the time window.
            end: Inclusive end of the time window.

        Returns:
            Trace counts keyed by agent name, largest first and at most
            `AGENT_COUNT_LIMIT` of them. Traces that record no agent name are
            left out.
        """
        raise NotImplementedError

    @abc.abstractmethod
    def submit_query(
        self,
        agent_name: str,
        metric_type: MetricType,
        start: dt.datetime,
        end: dt.datetime,
        limit: int,
    ) -> str:
        """Start a fetch for the given window and wait until it is ready.

        Args:
            agent_name: Name of the observed agent.
            metric_type: Evaluation scope selecting the query/mapping
                shape.
            start: Inclusive start of the time window.
            end: Inclusive end of the time window.
            limit: Maximum sessions (multi-turn) or turns (single-turn).

        Returns:
            An opaque ``execution_id`` to pass to `fetch_page`.
        """
        raise NotImplementedError

    @abc.abstractmethod
    def fetch_page(
        self,
        execution_id: str,
        metric_type: MetricType,
        page_token: str | None = None,
    ) -> Page:
        """Fetch one page of mapped evaluation cases.

        Args:
            execution_id: An id returned by `submit_query`.
            metric_type: Evaluation scope selecting the mapping shape.
                Must match the metric type passed to the originating
                `submit_query`.
            page_token: Token from a previous call; ``None`` starts at the
                first page.

        Returns:
            A `Page` wrapping an `EvaluationDataset` plus the next page
            token (``page.is_exhausted`` is ``True`` once none remain).
        """
        raise NotImplementedError


class BigQueryJobFetcher(BaseFetcher):
    """`BaseFetcher` that runs a paged BigQuery job over a single table.

    Owns the mechanical BigQuery plumbing shared by every table-backed
    fetcher: the client, the ``project.dataset.table`` reference, the
    submit-and-wait query with the standard
    ``agent_name``/``window_start``/``window_end``/``limit`` parameter
    binding, and the destination-table paging behind `fetch_page`.

    Subclasses supply only the parts that reflect their schema: the telemetry
    source they serve (`TELEMETRY_SOURCE`), the SQL pairs (`_build_ingestion_sql` and
    `_build_count_sql`) and the row-to-case mapping (`_map_row`).
    """

    TELEMETRY_SOURCE: ClassVar[str]
    """Telemetry source key for this fetcher, determining its selector table."""

    FOLLOWS_PAYLOAD_REFS: ClassVar[bool] = False
    """Whether ingestion reads message content from ``gs://`` objects the
    telemetry points at, and so needs read access to the payload bucket."""

    def __init__(
        self,
        project_id: str,
        dataset: str,
        table: str,
        location: str,
        *,
        observed_project_id: str = "",
        memory_guard: Callable[[], bool] | None = None,
        recorder: TrajectoryRecorder | NullTrajectoryRecorder | None = None,
        selector_sql: str = "",
        selector_ai_model: str = "",
        job_timeout_ms: int | None = None,
    ) -> None:
        """Initialize the fetcher.

        Args:
            project_id: GCP project the query jobs run in: AQuA's own.
            dataset: BigQuery dataset holding `table`.
            table: BigQuery table/view to query.
            location: BigQuery location/region of the job and dataset
                (e.g. ``"us-central1"``).
            observed_project_id: GCP project of the observed agent, which
                owns `dataset`, when it is not `project_id`.
            memory_guard: Returns ``True`` when the process is near its
                memory limit; mapping then stops early (see `_map_page`).
                Inject a run-shared guard so both scopes measure growth from
                the same baseline; a fresh `MemoryGuard` is used when omitted.
            recorder: Where to report the trajectories this fetcher samples,
                injected the way a logger is -- it knows which run and agent
                the rows belong to, which a fetcher does not. Omitted outside a
                sweep, and then nothing is written.
            selector_sql: Agent-authored SQL query targeting specific sessions
                to review (see `ambient_quality_agent.tools.ingestion.selector`).
                When empty, multi-turn ingestion defaults to random sampling
                across the window.
            selector_ai_model: Model endpoint substituted into the selector's
                `AI.IF` calls (see
                `ambient_quality_agent.tools.ingestion.selector.wrap`).
            job_timeout_ms: Server-side execution timeout in milliseconds for
                counting and ingestion query jobs. When None, query jobs run
                without a server-side timeout.
        """
        self._project_id = project_id
        self._observed_project_id = observed_project_id or project_id
        self._dataset = dataset
        self._table = table
        self._location = location
        self._client = bigquery.Client(project=project_id, location=location)
        self._memory_guard: Callable[[], bool] = (
            memory_guard or MemoryGuard().should_stop
        )
        self._memory_truncated = False
        self._recorder = recorder or NullTrajectoryRecorder()
        self._selector_sql = selector_sql
        self._selector_ai_model = selector_ai_model
        self._job_timeout_ms = job_timeout_ms
        self.counts = IngestionCounts()

    @property
    def table_ref(self) -> str:
        """Fully-qualified ``project.dataset.table`` reference."""
        return f"{self._observed_project_id}.{self._dataset}.{self._table}"

    def load_table(self) -> bigquery.Table:
        """Reads the telemetry table's metadata.

        Returns:
            The table, with its schema.
        """
        return self._client.get_table(self.table_ref)

    @property
    def selector_footprint(self) -> frozenset[str]:
        """Table names in the telemetry dataset a legitimate selector resolves to.

        See `selector.resolve_selector_footprint`.
        """
        return selector.resolve_selector_footprint(
            self.TELEMETRY_SOURCE, self._table
        )

    @property
    def selector_table_id(self) -> str:
        """Table ID of the one table in the telemetry dataset a selector may read.

        See `selector.resolve_selector_table_id`.
        """
        return selector.resolve_selector_table_id(
            self.TELEMETRY_SOURCE, self._table
        )

    @property
    def has_selector(self) -> bool:
        """Whether a custom selector query replaces the default targets CTE."""
        return bool(self._selector_sql.strip())

    def _substitute_table_ref(self, template: str) -> str:
        """Substitute `table_ref` into a static SQL template.

        Template substitution occurs before splicing agent-authored SQL to
        prevent `str.format` from misinterpreting SQL braces (such as regex
        quantifiers or JSON literals) as format placeholders.

        Args:
            template: SQL template string containing a ``{table_ref}`` placeholder.

        Returns:
            SQL string with the fully-qualified table reference populated.
        """
        return template.format(table_ref=self.table_ref)

    def _resolve_targets(self, default_targets: str, id_column: str) -> str:
        """Resolve the targets CTE fragment for the current run.

        Uses the wrapped agent selector when provided; otherwise falls back to
        the default window-scoped targets template with the table reference
        substituted.

        Args:
            default_targets: Default targets SQL template containing a
                ``{table_ref}`` placeholder.
            id_column: ID column name required by the outer query join.

        Returns:
            SQL fragment ready for splicing into the main query CTE.
        """
        if not self.has_selector:
            return self._substitute_table_ref(default_targets)
        return selector.wrap(
            self._selector_sql,
            id_column,
            ai_model=self._selector_ai_model,
        )

    @abc.abstractmethod
    def _build_ingestion_sql(self, metric_type: MetricType) -> str:
        """Return the complete ingestion query for ``metric_type``.

        Args:
            metric_type: Ingestion scope (single-turn or multi-turn).

        Returns:
            Executable SQL query string.
        """
        raise NotImplementedError

    @abc.abstractmethod
    def _build_count_sql(self, metric_type: MetricType) -> str:
        """Return the total matching count query for ``metric_type``.

        Evaluates the same targets population as `_build_ingestion_sql` without sampling or
        limit constraints to accurately report total scanned records.

        Args:
            metric_type: Ingestion scope (single-turn or multi-turn).

        Returns:
            Executable SQL query returning a single ``scanned`` count column.
        """
        raise NotImplementedError

    @abc.abstractmethod
    def _build_count_by_agent_sql(self, metric_type: MetricType) -> str:
        """Return the per-agent count query for ``metric_type``.

        The default targets population of `_build_count_sql`, without its agent
        filter, grouped by agent name. The agent-name column is where the
        sources' schemas differ, which is why each fetcher writes its own.

        Args:
            metric_type: Ingestion scope (single-turn or multi-turn).

        Returns:
            Executable SQL query returning one ``agent_name`` and ``scanned``
            row per non-null agent name, binding only the window parameters.
        """
        raise NotImplementedError

    @abc.abstractmethod
    def _map_row(
        self, row: dict[str, Any], metric_type: MetricType
    ) -> MappedRow:
        """Map one raw row to a `MappedRow`: a case, no case, or a partial one.

        Args:
            row: Raw query result row dictionary.
            metric_type: Ingestion scope (single-turn or multi-turn).

        Returns:
            Mapped row carrying the case and ingestion metadata.
        """
        raise NotImplementedError

    def count_scanned(
        self,
        agent_name: str,
        metric_type: MetricType,
        start: dt.datetime,
        end: dt.datetime,
    ) -> int:
        """Run the counting query and record its answer on `counts`.

        No ``limit`` is bound: the whole point of this query is that it has no
        ``LIMIT`` to bind one to.

        A zero logs at ``WARNING``. The sweep that follows will report a clean
        run over nothing, which reads the same in the logs whether the agent was
        idle or the name it filters on is wrong -- so the one line that knows the
        window was empty says so loudly enough to be found.

        Under a selector, the count reflects the selector subquery's matched rows
        rather than their intersection with telemetry. Enforcing that selectors
        read the selector table during `precheck_selector` prevents unbound or
        synthetic ID overcounting.

        Args:
            agent_name: Watched agent name.
            metric_type: Evaluation metric scope.
            start: Window start timestamp.
            end: Window end timestamp.

        Returns:
            Total matching trace count across the window.
        """
        sql = self._build_count_sql(metric_type)
        job_config = bigquery.QueryJobConfig(
            query_parameters=_build_scoping_parameters(agent_name, start, end),
            labels=build_request_labels(),
            job_timeout_ms=self._job_timeout_ms,
        )
        rows = list(self._client.query(sql, job_config=job_config).result())
        scanned = int(rows[0]["scanned"] or 0) if rows else 0
        logger.log(
            logging.WARNING if scanned == 0 else logging.INFO,
            "%s window holds %d trace(s) for agent %r before sampling.",
            metric_type.value,
            scanned,
            agent_name,
        )
        self.counts.scanned += scanned
        return scanned

    def count_by_agent(
        self,
        metric_type: MetricType,
        start: dt.datetime,
        end: dt.datetime,
    ) -> dict[str, int]:
        """Run the per-agent counting query.

        Leaves `counts` alone: these traces are not the run's population.

        Args:
            metric_type: Evaluation metric scope.
            start: Window start timestamp.
            end: Window end timestamp.

        Returns:
            Trace counts keyed by agent name, largest first, at most
            `AGENT_COUNT_LIMIT` of them.
        """
        sql = (
            f"SELECT agent_name, scanned FROM ({self._build_count_by_agent_sql(metric_type)})\n"  # noqa: S608 - trusted identifiers and fixed SQL; values are parameterized
            f"ORDER BY scanned DESC, agent_name\nLIMIT {AGENT_COUNT_LIMIT}"
        )
        job_config = bigquery.QueryJobConfig(
            query_parameters=[
                bigquery.ScalarQueryParameter(
                    "window_start", "TIMESTAMP", start
                ),
                bigquery.ScalarQueryParameter("window_end", "TIMESTAMP", end),
            ],
            labels=build_request_labels(),
            job_timeout_ms=self._job_timeout_ms,
        )
        rows = self._client.query(sql, job_config=job_config).result()
        return {
            str(row["agent_name"]): int(row["scanned"] or 0) for row in rows
        }

    def get_payload_ref(
        self, agent_name: str, start: dt.datetime, end: dt.datetime
    ) -> str | None:
        """Returns one ``gs://`` payload reference from the agent's telemetry in a window.

        Ingestion follows these references to read offloaded message content, so
        reading one proves the payload bucket is readable. A source that does
        not follow references (`FOLLOWS_PAYLOAD_REFS` false) keeps this default.

        Args:
            agent_name: Watched agent name.
            start: Window start timestamp.
            end: Window end timestamp.

        Returns:
            A ``gs://`` URI, or None when the window holds no reference.
        """
        del agent_name, start, end
        return None

    def _select_payload_ref(
        self,
        template: str,
        agent_name: str,
        start: dt.datetime,
        end: dt.datetime,
    ) -> str | None:
        """Runs a query returning at most one ``ref`` column, and returns its value.

        Args:
            template: SQL template containing a ``{table_ref}`` placeholder and
                binding `@agent_name`, `@window_start` and `@window_end`.
            agent_name: Watched agent name.
            start: Window start timestamp.
            end: Window end timestamp.

        Returns:
            The reference, or None when the query returned none.
        """
        job_config = bigquery.QueryJobConfig(
            query_parameters=_build_scoping_parameters(agent_name, start, end),
            labels=build_request_labels(),
            job_timeout_ms=self._job_timeout_ms,
        )
        sql = self._substitute_table_ref(template)
        rows = list(self._client.query(sql, job_config=job_config).result())
        return str(rows[0]["ref"]) if rows and rows[0]["ref"] else None

    def _map_page(
        self, rows: list[dict[str, Any]], metric_type: MetricType
    ) -> tuple[list[EvalCase], dict[str, str]]:
        """Map a page of rows to cases, tallying outcomes and guarding memory.

        Results accumulate across pages, and a row's content (inline or
        GCS-resolved) can be large; on a memory-capped job this risks an
        OOM-kill with no traceback. Before each row the memory guard checks
        whether little headroom remains: when so, stop mapping and set
        `_memory_truncated` so `fetch_page` stops paging. The cases mapped
        so far are still evaluated.

        Every row that *was* attempted lands in exactly one bucket of `counts`
        -- ingested, partial or failed -- and is reported to the recorder with
        the matching `IngestStatus`, so a row's counter and its stored status
        are the same decision rather than two that can disagree. The rows a memory stop left unread are
        counted in none of them: they were never ingested, successfully or
        otherwise.

        Every attempted row is also reported to the recorder, dropped ones
        included: a row that produced no case is still a trajectory the run
        sampled and failed to use, and it is what explains a sample smaller
        than the budget. Rows a memory stop left unread are reported to nobody,
        exactly as they land in none of the counters.

        The case goes with the ids, so the recorder can archive the
        conversation as well as name it. A dropped row passes ``None``, and is
        the one case that gets an index row and no stored copy -- there is
        nothing to store.

        Args:
            rows: Raw result rows for the page.
            metric_type: Ingestion scope (single-turn or multi-turn).

        Returns:
            The page's cases, and the deployment revision of each, keyed by
            ``eval_case_id``. A source that reports no revision yields an empty
            map, which reads as its single unnamed revision.
        """
        total = len(rows)
        cases: list[EvalCase] = []
        revisions: dict[str, str] = {}
        for index, row in enumerate(rows, start=1):
            if self._memory_guard():
                logger.warning(
                    "Near memory limit before row %d/%d; stopping page early.",
                    index,
                    total,
                )
                self._memory_truncated = True
                break
            # Every 100th row, plus the last, so small pages still report once.
            if index % 100 == 0 or index == total:
                logger.info("Mapping row %d/%d.", index, total)
            mapped = self._map_row(row, metric_type)
            # One classification, used twice: it picks the counter to bump and
            # it is the status the trajectory store keeps. A fetcher says what
            # it found; this says what became of it.
            status = _resolve_ingest_status(mapped)
            # The case goes with the ids: this is the one moment that holds
            # both, and archiving it here is what keeps the payload store off
            # every other phase's plumbing.
            self._recorder.add(
                trajectory_id=mapped.trajectory_id,
                session_id=mapped.session_id,
                trace_ids=mapped.trace_ids,
                status=status,
                case=mapped.case,
                agent_revision=mapped.agent_revision,
            )
            if mapped.case is None:
                self.counts.failed += 1
                continue
            if status is IngestStatus.PARTIAL:
                self.counts.partial += 1
            else:
                self.counts.ingested += 1
            cases.append(mapped.case)
            if mapped.agent_revision and mapped.case.eval_case_id:
                revisions[mapped.case.eval_case_id] = mapped.agent_revision
        return cases, revisions

    def precheck_selector(
        self,
        agent_name: str,
        start: dt.datetime,
        end: dt.datetime,
        limit: int = 100,
    ) -> selector.PrecheckResult:
        """Validates this fetcher's selector using lexical checks and BigQuery dry runs.

        After `selector.find_lexical_violation`, the lexical table guard
        (`selector.find_table_violation`) requires the selector to name
        `selector_table_id` and no other `selector_footprint` table; it is the
        only check that distinguishes a view from its base tables. Two dry runs then enforce the data boundary (see
        `ambient_quality_agent.tools.ingestion.selector`):
        1. Wrapped multi-turn query: Verifies query validity and projection of
           `TARGET_COLUMN`, anchors the canonical project on its telemetry-table
           entry, and requires its referenced tables to equal the
           `selector_footprint` in that project.
        2. Standalone selector query: Requires the same equality for the
           isolated selector, catching tables pruned by the outer query and
           rejecting selectors that read no tables (e.g. literal ID lists) or
           only part of a view's footprint.

        Args:
            agent_name: Observed agent name, bound as `@agent_name`.
            start: Window start timestamp, bound as `@window_start`.
            end: Window end timestamp, bound as `@window_end`.
            limit: Evaluation sampling limit, bound as `@limit`.

        Returns:
            A `selector.PrecheckResult` indicating validation status and any rejection.

        Raises:
            ValueError: If the fetcher does not have a configured `selector_sql`.
        """
        if not self.has_selector:
            raise ValueError("This fetcher holds no selector to precheck.")
        violation = selector.find_lexical_violation(self._selector_sql)
        if violation is None:
            violation = selector.find_table_violation(
                self._selector_sql,
                dataset=self._dataset,
                selector_table_id=self.selector_table_id,
                other_table_ids=self.selector_footprint
                - {self.selector_table_id},
            )
        if violation is not None:
            return selector.PrecheckResult(rejection=violation)

        run_sql = self._build_ingestion_sql(MetricType.MULTI_TURN)
        run_job = self._submit_dry_run(
            run_sql, _build_run_parameters(agent_name, start, end, limit)
        )
        if isinstance(run_job, selector.PrecheckResult):
            return run_job
        run_tables = _extract_referenced_tables(run_job)
        expected = self._resolve_expected_tables(run_tables)
        off_allowlist = self._find_footprint_violation(run_tables, expected)
        if off_allowlist is not None:
            return off_allowlist

        # Dry-run the standalone selector to verify it reads the selector table.
        # Omits `@limit` because standalone selectors do not accept sampling limits.
        selector_job = self._submit_dry_run(
            selector.build_standalone_selector(
                self._selector_sql,
                ai_model=self._selector_ai_model,
            ),
            _build_scoping_parameters(agent_name, start, end),
        )
        if isinstance(selector_job, selector.PrecheckResult):
            return selector_job
        off_selector_table = self._find_footprint_violation(
            _extract_referenced_tables(selector_job), expected
        )
        if off_selector_table is not None:
            return off_selector_table

        return selector.PrecheckResult(
            referenced_tables=_format_table_names(run_tables),
            estimated_bytes=int(run_job.total_bytes_processed or 0),
        )

    def _submit_dry_run(
        self, sql: str, parameters: list[bigquery.ScalarQueryParameter]
    ) -> bigquery.QueryJob | selector.PrecheckResult:
        """Executes a dry run to validate a query without running it.

        Args:
            sql: SQL query to validate.
            parameters: Scalar query parameters referenced by the query.

        Returns:
            The planned BigQuery QueryJob, or a rejected `PrecheckResult` if
            BigQuery returns a client error (4xx). Server errors (5xx) propagate.
        """
        job_config = bigquery.QueryJobConfig(
            query_parameters=parameters,
            labels=build_request_labels(),
            dry_run=True,
            # Query caching is disabled so the dry run reports actual table references and byte estimates.
            use_query_cache=False,
        )
        try:
            return self._client.query(sql, job_config=job_config)
        except api_exceptions.ClientError as exc:
            return selector.PrecheckResult(
                rejection=_build_sanitized_rejection(exc, sql)
            )

    def _resolve_expected_tables(
        self, run_tables: tuple[_Table, ...]
    ) -> frozenset[_Table]:
        """Resolves the selector footprint to tables in the canonical project.

        The outer ingestion query always reads the telemetry table, so it should
        match exactly one run-query entry by dataset and table; that entry's
        project is canonical. The configured project may be a project number, while dry
        runs report the project ID, so the configured value is not compared.

        Args:
            run_tables: Tables resolved by the wrapped ingestion query's dry run.

        Returns:
            The footprint tables in the canonical project, or an empty set when
            the telemetry table matches zero entries or more than one (a
            same-named table in another project).
        """
        projects = [
            table.project
            for table in run_tables
            if table.matches(self._dataset, self._table)
        ]
        if not projects:
            # The outer query always reads the telemetry table, so a miss points
            # at configuration rather than the selector.
            logger.warning(
                "Run dry run resolved no %s.%s entry; check the telemetry config.",
                self._dataset,
                self._table,
            )
        if len(projects) != 1:
            return frozenset()
        expected = frozenset(
            _Table(projects[0], self._dataset, name)
            for name in self.selector_footprint
        )
        if not expected <= frozenset(run_tables):
            # The table guard passed, so either the selector table's definition
            # changed (such as an agents-cli view change) or the selector does
            # not actually read it (such as a CTE shadowing the view's name).
            logger.warning(
                "Run dry run resolved %s; expected selector footprint %s. Either "
                "the selector table's definition changed or the selector does "
                "not read it (e.g. a CTE shadows its name).",
                ", ".join(_format_table_names(run_tables)),
                ", ".join(_format_table_names(tuple(sorted(expected)))),
            )
        return expected

    def _find_footprint_violation(
        self, referenced: tuple[_Table, ...], expected: frozenset[_Table]
    ) -> selector.PrecheckResult | None:
        """Verifies that a dry run resolved exactly the expected footprint tables.

        Args:
            referenced: Table references resolved during the dry run.
            expected: Footprint tables in the canonical project, from
                `_resolve_expected_tables`; empty when no canonical project was found.

        Returns:
            None if the referenced tables equal a non-empty `expected`; otherwise
            a rejected PrecheckResult detailing the mismatch.
        """
        if expected and frozenset(referenced) == expected:
            return None
        if not expected:
            explanation = _explain_unidentified_telemetry_table(
                referenced, dataset=self._dataset, table=self._table
            )
        else:
            explanation = _explain_table_mismatch(
                referenced,
                expected,
                dataset=self._dataset,
                selector_table_id=self.selector_table_id,
            )
        return selector.PrecheckResult(
            rejection=selector.Rejection(
                reason=selector.RejectionReason.UNAUTHORIZED_TABLE,
                explanation=explanation,
            ),
            referenced_tables=_format_table_names(referenced),
        )

    def submit_query(
        self,
        agent_name: str,
        metric_type: MetricType,
        start: dt.datetime,
        end: dt.datetime,
        limit: int = 100,
    ) -> str:
        """Submit a background query job and wait for it to complete.

        The query shape depends on ``metric_type`` (session-level for
        ``MULTI_TURN``, turn-level for ``SINGLE_TURN``). ``limit`` is the
        per-metric budget: the number of sessions/turns to retrieve.

        Args:
            agent_name: Watched agent's name (bound as ``@agent_name``).
            metric_type: Evaluation scope selecting the query.
            start: Inclusive start of the time window.
            end: Inclusive end of the time window.
            limit: Maximum sessions (multi-turn) or turns (single-turn).

        Returns:
            The completed job's id, usable with `fetch_page`.
        """
        sql = self._build_ingestion_sql(metric_type)
        job_config = bigquery.QueryJobConfig(
            query_parameters=_build_run_parameters(
                agent_name, start, end, limit
            ),
            # Billing attributes BigQuery bytes to the job, not to the caller,
            # so AQA's label goes on the job.
            labels=build_request_labels(),
            job_timeout_ms=self._job_timeout_ms,
        )

        query_job = self._client.query(sql, job_config=job_config)
        logger.info(
            "Submitted %s ingestion job %s for agent %r (limit=%d).",
            metric_type.value,
            query_job.job_id,
            agent_name,
            limit,
        )
        query_job.result()
        logger.info("Ingestion job %s completed.", query_job.job_id)
        return query_job.job_id

    def fetch_page(
        self,
        execution_id: str,
        metric_type: MetricType,
        page_token: str | None = None,
    ) -> Page:
        """Fetch one page of a completed query's results as eval cases.

        Args:
            execution_id: A BigQuery job id returned by `submit_query`.
            metric_type: Evaluation scope selecting the mapping shape.
                Must match the metric type used in the originating
                `submit_query`.
            page_token: Token from a previous call; ``None`` starts at the
                first page.

        Returns:
            A `Page` wrapping an `EvaluationDataset` and the token for the
            next page (``page.is_exhausted`` is ``True`` once none remain).
        """
        rows, next_page_token = self._fetch_page_rows(execution_id, page_token)
        cases, revisions = self._map_page(rows, metric_type)
        # One write per page: the unit ingestion produces, and what the store
        # is sized for.
        self._recorder.flush()
        if self._memory_truncated:
            next_page_token = None
        logger.debug(
            "Fetched page from job %s: %d case(s), exhausted=%s, truncated=%s.",
            execution_id,
            len(cases),
            next_page_token is None,
            self._memory_truncated,
        )
        return Page(
            dataset=EvaluationDataset(eval_cases=cases),
            next_page_token=next_page_token,
            memory_truncated=self._memory_truncated,
            revisions=revisions,
        )

    def _fetch_page_rows(
        self, execution_id: str, page_token: str | None
    ) -> tuple[list[dict[str, Any]], str | None]:
        """Read one page of a completed job's destination table.

        Args:
            execution_id: BigQuery job ID whose destination table to page.
            page_token: Next page token, or None for the initial page.

        Returns:
            The page's raw rows (as dicts) and the next page token, or
            ``None`` once no pages remain.
        """
        query_job = self._client.get_job(execution_id, location=self._location)
        if not isinstance(query_job, bigquery.QueryJob):
            raise TypeError(
                f"Job {execution_id} is a {type(query_job).__name__}, not a QueryJob."
            )
        destination = query_job.destination
        if destination is None:
            raise ValueError(
                f"Job {execution_id} has no destination table to page over."
            )

        row_iterator = self._client.list_rows(
            destination, page_token=page_token
        )
        page = next(row_iterator.pages)
        rows = [dict(row.items()) for row in page]
        return rows, (row_iterator.next_page_token or None)


def _build_scoping_parameters(
    agent_name: str, start: dt.datetime, end: dt.datetime
) -> list[bigquery.ScalarQueryParameter]:
    """Constructs query parameters for agent scoping and time window boundaries.

    Args:
        agent_name: Name of the observed agent (`@agent_name`).
        start: Start timestamp of the window (`@window_start`).
        end: End timestamp of the window (`@window_end`).

    Returns:
        List of configured BigQuery scalar query parameters.
    """
    return [
        bigquery.ScalarQueryParameter("agent_name", "STRING", agent_name),
        bigquery.ScalarQueryParameter("window_start", "TIMESTAMP", start),
        bigquery.ScalarQueryParameter("window_end", "TIMESTAMP", end),
    ]


def _build_run_parameters(
    agent_name: str, start: dt.datetime, end: dt.datetime, limit: int
) -> list[bigquery.ScalarQueryParameter]:
    """Build standard query parameters for BigQuery ingestion jobs.

    Args:
        agent_name: Name of the observed agent.
        start: Window start timestamp.
        end: Window end timestamp.
        limit: Sampling budget limit.

    Returns:
        List of configured scalar query parameters.
    """
    return [
        *_build_scoping_parameters(agent_name, start, end),
        bigquery.ScalarQueryParameter("limit", "INT64", limit),
    ]


@dataclasses.dataclass(frozen=True, order=True)
class _Table:
    """Table reference resolved by a BigQuery dry run."""

    project: str
    dataset_id: str
    table_id: str

    def __str__(self) -> str:
        return f"{self.project}.{self.dataset_id}.{self.table_id}"

    def matches(self, dataset: str, table: str) -> bool:
        """Check whether this table matches a dataset and table name regardless of project.

        Args:
            dataset: Expected dataset name.
            table: Expected table name.

        Returns:
            True if dataset and table IDs match, False otherwise.
        """
        return self.dataset_id == dataset and self.table_id == table


def _extract_referenced_tables(job: bigquery.QueryJob) -> tuple[_Table, ...]:
    """Extract sorted, deduplicated table references resolved by a dry run.

    Args:
        job: BigQuery query job from a dry run.

    Returns:
        Sorted tuple of unique tables referenced by the job.
    """
    return tuple(
        sorted(
            {
                _Table(table.project, table.dataset_id, table.table_id)
                for table in job.referenced_tables
            }
        )
    )


def _format_table_names(referenced: tuple[_Table, ...]) -> tuple[str, ...]:
    """Format table references as fully qualified strings for diagnostic output.

    Args:
        referenced: Table references to format.

    Returns:
        Tuple of formatted 'project.dataset.table' strings.
    """
    return tuple(str(table) for table in referenced)


def _explain_table_mismatch(
    referenced: tuple[_Table, ...],
    expected: frozenset[_Table],
    *,
    dataset: str,
    selector_table_id: str,
) -> str:
    """Generates an explanation when resolved tables do not match the footprint.

    Names only table references to avoid leaking raw BigQuery error details.

    Args:
        referenced: Tables resolved by the dry run.
        expected: Footprint tables in the canonical project; non-empty.
        dataset: Telemetry dataset holding the selector table.
        selector_table_id: Table ID of the one table the selector may read.

    Returns:
        Explanation naming the selector table as ``dataset.table``, the tables
        read that are not allowed, and the footprint tables not read.
    """
    allowed = f"{dataset}.{selector_table_id}"
    if not referenced:
        return (
            f"The selector must read the selector table {allowed}, "
            "but it reads no table at all."
        )
    parts = [f"The selector may only read the selector table {allowed}."]
    not_allowed = sorted(frozenset(referenced) - expected)
    if not_allowed:
        parts.append(
            f"It reads tables it may not: {', '.join(_format_table_names(tuple(not_allowed)))}."
        )
    missing = sorted(expected - frozenset(referenced))
    if missing:
        parts.append(
            f"It does not resolve to {', '.join(_format_table_names(tuple(missing)))}, which "
            f"reading {allowed} requires."
        )
    return " ".join(parts)


def _explain_unidentified_telemetry_table(
    referenced: tuple[_Table, ...], *, dataset: str, table: str
) -> str:
    """Generates an explanation when no canonical project can be anchored.

    Args:
        referenced: Tables resolved by the wrapped ingestion query's dry run.
        dataset: Configured telemetry dataset.
        table: Configured telemetry table.

    Returns:
        Explanation naming the telemetry table and why it was not identified.
    """
    telemetry = f"{dataset}.{table}"
    parts = [
        f"The telemetry table {telemetry} could not be identified in the dry run, "
        "so no table can be allowed."
    ]
    matches = tuple(
        entry for entry in referenced if entry.matches(dataset, table)
    )
    if matches:
        parts.append(
            f"It resolves {telemetry} in more than one project "
            f"({', '.join(_format_table_names(matches))}): the selector may not read a "
            "same-named table in another project."
        )
    else:
        resolved = (
            f" (only {', '.join(_format_table_names(referenced))})"
            if referenced
            else ""
        )
        parts.append(
            f"It resolves no {telemetry}{resolved}, which points at the "
            "telemetry configuration rather than the selector."
        )
    return " ".join(parts)


_ERROR_LOCATION = re.compile(r"\[(\d+):\d+\]")
"""BigQuery line-column error pointer pattern."""

_MAX_SNIPPET_CHARS = 120
"""Maximum character length for quoted SQL error snippets."""


def _build_sanitized_rejection(exc: Exception, sql: str) -> selector.Rejection:
    """Construct a sanitized `Rejection` from a BigQuery dry-run failure.

    Omits raw error messages to avoid exposing row data, returning only the
    BigQuery error reason and the truncated line of query text referenced in the error.

    Args:
        exc: Exception raised by BigQuery.
        sql: Query submitted to BigQuery, used to locate the error line.

    Returns:
        A `selector.Rejection` with reason `INVALID_SQL`.
    """
    errors = getattr(exc, "errors", None) or []
    first = errors[0] if errors else None
    reason = first.get("reason") if isinstance(first, dict) else None
    detail = f"BigQuery rejected the selector (reason: {reason or type(exc).__name__})."
    location = _ERROR_LOCATION.search(str(exc))
    if location is not None:
        lines = sql.splitlines()
        index = int(location.group(1)) - 1
        if 0 <= index < len(lines):
            snippet = lines[index].strip()[:_MAX_SNIPPET_CHARS]
            detail += f" Check this line of the query: {snippet!r}"
    return selector.Rejection(
        reason=selector.RejectionReason.INVALID_SQL, explanation=detail
    )


def _resolve_ingest_status(mapped: MappedRow) -> IngestStatus:
    """Classify one attempted row: what did ingestion make of it?

    Dropped outranks partial. A row that produced no case lost everything
    rather than some of it, so `MappedRow.partial` says nothing about it.

    Args:
        mapped: Mapped row result.

    Returns:
        IngestStatus outcome (NOT_INGESTED, PARTIAL, or INGESTED).
    """
    if mapped.case is None:
        return IngestStatus.NOT_INGESTED
    return IngestStatus.PARTIAL if mapped.partial else IngestStatus.INGESTED
