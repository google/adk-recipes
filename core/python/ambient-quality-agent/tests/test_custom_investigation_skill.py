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

"""Tests for the custom investigation skill and its per-source references.

Verifies that the static skill sends the model to `describe_telemetry` for the
selector table, that each `references/<source>.md` names only its own source,
and that every SQL recipe clears the production guards and names columns its
table defines. The `big_query` table definition is parsed from the installed
ADK BigQuery Agent Analytics plugin; the `cloud_logging` and `cloud_ops` views
are defined in `tests/fixtures/telemetry_schemas/`.
"""

from __future__ import annotations

import ast
import dataclasses
import functools
import json
import re
from pathlib import Path
from typing import Any

import ambient_quality_agent
import google.adk
import pytest
from ambient_quality_agent import config as config_module
from ambient_quality_agent.core import custom_investigation_skill, skill_toolset
from ambient_quality_agent.tools.ingestion import (
    bigquery_fetcher,
    cloud_ops_fetcher,
    selector,
    trace_converter,
)

SOURCES = ("big_query", "cloud_ops", "cloud_logging")

_PLACEHOLDER = "SELECTOR_TABLE"
"""How the references write the table `describe_telemetry` returns."""

_FIXTURES = Path(__file__).parent / "fixtures" / "telemetry_schemas"

_SPAN_ATTRIBUTE_READERS = Path(trace_converter.__file__).read_text(
    encoding="utf-8"
) + Path(cloud_ops_fetcher.__file__).read_text(encoding="utf-8")
"""Telemetry reader code used to verify that recipe span attribute keys are valid."""

_ADK_PLUGIN_PATH = (
    Path(google.adk.__file__).parent
    / "plugins"
    / "bigquery_agent_analytics_plugin.py"
)

_TABLES = {
    "big_query": "agent_events",
    "cloud_ops": "_AllSpans",
    "cloud_logging": selector.CLOUD_LOGGING_SELECTOR_VIEW,
}
"""Default selector table names keyed by telemetry source."""

_OTHER_TABLE_NAMES = {
    "agent_events",
    "_AllSpans",
    "completions_view",
    "aiplatform_googleapis_com_reasoning_engine_stdout",
    "gen_ai_client_inference_operation_details",
}
"""Table names across all sources, used to verify source isolation in references."""

# JSON fields accessed via dotted notation that must be documented in the skill.
_JSON_KEYS = {
    "big_query": {
        "text_summary",
        "prompt",
        "system_prompt",
        "response",
        "usage",
        "completion",
        "total",
        "tool",
        "args",
        "result",
        "tool_origin",
        "error_traceback",
        "artifacts",
        "state",
        "usage_metadata",
        "model_version",
        "root_agent_name",
        "session_metadata",
        "adk",
        "total_ms",
        "model",
        "custom_tags",
        "labels",
        "otel",
    },
    "cloud_ops": set(),
    "cloud_logging": set(),
}

_ABSENCE_PHRASES = (
    "no error column",
    "not expressible",
    "what is:",
    "records no",
    "no trace ids",
    "not in this table",
    "is not present",
    "are not present",
    "not available on this source",
)
"""Phrases asserting missing data or unsupported fields forbidden in skill prose."""

# Named arguments for AI.IF function calls, excluded from column validation.
_CALL_ARGUMENTS = {"endpoint"}

_SQL_BLOCK = re.compile(r"^```sql\n(.*?)^```", re.DOTALL | re.MULTILINE)
_STRING_OR_TABLE = re.compile(r"r?'[^']*'|\"[^\"]*\"|`[^`]*`", re.DOTALL)
_PARAMETER = re.compile(r"@[a-z_]+")
_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_]*(?:\.[A-Za-z_][A-Za-z0-9_]*)*")
_ALIAS = re.compile(r"\bAS\s+([a-z_][a-z0-9_]*)")
_JSON_PATH_KEY = re.compile(r"\$\.\"([^\"]+)\"")
_NOTED_COLUMNS_SECTION = re.compile(
    r"^### Columns worth knowing\n(.*?)(?=^#)", re.DOTALL | re.MULTILINE
)
_NOTED_COLUMN_BULLET = re.compile(r"^- (.*?) -- ", re.MULTILINE)

_A2A_RESPONSE_KEYS = {"artifacts", "state"}
"""A2A task response keys stored verbatim in `content` by the analytics plugin."""


def _config(source: str) -> config_module.Config:
    """Builds a test configuration for a specific telemetry source.

    Args:
        source: Telemetry source key.

    Returns:
        Config instance populated with source-specific mock coordinates.
    """
    return dataclasses.replace(
        config_module.config,
        telemetry_ingestion_source=source,
        project_id=f"fake-project-{source.replace('_', '-')}",
        telemetry_dataset=f"fake_dataset_{source}",
        telemetry_table=config_module.DEFAULT_TELEMETRY_TABLES[source],
    )


def _selector_table(source: str) -> str:
    cfg = _config(source)
    return f"{cfg.project_id}.{cfg.telemetry_dataset}.{_TABLES[source]}"


def _read_skill_md() -> str:
    """Reads the skill's source-neutral instructions.

    Returns:
        The `SKILL.md` body, without frontmatter.
    """
    return custom_investigation_skill.load_custom_investigation_skill().instructions


def _read_raw_reference(source: str) -> str:
    """Reads a source's reference as the skill ships it.

    Args:
        source: Telemetry source key.

    Returns:
        The reference text, with the `SELECTOR_TABLE` placeholder intact.
    """
    content = custom_investigation_skill.load_custom_investigation_skill().resources.get_reference(
        f"{source}.md"
    )
    assert isinstance(content, str), source
    return content


def _read_reference(source: str) -> str:
    """Reads a source's reference with the placeholder replaced, as the model must.

    Args:
        source: Telemetry source key.

    Returns:
        The reference text naming the test configuration's selector table.
    """
    return _read_raw_reference(source).replace(
        _PLACEHOLDER, _selector_table(source)
    )


def _read_instructions(source: str) -> str:
    """Joins what the model reads for a source: `SKILL.md` and its reference.

    Args:
        source: Telemetry source key.

    Returns:
        The skill instructions followed by the source's reference.
    """
    return f"{_read_skill_md()}\n\n{_read_reference(source)}"


def _flat(text: str) -> str:
    """Collapses whitespace so phrase assertions survive paragraph reflowing.

    Args:
        text: Input string with arbitrary whitespace formatting.

    Returns:
        String with whitespace sequences collapsed to single spaces.
    """
    return " ".join(text.split())


def _list_recipes(source: str) -> list[str]:
    """Extracts runnable selector query examples from a source's reference.

    Filters for SQL blocks binding `@window_start` to ignore illustrative snippets.

    Args:
        source: Telemetry source key.

    Returns:
        List of SQL selector queries, naming the test configuration's table.
    """
    return [
        block.group(1)
        for block in _SQL_BLOCK.finditer(_read_reference(source))
        if "@window_start" in block.group(1)
    ]


_ALL_RECIPES = [
    (source, sql) for source in SOURCES for sql in _list_recipes(source)
]


def _schema_paths(fields: list[dict[str, Any]], prefix: str = "") -> set[str]:
    """Flattens a schema definition into dotted column paths.

    Args:
        fields: Schema field dictionaries containing `name` and optional nested `fields`.
        prefix: Dotted path prefix of the parent record.

    Returns:
        Set of dotted column and nested field paths (e.g. `status.code`).
    """
    paths: set[str] = set()
    for field in fields:
        path = f"{prefix}{field['name']}"
        paths.add(path)
        paths |= _schema_paths(field.get("fields", []), f"{path}.")
    return paths


def _fixture(name: str) -> dict[str, Any]:
    return json.loads((_FIXTURES / name).read_text(encoding="utf-8"))


@dataclasses.dataclass(frozen=True)
class _PluginSchema:
    """The events table definition written by the ADK analytics plugin.

    Attributes:
        columns: Schema field dictionaries with `name`, `type`, `mode` and
            nested `fields`, the shape the view fixtures use.
        event_types: Every `event_type` value the plugin writes.
    """

    columns: list[dict[str, Any]]
    event_types: frozenset[str]


@functools.cache
def _parse_adk_plugin_schema() -> _PluginSchema:
    """Parses the events table definition out of the installed ADK plugin.

    Parses the plugin source via AST to avoid runtime dependency errors from
    optional storage libraries. Skips the calling test when the plugin is not
    installed; the skip is raised before anything is cached.

    Returns:
        The plugin's columns and event types.
    """
    if not _ADK_PLUGIN_PATH.is_file():
        pytest.skip(
            f"ADK BigQuery analytics plugin not installed: {_ADK_PLUGIN_PATH}"
        )
    tree = ast.parse(_ADK_PLUGIN_PATH.read_text(encoding="utf-8"))
    schema_fn = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef)
        and node.name == "_get_events_schema"
    )
    returned = next(
        node for node in ast.walk(schema_fn) if isinstance(node, ast.Return)
    )
    assert isinstance(returned.value, ast.List)
    view_defs = _module_assignment(tree, "_EVENT_VIEW_DEFS")
    hitl_map = _module_assignment(tree, "_HITL_EVENT_MAP")
    assert isinstance(view_defs, ast.Dict)
    assert isinstance(hitl_map, ast.Call)
    hitl_events = ast.literal_eval(hitl_map.args[0]).values()
    return _PluginSchema(
        columns=[_schema_field(item) for item in returned.value.elts],
        event_types=frozenset(
            {ast.literal_eval(key) for key in view_defs.keys if key is not None}
            | {f"{event}_COMPLETED" for event in hitl_events}
        ),
    )


def _list_defined_columns(source: str) -> set[str]:
    """Loads the column paths a telemetry source's table defines.

    Args:
        source: Telemetry source key.

    Returns:
        Set of dotted column paths, from the ADK plugin for `big_query` and
        from the view fixtures otherwise.
    """
    if source == "big_query":
        return _schema_paths(_parse_adk_plugin_schema().columns)
    if source == "cloud_logging":
        return _schema_paths(_fixture("completions_view.json")["columns"])
    return _schema_paths(_fixture("all_spans_view.json")["columns"])


def _identifiers(sql: str) -> set[str]:
    """Extracts lowercase column and field identifiers from a SQL query.

    Because examples use uppercase for SQL keywords and functions, isolating
    lowercase tokens reveals referenced columns and struct fields after
    stripping string literals, parameters, and aliases.

    Args:
        sql: The SQL query string to parse.

    Returns:
        Set of lowercase column and field identifiers referenced in the query.
    """
    aliases = set(_ALIAS.findall(sql))
    masked = _PARAMETER.sub(" ", _STRING_OR_TABLE.sub(" ", sql))
    parts: set[str] = set()
    for token in _IDENTIFIER.findall(masked):
        parts.update(token.split("."))
    return {
        part
        for part in parts
        if part.islower()
        and part not in aliases
        and part not in _CALL_ARGUMENTS
    }


def _mixed_case_identifiers(sql: str) -> set[str]:
    """Extracts identifiers that mix uppercase and lowercase characters.

    Args:
        sql: The SQL query string to inspect.

    Returns:
        Set of mixed-case identifier tokens found in the query.
    """
    masked = _PARAMETER.sub(" ", _STRING_OR_TABLE.sub(" ", sql))
    parts: set[str] = set()
    for token in _IDENTIFIER.findall(masked):
        parts.update(token.split("."))
    return {part for part in parts if not part.islower() and not part.isupper()}


def _module_assignment(tree: ast.Module, name: str) -> ast.expr:
    """Finds the AST expression assigned to a module-level variable.

    Args:
        tree: Parsed module AST.
        name: Target variable name.

    Returns:
        The assigned AST expression.
    """
    for node in tree.body:
        if isinstance(node, ast.Assign):
            targets = node.targets
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
        else:
            continue
        if any(isinstance(t, ast.Name) and t.id == name for t in targets):
            assert node.value is not None
            return node.value
    raise AssertionError(f"{name} is not assigned in {_ADK_PLUGIN_PATH}")


def _schema_field(call: ast.expr) -> dict[str, Any]:
    """Converts a `SchemaField` AST call into a schema field dictionary.

    Args:
        call: AST call node representing a `SchemaField` instantiation.

    Returns:
        Dictionary containing the field's name, type, mode, and any nested fields.
    """
    assert isinstance(call, ast.Call)
    keywords = {kw.arg: kw.value for kw in call.keywords}
    field: dict[str, Any] = {
        "name": ast.literal_eval(call.args[0]),
        "type": ast.literal_eval(call.args[1]),
        "mode": ast.literal_eval(keywords["mode"]),
    }
    if "fields" in keywords:
        nested = keywords["fields"]
        assert isinstance(nested, ast.List)
        field["fields"] = [_schema_field(item) for item in nested.elts]
    return field


# --- the skill itself ------------------------------------------------------ #


def test_the_skill_loads_and_its_frontmatter_parses() -> None:
    skill = custom_investigation_skill.load_custom_investigation_skill()

    assert skill.name == custom_investigation_skill.SKILL_NAME
    assert skill.description
    assert skill.instructions


def test_the_description_makes_no_per_source_capability_claims() -> None:
    skill = custom_investigation_skill.load_custom_investigation_skill()

    for claim in ("human", "agent-to-agent", "HITL", "A2A", "revision"):
        assert claim not in skill.description, claim


def test_the_skill_ships_inside_the_installed_package() -> None:
    """Verifies the skill resides in the package since root skills/ is not deployed."""
    package_root = Path(ambient_quality_agent.__file__).parent

    assert (custom_investigation_skill.SKILL_DIR / "SKILL.md").is_file()
    assert custom_investigation_skill.SKILL_DIR.resolve().is_relative_to(
        package_root.resolve()
    )


def test_the_skill_bundles_no_scripts_and_one_reference_per_source() -> None:
    """Each supported source's recipes are loadable through `load_skill_resource`."""
    skill = custom_investigation_skill.load_custom_investigation_skill()

    assert skill.resources.list_scripts() == []
    assert skill.resources.list_assets() == []
    assert sorted(skill.resources.list_references()) == sorted(
        f"{source}.md"
        for source in config_module.ALLOWED_TELEMETRY_INGESTION_SOURCES
    )


@pytest.mark.parametrize("source", SOURCES)
def test_the_recipes_resource_names_a_reference_the_skill_carries(
    source: str,
) -> None:
    recipes = custom_investigation_skill.resolve_selector_recipes(source)
    skill = custom_investigation_skill.load_custom_investigation_skill()

    assert recipes == {
        "skill_name": skill.name,
        "file_path": f"references/{source}.md",
    }
    assert (
        skill.resources.get_reference(
            recipes["file_path"].removeprefix("references/")
        )
        is not None
    )


def test_the_recipes_resource_refuses_an_unknown_source() -> None:
    with pytest.raises(ValueError, match="Unknown telemetry source"):
        custom_investigation_skill.resolve_selector_recipes("stdout")


def test_the_agents_toolset_carries_both_skills() -> None:
    """The skill reaches the agent only through the shared toolset."""
    toolset = skill_toolset.build_skill_toolset()

    assert set(toolset._skills) == {
        "rca",
        custom_investigation_skill.SKILL_NAME,
    }
    assert (
        custom_investigation_skill.load_custom_investigation_skill()
        in toolset._skills.values()
    )


def test_the_skill_sends_the_model_to_describe_telemetry_for_the_table() -> (
    None
):
    instructions = _flat(_read_skill_md())

    assert "Call `describe_telemetry`" in instructions
    assert "`load_skill_resource`" in instructions
    assert "`selector_recipes`" in instructions
    assert f"`{_PLACEHOLDER}`" in instructions
    assert "using only columns the recipes use" in instructions
    assert "the preview's dry run refuses any column the table lacks" in (
        instructions
    )
    workflow = instructions[instructions.index("## Workflow") :]
    assert workflow.index("`describe_telemetry`") < workflow.index(
        "Write the selector"
    )


def test_the_skill_says_the_live_columns_are_the_full_definition() -> None:
    instructions = _flat(_read_skill_md())

    assert (
        "The columns `describe_telemetry` returns are the table's live "
        "definition" in instructions
    )
    assert "column list below" not in instructions


# --- one source per reference ---------------------------------------------- #


def test_the_skill_names_no_source_table() -> None:
    instructions = _read_skill_md()

    for table in _OTHER_TABLE_NAMES:
        assert table not in instructions, table


@pytest.mark.parametrize("source", SOURCES)
def test_each_reference_names_only_its_own_tables(source: str) -> None:
    reference = _read_raw_reference(source)

    for other in _OTHER_TABLE_NAMES - {_TABLES[source]}:
        assert other not in reference, other


@pytest.mark.parametrize("source", SOURCES)
def test_each_reference_writes_the_table_as_the_placeholder(
    source: str,
) -> None:
    reference = _read_raw_reference(source)

    assert reference.count("## The selector table:") == 1
    assert f"`{_PLACEHOLDER}`" in reference
    assert "PROJECT.DATASET.TABLE" not in reference
    for sql in _SQL_BLOCK.finditer(reference):
        if "@window_start" in sql.group(1):
            assert f"`{_PLACEHOLDER}`" in sql.group(1)


@pytest.mark.parametrize("source", SOURCES)
def test_no_reference_claims_to_list_every_column(source: str) -> None:
    """The live schema from `describe_telemetry` is the column list of record."""
    raw = _read_raw_reference(source)
    reference = _flat(raw)

    assert not re.search(r"^### Columns$", raw, re.MULTILINE)
    assert "This is every column" not in reference
    assert "`describe_telemetry` lists every column" in reference


@pytest.mark.parametrize("source", SOURCES)
def test_every_noted_column_is_one_its_table_defines(source: str) -> None:
    """Verifies that every column noted in the reference exists in its table."""
    section = _NOTED_COLUMNS_SECTION.search(_read_raw_reference(source))
    assert section is not None, "no '### Columns worth knowing' section"
    noted = {
        name
        for names in _NOTED_COLUMN_BULLET.findall(section.group(1))
        for name in re.findall(r"`([a-z_.]+)`", names)
    }

    assert noted
    assert noted <= _list_defined_columns(source), sorted(
        noted - _list_defined_columns(source)
    )


@pytest.mark.parametrize("source", SOURCES)
def test_the_skill_makes_no_absence_claims(source: str) -> None:
    instructions = _flat(_read_instructions(source)).lower()

    for phrase in _ABSENCE_PHRASES:
        assert not re.search(
            rf"(?<!\w){re.escape(phrase)}(?!\w)", instructions
        ), phrase


def test_the_skill_separates_missing_columns_from_empty_ones() -> None:
    instructions = _flat(_read_skill_md())

    assert "A column that exists can still be empty" in instructions
    assert "`matched`" in instructions


def test_the_cloud_logging_reference_warns_that_token_counts_are_strings() -> (
    None
):
    reference = _flat(_read_reference("cloud_logging"))

    assert "compares lexically" in reference
    assert "SAFE_CAST" in reference
    assert "Deduplicate per `span_id`" in reference


def test_the_cloud_ops_reference_says_attribute_keys_depend_on_the_agent() -> (
    None
):
    reference = _flat(_read_reference("cloud_ops"))

    assert "The column set is fixed; the keys inside `attributes` are not." in (
        reference
    )


# --- the column definitions the skill is checked against ------------------- #


def test_the_adk_plugin_parse_finds_what_the_skill_depends_on() -> None:
    """Fails loudly if a plugin refactor breaks the parse.

    Every `big_query` check below compares against this parse, so a parse that
    silently found less would weaken them all.
    """
    parsed = _parse_adk_plugin_schema()

    assert {
        "timestamp",
        "event_type",
        "agent",
        "session_id",
        "span_id",
        "content",
        "attributes",
        "error_message",
    } <= _schema_paths(parsed.columns)
    assert {
        "USER_MESSAGE_RECEIVED",
        "LLM_RESPONSE",
        "TOOL_STARTING",
        "TOOL_ERROR",
        "AGENT_RESPONSE",
        "A2A_INTERACTION",
        "HITL_INPUT_REQUEST",
        "HITL_INPUT_REQUEST_COMPLETED",
    } <= parsed.event_types


# --- the selector contract ------------------------------------------------- #


@pytest.mark.parametrize("source", SOURCES)
def test_there_are_worked_examples_for_every_source(source: str) -> None:
    assert len(_list_recipes(source)) >= 2


@pytest.mark.parametrize(("source", "sql"), _ALL_RECIPES)
def test_every_example_clears_the_production_validator(
    source: str, sql: str
) -> None:
    """Verifies that all examples pass the runtime lexical validator."""
    del source
    assert selector.find_lexical_violation(sql) is None


@pytest.mark.parametrize(("source", "sql"), _ALL_RECIPES)
def test_every_example_projects_the_target_column(
    source: str, sql: str
) -> None:
    del source
    assert f"AS {selector.TARGET_COLUMN}" in sql


@pytest.mark.parametrize(("source", "sql"), _ALL_RECIPES)
def test_no_example_binds_the_reserved_limit_parameter(
    source: str, sql: str
) -> None:
    """Verifies no example binds @limit, which breaks the pre-run count query."""
    del source
    assert selector.LIMIT_PARAMETER not in sql


@pytest.mark.parametrize(("source", "sql"), _ALL_RECIPES)
def test_every_example_reads_the_selector_table(source: str, sql: str) -> None:
    assert f"`{_selector_table(source)}`" in sql


@pytest.mark.parametrize(("source", "sql"), _ALL_RECIPES)
def test_every_example_clears_the_table_guard(source: str, sql: str) -> None:
    """Verifies that all examples pass the runtime table guard for their source."""
    cfg = _config(source)
    table_id = selector.resolve_selector_table_id(source, cfg.telemetry_table)

    assert (
        selector.find_table_violation(
            sql,
            dataset=cfg.telemetry_dataset,
            selector_table_id=table_id,
            other_table_ids=selector.resolve_selector_footprint(
                source, cfg.telemetry_table
            )
            - {table_id},
        )
        is None
    )


# --- grounding: the examples name columns the table has -------------------- #


@pytest.mark.parametrize("source", SOURCES)
def test_every_json_key_the_examples_may_use_is_documented(source: str) -> None:
    instructions = _read_instructions(source)

    for key in _JSON_KEYS[source]:
        assert re.search(rf"[.`]{re.escape(key)}`", instructions), key


def test_every_big_query_json_key_is_one_the_plugin_writes() -> None:
    """Verifies that JSON allowlist keys correspond to fields written by the plugin."""
    if not _ADK_PLUGIN_PATH.is_file():
        pytest.skip(
            f"ADK BigQuery analytics plugin not installed: {_ADK_PLUGIN_PATH}"
        )
    plugin_source = _ADK_PLUGIN_PATH.read_text(encoding="utf-8")

    for key in _JSON_KEYS["big_query"] - _A2A_RESPONSE_KEYS:
        assert f'"{key}"' in plugin_source, key


def test_every_span_attribute_key_is_one_aqua_reads() -> None:
    """Verifies recipe span attribute keys against telemetry reader code.

    Guards against typos in JSON path string literals, which are masked during
    general SQL column validation.
    """
    keys = {
        key
        for key in _JSON_PATH_KEY.findall(_read_instructions("cloud_ops"))
        if "<" not in key
    }

    assert keys
    for key in keys:
        assert key in _SPAN_ATTRIBUTE_READERS, key


@pytest.mark.parametrize(("source", "sql"), _ALL_RECIPES)
def test_every_example_names_columns_its_table_has(
    source: str, sql: str
) -> None:
    leaves = {path.rsplit(".", 1)[-1] for path in _list_defined_columns(source)}
    allowed = leaves | _JSON_KEYS[source] | {selector.TARGET_COLUMN}

    unknown = _identifiers(sql) - allowed
    assert not unknown, (
        f"{source} example names unknown columns: {sorted(unknown)}"
    )


@pytest.mark.parametrize(("source", "sql"), _ALL_RECIPES)
def test_every_example_keeps_the_casing_the_column_check_depends_on(
    source: str, sql: str
) -> None:
    """Enforces uppercase for SQL keywords and lowercase for column identifiers."""
    del source
    assert not _mixed_case_identifiers(sql)


def test_the_prose_lists_every_event_type_the_plugin_writes() -> None:
    listed = re.search(
        r"`event_type` is one of(.+?)\.\n",
        _read_instructions("big_query"),
        re.DOTALL,
    )

    assert listed is not None
    assert (
        set(re.findall(r"`([A-Z0-9_]+)`", listed.group(1)))
        == _parse_adk_plugin_schema().event_types
    )


@pytest.mark.parametrize("sql", _list_recipes("big_query"))
def test_every_event_type_literal_is_one_the_plugin_writes(sql: str) -> None:
    event_types = _parse_adk_plugin_schema().event_types
    literals = {
        value
        for value in re.findall(r"'([A-Z][A-Z0-9_]+)'", sql)
        if value.isupper() and "_" in value
    }

    assert literals <= event_types, sorted(literals - event_types)


@pytest.mark.parametrize("sql", _list_recipes("big_query"))
def test_every_big_query_example_matches_the_observed_agent_as_ingestion_does(
    sql: str,
) -> None:
    """Matching `agent` alone misses sessions where the Runner starts at a
    sub-agent, which ingestion includes."""
    assert f"{bigquery_fetcher._OBSERVED_AGENT_NAME_SQL} = @agent_name" in sql
    assert re.search(r"\bagent = @agent_name", sql) is None


# --- semantic selection ---------------------------------------------------- #


@pytest.mark.parametrize("source", ["big_query", "cloud_logging"])
def test_the_semantic_examples_use_the_model_placeholder_and_no_connection(
    source: str,
) -> None:
    """Verifies that AI.IF recipes use the model placeholder and omit connection_id."""
    recipes = _list_recipes(source)
    semantic = [sql for sql in recipes if "AI.IF" in sql]

    assert semantic
    for sql in semantic:
        assert selector.AI_MODEL_PLACEHOLDER in sql
    for sql in recipes:
        assert "connection_id" not in sql


@pytest.mark.parametrize("source", SOURCES)
def test_the_skill_argues_for_narrowing_before_the_model_call(
    source: str,
) -> None:
    instructions = _flat(_read_instructions(source))

    assert "Narrow first" in instructions
    assert "per row" in instructions
    assert "quota" in instructions


@pytest.mark.parametrize("source", SOURCES)
def test_the_skill_carries_the_tools_own_limits(source: str) -> None:
    """Verifies that instructions state retry thresholds and review requirements."""
    instructions = _flat(_read_instructions(source))

    assert "three refusals per conversation" in instructions
    assert "attempts_exhausted" in instructions
    assert "session_review" in instructions


# --- the guidance that has no runtime enforcement -------------------------- #


@pytest.mark.parametrize("source", SOURCES)
def test_the_skill_states_capabilities_before_asking_anything(
    source: str,
) -> None:
    instructions = _flat(_read_instructions(source))

    assert "Open by saying what you can do" in instructions
    assert "What are you trying to find out?" in instructions


@pytest.mark.parametrize("source", SOURCES)
def test_the_skill_puts_group_conditions_in_having(source: str) -> None:
    instructions = _flat(_read_instructions(source))

    assert "Conditions on the group go in `HAVING`" in instructions
    assert "HAVING" in instructions


@pytest.mark.parametrize("source", SOURCES)
def test_the_workflow_previews_and_asks_before_it_starts_a_run(
    source: str,
) -> None:
    instructions = _flat(_read_instructions(source))

    assert "preview_custom_investigation" in instructions
    assert "start_custom_investigation" in instructions
    assert "get approval" in instructions
    assert (
        "The examples are not the conversations the run will review."
        in instructions
    )


@pytest.mark.parametrize("source", SOURCES)
def test_the_workflow_reads_the_memories_before_writing_the_selector(
    source: str,
) -> None:
    """A query or column the developer asked AQuA to remember is reused
    instead of rediscovered. Text assertion: the step is in the skill, not
    proof the model takes it."""
    instructions = _flat(_read_instructions(source))

    assert instructions.index("Call `get_memories` once") < instructions.index(
        "Write the selector"
    )
    assert "Never call `remember` on your own initiative" in instructions


@pytest.mark.parametrize("source", SOURCES)
def test_the_skill_gives_a_markdown_template_for_linking_every_example(
    source: str,
) -> None:
    """Verifies that instructions provide exact markdown templates for example links.

    Pins the markdown format instruction so example conversations are linked
    to dashboard paths rather than output as plain text.
    """
    instructions = _flat(_read_instructions(source))

    assert (
        "- [<trajectory_id>](<case_view_path>) -- <turn_count> turns -- "
        '"<first_user_message>" -- [Cloud Trace](<trace_url>)' in instructions
    )
    assert (
        "Every example gets its `case_view_path` link, every time."
        in instructions
    )
    assert (
        "Append the `[Cloud Trace](<trace_url>)` segment only when the example "
        "actually carries `trace_url`." in instructions
    )


@pytest.mark.parametrize("source", SOURCES)
def test_the_skill_separates_narrowing_scope_from_goal_seeking(
    source: str,
) -> None:
    instructions = _flat(_read_instructions(source))

    assert "session_review_focus" in instructions
    assert "report only problems with the refund tool" in instructions
    assert "find evidence the refund tool is broken" in instructions


@pytest.mark.parametrize("source", SOURCES)
def test_the_skill_states_that_windows_are_utc_instants_with_an_offset(
    source: str,
) -> None:
    instructions = _flat(_read_instructions(source))

    assert "Windows are UTC" in instructions
    assert "offset" in instructions


@pytest.mark.parametrize("source", SOURCES)
def test_the_skill_warns_that_an_old_window_refreshes_what_it_finds(
    source: str,
) -> None:
    instructions = _flat(_read_instructions(source))

    assert (
        "An old window is allowed, but it refreshes what it finds"
        in instructions
    )
    assert "auto-resolve" in instructions


@pytest.mark.parametrize("source", SOURCES)
def test_the_skill_explains_how_to_replay_a_past_run(source: str) -> None:
    instructions = _flat(_read_instructions(source))

    assert "custom_overrides" in instructions
    assert "get_investigation" in instructions
    assert "list_investigations" in instructions
