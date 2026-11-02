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

"""Wrapping and validation for agent-authored SQL selectors for ingestion.

A selector is a SQL query targeting specific conversations for investigation.
It supplies target session IDs for the ingestion query's targets CTE while the
outer query structure remains fixed.

Requirements for valid selectors:
* Must project a single column named `TARGET_COLUMN`, which `wrap` aliases to
  the join column required by the telemetry source.
* Must not contain a `@limit` parameter, allowing `BigQueryJobFetcher.count_scanned`
  to execute the targets subquery without limit constraints.
* Must read the source's selector table (`build_selector_table_ref`) by name and name no
  other table of its footprint (`find_table_violation`).
* Must resolve, in its dry run, to exactly that table's footprint
  (`resolve_selector_footprint`).

Security model:
The referenced-table allowlist is the primary security boundary. Because the
service account holds broad BigQuery permissions across internal datasets,
queries are not isolated by dataset permissions alone.

Each telemetry source has exactly one selector table, fixed by deployment
configuration and source rather than chosen by the agent: the telemetry table
for `big_query` and `cloud_ops`, and the agents-cli `CLOUD_LOGGING_SELECTOR_VIEW`
for `cloud_logging`, which exposes conversation content inline for `AI.IF`.
A dry run reports a logical view such as `CLOUD_LOGGING_SELECTOR_VIEW` by its
base tables in `referenced_tables`, never the view itself, so the precheck
compares against the selector table's footprint (`resolve_selector_footprint`): the
tables a legitimate selector resolves to. Linked-dataset views such as the
`cloud_ops` `_AllSpans` are reported as themselves, so their footprint is the
view alone.

Two checks enforce the rule, each covering what the other cannot see:
* The lexical table guard (`find_table_violation`) requires the selector to
  name the selector table and no other footprint table, as dataset-qualified
  references. It is the only check that tells a selector over a logical view
  from one joining the view's base tables directly, since both resolve to the
  same footprint. It inspects text, so it enforces the one-table rule rather
  than bounding data access.
* The dry runs enforce the data boundary: the referenced tables must equal the
  footprint in the canonical project, whatever the selector text says.

`BigQueryJobFetcher.precheck_selector` anchors the canonical project on the
wrapped ingestion query's dry run. The outer query's `FROM` always reads the
telemetry table, so exactly one referenced table must match the configured
telemetry dataset and table; its project is canonical. Configuration may hold
the project number (Agent Runtime injects `GOOGLE_CLOUD_PROJECT` as the number),
whereas dry runs report the project ID. Zero matches, or more than one (a
same-named table in another project), is a rejection.

Validation executes two dry runs, and each must reference exactly the footprint
tables in the canonical project:
1. Wrapped ingestion query: Verifies that executing the full query touches
   nothing beyond the footprint.
2. Standalone selector query (`build_standalone_selector`): Verifies that the selector itself
   resolves to the whole footprint. This rejects selectors that read no tables (such
   as static ID lists), that read only part of a view's footprint, and that
   reference unauthorized tables in columns the outer query would otherwise
   prune. Metadata queries (such as `INFORMATION_SCHEMA`) and foreign-project
   tables appear in `referenced_tables` and fail the comparison.

Lexical checks run beforehand, without issuing BigQuery RPCs:
`find_lexical_violation` catches unsupported statements, escape sequences in
quoted identifiers, and enforces parameter bindings (`@window_start`,
`@window_end`, `@agent_name`), then `find_table_violation` applies the table
guard. Both inspect the SQL with string literals and comments masked but quoted
identifiers visible (`_mask_literals_and_comments`), so a table or function name cannot hide in
text the scanner mistakes for a literal.

To prevent data exfiltration via error messages, query rejections return only a
structured reason code and the failing SQL line rather than raw BigQuery error text.
The scanned count remains an unavoidable numeric oracle, bounding potential leakage.
"""

from __future__ import annotations

import dataclasses
import enum
import re

TARGET_COLUMN = "target_id"
"""Expected column name projected by a selector and aliased by `wrap`."""

AI_MODEL_PLACEHOLDER = "__AI_MODEL__"
"""Placeholder token substituted with the model endpoint in `AI.IF` calls."""

WINDOW_PARAMETERS = ("@window_start", "@window_end")
"""Query parameters a selector must reference to bound the scan window."""

AGENT_PARAMETER = "@agent_name"
"""Query parameter a selector must reference to scope queries to the observed agent."""

LIMIT_PARAMETER = "@limit"
"""Query parameter reserved for ingestion CTE sampling and forbidden in selectors.

`count_scanned` binds only agent and window parameters. Rejecting `@limit`
lexically prevents selectors from passing dry-run precheck and failing during
count execution with an unbound parameter error.
"""

BIG_QUERY_SOURCE = "big_query"
"""Telemetry source key for the ADK BigQuery analytics plugin table."""

CLOUD_OPS_SOURCE = "cloud_ops"
"""Telemetry source key for the Cloud Trace linked dataset."""

CLOUD_LOGGING_SOURCE = "cloud_logging"
"""Telemetry source key whose selector table is `CLOUD_LOGGING_SELECTOR_VIEW`."""

CLOUD_LOGGING_SELECTOR_VIEW = "completions_view"
"""Selector table for the `cloud_logging` source.

agents-cli `telemetry.tf` creates this view in the telemetry dataset. It joins
the Cloud Logging sink table with the `CLOUD_LOGGING_VIEW_BASE_TABLE` BigLake
table so conversation content is available inline to `AI.IF`.
"""

CLOUD_LOGGING_VIEW_BASE_TABLE = "completions"
"""BigLake table joined by `CLOUD_LOGGING_SELECTOR_VIEW`.

agents-cli `telemetry.tf` creates it in the telemetry dataset.
"""


def resolve_selector_table_id(source: str, telemetry_table: str) -> str:
    """Returns the table ID a selector may read for a given telemetry source.

    Args:
        source: Telemetry source key (`big_query`, `cloud_ops` or `cloud_logging`).
        telemetry_table: Configured telemetry table.

    Returns:
        Table ID within the telemetry dataset.
    """
    if source == CLOUD_LOGGING_SOURCE:
        return CLOUD_LOGGING_SELECTOR_VIEW
    return telemetry_table


def build_selector_table_ref(
    source: str, *, project_id: str, dataset: str, telemetry_table: str
) -> str:
    """Resolves the one table a selector may read for a telemetry source.

    Args:
        source: Telemetry source key (`big_query`, `cloud_ops` or `cloud_logging`).
        project_id: Project that owns the telemetry dataset.
        dataset: Telemetry dataset.
        telemetry_table: Configured telemetry table.

    Returns:
        Fully-qualified ``project.dataset.table`` reference.
    """
    return f"{project_id}.{dataset}.{resolve_selector_table_id(source, telemetry_table)}"


def resolve_selector_footprint(
    source: str, telemetry_table: str
) -> frozenset[str]:
    """Returns the table names a valid selector dry run resolves to.

    A dry run reports a view's base tables rather than the view, so the
    `cloud_logging` footprint is the sink table plus the BigLake table that
    `CLOUD_LOGGING_SELECTOR_VIEW` joins.

    Args:
        source: Telemetry source key (`big_query`, `cloud_ops` or `cloud_logging`).
        telemetry_table: Configured telemetry table.

    Returns:
        Table names within the telemetry dataset.
    """
    if source == CLOUD_LOGGING_SOURCE:
        return frozenset({telemetry_table, CLOUD_LOGGING_VIEW_BASE_TABLE})
    return frozenset({telemetry_table})


_FORBIDDEN_KEYWORDS = (
    "alter",
    "begin",
    "call",
    "commit",
    "create",
    "declare",
    "delete",
    "drop",
    "execute",
    "export",
    "external_query",
    "grant",
    "insert",
    "load",
    "merge",
    "revoke",
    "rollback",
    "set",
    "truncate",
    "update",
)
"""Keywords forbidden in read-only selectors: DDL/DML statements, plus
`EXTERNAL_QUERY` (which federates to external sources bypassed by the
referenced-table check)."""

_FORBIDDEN_PATTERN = re.compile(
    rf"\b({'|'.join(_FORBIDDEN_KEYWORDS)})\b", re.IGNORECASE
)

_QUOTED_OR_COMMENTED = re.compile(
    r"""`(?:\\.|[^`\\])*`          # backtick-quoted identifier
      | '''(?:\\.|[^\\])*?'''      # triple-quoted, single
      | \"\"\"(?:\\.|[^\\])*?\"\"\" # triple-quoted, double
      | '(?:\\.|[^'\\\n])*'         # single-quoted
      | "(?:\\.|[^"\\\n])*"         # double-quoted
      | --[^\n]*                    # line comment
      | \#[^\n]*                    # line comment, hash form
      | /\*.*?\*/                   # block comment
    """,
    re.DOTALL | re.VERBOSE,
)
"""Matches quoted identifiers, string literals and comments in one alternation.

The leftmost match wins, so a quote character inside one token cannot open
another: an apostrophe in `` `v'` `` stays part of the identifier rather than
starting a literal that would mask the SQL after it.
"""


def _is_quoted_identifier(token: str) -> bool:
    """Checks whether a `_QUOTED_OR_COMMENTED` match is a backtick-quoted identifier.

    Args:
        token: Text of one match.

    Returns:
        True for a backtick-quoted identifier.
    """
    return token.startswith("`")


def _mask_literals_and_comments(selector_sql: str) -> str:
    """Replace string literals and comments with whitespace to isolate SQL keywords.

    Masking literals prevents natural language prompts (such as within `AI.IF`)
    from triggering keyword denylist rejections. Backtick-quoted identifiers
    stay visible because they name tables and functions the checks must see.

    Args:
        selector_sql: Raw SQL selector query.

    Returns:
        SQL string with literals and comments replaced by equivalent whitespace.
    """

    def mask(match: re.Match[str]) -> str:
        token = match.group(0)
        return token if _is_quoted_identifier(token) else " " * len(token)

    return _QUOTED_OR_COMMENTED.sub(mask, selector_sql)


def _has_escaped_identifier(selector_sql: str) -> bool:
    """Checks whether any backtick-quoted identifier contains a backslash.

    An escape sequence such as `` `\\x63ompletions` `` spells a name the
    lexical checks cannot read.

    Args:
        selector_sql: Raw SQL selector query.

    Returns:
        True if a backtick-quoted identifier contains a backslash.
    """
    return any(
        _is_quoted_identifier(match.group(0)) and "\\" in match.group(0)
        for match in _QUOTED_OR_COMMENTED.finditer(selector_sql)
    )


class RejectionReason(enum.StrEnum):
    """Reason categories for rejecting an agent-authored selector."""

    STATEMENT_SEPARATOR = "statement_separator"
    """Selector contains multiple SQL statements separated by semicolons."""

    FORBIDDEN_KEYWORD = "forbidden_keyword"
    """Selector contains a keyword from `_FORBIDDEN_KEYWORDS` outside a string literal."""

    UNBOUNDED_WINDOW = "unbounded_window"
    """Selector fails to reference both @window_start and @window_end."""

    MISSING_AGENT_FILTER = "missing_agent_filter"
    """Selector fails to reference @agent_name."""

    RESERVED_LIMIT_PARAMETER = "reserved_limit_parameter"
    """Selector references @limit, which only the ingestion CTE may bind."""

    INVALID_SQL = "invalid_sql"
    """BigQuery dry run rejected the query due to syntax errors or invalid projection."""

    UNAUTHORIZED_TABLE = "unauthorized_table"
    """Selector does not name the selector table, names another footprint table,
    or spells an identifier with escape sequences, or a dry run resolves tables
    other than the selector table's footprint."""


@dataclasses.dataclass(frozen=True)
class Rejection:
    """Rejection details for an invalid selector."""

    reason: RejectionReason
    """Machine-readable rejection code."""

    explanation: str
    """Actionable explanation for the caller. Omits raw BigQuery error details
    to prevent leaking table data."""


class SelectorRefused(Exception):
    """Raised when validation refuses a selector before query execution.

    Carries the `Rejection` details so callers can inspect the structured reason
    code directly.
    """

    def __init__(self, rejection: Rejection) -> None:
        super().__init__(f"{rejection.reason.value}: {rejection.explanation}")
        self.rejection = rejection


@dataclasses.dataclass(frozen=True)
class PrecheckResult:
    """Result of lexical and dry-run validation on a selector."""

    rejection: Rejection | None = None
    """Rejection details if validation failed, or None if valid."""

    referenced_tables: tuple[str, ...] = ()
    """Sorted tuple of fully-qualified table names resolved by the dry run."""

    estimated_bytes: int = 0
    """Estimated bytes scanned, reported by BigQuery dry run."""

    @property
    def is_valid(self) -> bool:
        """Whether the selector passed all validation checks."""
        return self.rejection is None


def build_standalone_selector(
    selector_sql: str,
    *,
    ai_model: str = "",
) -> str:
    """Resolves the `AI.IF` model placeholder in a selector for standalone execution.

    Substituting the placeholder produces an executable query that can be dry-run
    independently to verify which tables the selector itself accesses.

    Args:
        selector_sql: Agent-authored query projecting `TARGET_COLUMN`.
        ai_model: Model endpoint identifier for `AI.IF`.

    Returns:
        Selector query with the model placeholder resolved.
    """
    return selector_sql.replace(AI_MODEL_PLACEHOLDER, ai_model)


def wrap(
    selector_sql: str,
    id_column: str,
    *,
    ai_model: str = "",
) -> str:
    """Build the targets CTE query fragment aliasing `TARGET_COLUMN` to `id_column`.

    Deduplicates target IDs to avoid multiplying session rows in subsequent joins.

    `AI.IF` requires `endpoint` as static syntax rather than a query parameter,
    so the model placeholder is substituted before submission and dry-run
    validation. The model call runs under the query runner's credentials without
    requiring a BigQuery connection.

    Args:
        selector_sql: Agent-authored query projecting `TARGET_COLUMN`.
        id_column: Target ID column name expected by the outer query join.
        ai_model: Model endpoint identifier for `AI.IF`. Interpolated unquoted
            because it originates from deployment configuration, not agent input.

    Returns:
        CTE query fragment projecting distinct target IDs as `id_column`.
    """
    resolved = build_standalone_selector(selector_sql, ai_model=ai_model)
    return (
        f"SELECT DISTINCT {TARGET_COLUMN} AS {id_column} FROM (\n{resolved}\n)"  # noqa: S608 - agent-authored selector SQL; bounded by the selector.py table guard and dry-run allowlist
    )


def find_lexical_violation(selector_sql: str) -> Rejection | None:
    """Perform static lexical validation on an unwrapped selector query.

    Checks for single-statement structure, escape sequences in quoted
    identifiers, forbidden keywords, window parameter references, and agent
    filtering before making BigQuery dry-run calls.

    Args:
        selector_sql: Unwrapped agent-authored selector SQL.

    Returns:
        First `Rejection` encountered, or None if all lexical checks pass.

    Raises:
        ValueError: If `selector_sql` is empty or whitespace.
    """
    if not selector_sql.strip():
        raise ValueError("Cannot validate an empty selector.")
    body = _mask_literals_and_comments(selector_sql)
    if ";" in body:
        return Rejection(
            reason=RejectionReason.STATEMENT_SEPARATOR,
            explanation=(
                "The selector must be a single SELECT expression; remove the ';'."
            ),
        )
    if _has_escaped_identifier(selector_sql):
        return Rejection(
            reason=RejectionReason.UNAUTHORIZED_TABLE,
            explanation=(
                "Backtick-quoted identifiers may not contain escape sequences: "
                "spell every table, column and function name literally."
            ),
        )
    keyword = _FORBIDDEN_PATTERN.search(body)
    if keyword is not None:
        return Rejection(
            reason=RejectionReason.FORBIDDEN_KEYWORD,
            explanation=(
                f"The selector may only read: rewrite it without {keyword.group(0)!r}."
            ),
        )
    if not all(parameter in body for parameter in WINDOW_PARAMETERS):
        return Rejection(
            reason=RejectionReason.UNBOUNDED_WINDOW,
            explanation=(
                "The selector must bound its scan to the investigation window by "
                f"filtering on {WINDOW_PARAMETERS[0]} and {WINDOW_PARAMETERS[1]}."
            ),
        )
    if AGENT_PARAMETER not in body:
        return Rejection(
            reason=RejectionReason.MISSING_AGENT_FILTER,
            explanation=(
                "The selector must restrict itself to the observed agent by "
                f"filtering on {AGENT_PARAMETER}."
            ),
        )
    if LIMIT_PARAMETER in body:
        return Rejection(
            reason=RejectionReason.RESERVED_LIMIT_PARAMETER,
            explanation=(
                f"The selector may not reference {LIMIT_PARAMETER}: the budget "
                "bounds the sample the ingestion query takes from the selection, "
                "not the selection itself."
            ),
        )
    return None


def find_table_violation(
    selector_sql: str,
    *,
    dataset: str,
    selector_table_id: str,
    other_table_ids: frozenset[str],
) -> Rejection | None:
    """Checks that a selector names its selector table and no other footprint table.

    A dry run reports a logical view's base tables rather than the view, so it
    cannot tell a selector over `CLOUD_LOGGING_SELECTOR_VIEW` from one joining
    the view's base tables directly. Only the selector text distinguishes them.

    Dry runs set no default dataset, so a table reference always carries its
    dataset; only ``dataset.table`` references count, which keeps a column
    alias or a project segment spelled like a table ID from matching. Names
    are matched case-insensitively as whole identifiers in the SQL body,
    ignoring string literals and comments, so `completions_view` does not name
    `completions` and a prompt mentioning a table name is not a reference.

    Args:
        selector_sql: Unwrapped agent-authored selector SQL.
        dataset: Telemetry dataset, used to name the selector table.
        selector_table_id: Table ID of the one table the selector may read.
        other_table_ids: Other table IDs in the selector table's footprint, such
            as a view's base tables, which the selector must not name.

    Returns:
        An `UNAUTHORIZED_TABLE` rejection, or None if the selector names the
        selector table and none of `other_table_ids`.
    """
    # Quoting may split a path at any dot (`p`.`d`.`t`, `p.d`.t), so the
    # backticks are dropped to compare every spelling as one dotted path.
    body = _mask_literals_and_comments(selector_sql).replace("`", "")
    allowed = f"{dataset}.{selector_table_id}"
    named_others = sorted(
        f"{dataset}.{table}"
        for table in other_table_ids
        if _has_table_reference(body, dataset, table)
    )
    names_selector_table = _has_table_reference(
        body, dataset, selector_table_id
    )
    if names_selector_table and not named_others:
        return None
    problems = []
    if not names_selector_table:
        problems.append(f"This one does not name {allowed}.")
    if named_others:
        problems.append(
            f"Remove {', '.join(named_others)}: {allowed} is built on them, and "
            "a selector may not name them itself."
        )
    return Rejection(
        reason=RejectionReason.UNAUTHORIZED_TABLE,
        explanation=" ".join(
            [
                f"A selector must read the selector table {allowed} by name "
                "and name no other table.",
                *problems,
            ]
        ),
    )


def _has_table_reference(body: str, dataset: str, table: str) -> bool:
    """Checks whether masked SQL references ``dataset.table`` as whole identifiers.

    Args:
        body: Selector SQL masked by `_mask_literals_and_comments`, with backticks removed.
        dataset: Dataset the reference must be qualified with.
        table: Table ID to look for.

    Returns:
        True if ``dataset.table`` appears, optionally after a project segment,
        bounded by non-identifier characters.
    """
    # Project IDs and table names may contain hyphens, so a hyphen continues
    # an identifier rather than ending it. A leading dot is allowed as the
    # separator after a project segment.
    pattern = (
        rf"(?<![\w-]){re.escape(dataset)}\s*\.\s*{re.escape(table)}(?![\w-])"
    )
    return re.search(pattern, body, re.IGNORECASE) is not None
