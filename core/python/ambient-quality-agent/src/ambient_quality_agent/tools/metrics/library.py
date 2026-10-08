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

"""Load custom metrics from an agents-cli eval config in memory.

Pairs eval config specifications with in-memory Python modules so the same
metrics grade locally and score sweeps. A metric marked remote, or one that is
only a prompt template, is loaded for the eval service to run or judge instead.
"""

from __future__ import annotations

import ast
import dataclasses
import hashlib
import logging
import math
import re
import sys
import types
from typing import TYPE_CHECKING, Any, ClassVar

import yaml
from agentplatform._genai.types import CodeExecutionMetric, LLMMetric
from ambient_quality_agent.tools.metrics.timeout import wrap_with_timeout
from ambient_quality_agent.tools.transient_retry import (
    wrap_with_transient_retry,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

logger = logging.getLogger(__name__)

CONFIG_NAMES = ("eval_config.yaml", "eval_config.yml", "eval_config.json")
"""Supported eval config filenames, in lookup order."""

_REMOTE_KEYS = ("remote_custom_function",)
"""Keys specifying a function executed remotely by the eval service.

Along with `execution: remote`, marks a metric to run on external eval service
infrastructure to avoid running untrusted code locally or exposing agent
credentials.
"""

LOCAL_EXECUTION = "local"
REMOTE_EXECUTION = "remote"
"""Execution setting directing a `custom_function` to run on the eval service.

Because `agents-cli` defaults `execution` to `local`, explicitly setting
`remote` sends the function source in a `CodeExecutionMetric` for remote execution.
"""

EXECUTIONS = (LOCAL_EXECUTION, REMOTE_EXECUTION)
"""Valid `execution` values accepted from an `agents-cli` config.

Unrecognized values are rejected to prevent misconfigurations (such as
capitalized `Remote`) from defaulting to local execution and running untrusted
code under local credentials.
"""

_RUBRIC_GROUP_KEY = "rubric_group_name"
"""Key specifying the rubric group graded by the eval service.

Valid only for managed rubric metrics; custom metrics compute verdicts directly.
"""


def _is_sdk_computed(name: str) -> bool:
    """Checks whether the metric is computed directly by the SDK from its name.

    `exact_match`, `bleu`, and `rouge*` metrics are built into the SDK without
    appearing in the predefined metric registry.

    Args:
        name: Name of the metric to check.

    Returns:
        True if the metric is computed by the SDK without an explicit body;
        False otherwise.
    """
    lowered = name.lower()
    return lowered in ("exact_match", "bleu") or lowered.startswith("rouge")


_LOCAL_FUNCTION_KEYS = ("custom_function_file", "custom_function")
"""Config keys designating a custom function executed locally."""

_FUNCTION_KEYS = (*_REMOTE_KEYS, *_LOCAL_FUNCTION_KEYS)
"""All config keys that provide an executable function."""

_JUDGE_KEYS = ("prompt_template",)
"""Config keys identifying a metric the eval service judges with a model."""

PREDEFINED_KIND = "predefined"
REMOTE_KIND = "remote"
JUDGED_KIND = "judged"

_MODEL_MODULES = (
    "google.genai",
    "google.generativeai",
    "vertexai",
    "openai",
    "anthropic",
    "litellm",
)
"""Model client modules used to detect whether a metric invokes an LLM.

Metrics importing these modules may incur per-session API charges on local
engine credentials. Identifying them allows flagging potential costs in run
summaries without blocking execution.
"""


@dataclasses.dataclass(frozen=True)
class CodeMetric:
    """A loaded evaluation metric and its expected behavior contract."""

    name: str
    expected: str
    """Expected agent behavior, mapped to `Finding.expected_behavior`."""

    evaluate: Callable[[dict[str, Any]], Any]
    """Callable that evaluates a session instance, wrapped with timeouts and retries."""

    model_modules: tuple[str, ...] = ()
    """Model client modules imported by this metric, if any."""

    threshold: float = 1.0
    """Score at or above which the metric passes."""

    def __post_init__(self) -> None:
        # Bound each retry attempt with an independent timeout rather than
        # sharing a single deadline across all attempts.
        object.__setattr__(
            self,
            "evaluate",
            wrap_with_transient_retry(wrap_with_timeout(self.evaluate)),
        )


@dataclasses.dataclass(frozen=True)
class RemoteCodeMetric:
    """Metric executed remotely by the eval service.

    Holds uncompiled source code rather than a callable so external code is
    neither imported nor executed locally, avoiding local side effects and
    credential exposure.
    """

    kind: ClassVar[str] = REMOTE_KIND

    name: str
    """Metric name as specified in the evaluation config."""

    service_name: str
    """Canonical name generated by the SDK for result matching.

    The SDK lowercases `Metric.name` when reporting results. Precomputing this
    canonical name prevents join mismatches against uppercase config names,
    which would otherwise appear as missing verdicts.
    """

    expected: str
    """Expected agent behavior, mapped to `Finding.expected_behavior`."""

    source: str
    """Python source defining `evaluate(instance)`, sent to the eval service."""

    threshold: float = 1.0
    """Passing score threshold."""


@dataclasses.dataclass(frozen=True)
class RemoteJudgeMetric:
    """A metric the eval service's autorater judges from a prompt template.

    Nothing here is executed on either side: the template travels in an SDK
    `LLMMetric`, and the service renders it per session and reads a score and
    an explanation back. Built with `LLMMetric.model_validate(entry)` as
    `agents-cli` builds it, so every judge setting that tool passes through
    passes through here, and every value the SDK refuses is refused here.
    """

    kind: ClassVar[str] = JUDGED_KIND

    name: str
    """The metric's name as the eval config spells it, used in reports."""

    service_name: str
    """The same metric, named as the eval service will name it back; see
    `RemoteCodeMetric.service_name`."""

    expected: str
    """Expected agent behavior, mapped to `Finding.expected_behavior`."""

    metric: LLMMetric
    """The SDK metric sent as-is, named `service_name`."""

    threshold: float | None = None
    """Score at or above which the metric passes. None, as when the config sets
    no `threshold`, reports scores only: agents-cli has no pass mark for a
    judge, so no session fails and no finding is filed."""


RemoteMetric = RemoteCodeMetric | RemoteJudgeMetric
"""A metric the eval service runs or judges, sent in one call with its peers."""


class MetricLibrary(dict[str, "CodeMetric"]):
    """Collection of loaded local metrics along with remote and failed entries.

    Acts as a mapping for local metrics while tracking remote and judged
    metrics, load failures, and skipped entries so the execution node can
    report full status in run summaries.
    """

    failures: tuple[tuple[str, str], ...] = ()

    remote: tuple[RemoteCodeMetric, ...] = ()
    """Metrics designated for remote execution by the eval service."""

    judged: tuple[RemoteJudgeMetric, ...] = ()
    """Metrics the eval service judges with a model; see `RemoteJudgeMetric`."""

    declined: tuple[tuple[str, str, str], ...] = ()
    """Entries intentionally skipped as `(name, reason, kind)` tuples."""


def load_library(objects: Mapping[str, bytes]) -> MetricLibrary:
    """Loads custom metrics specified in an agents-cli evaluation config.

    Separates metrics into locally compiled callables, remote source
    definitions, and prompt templates for the eval service to judge, skipping
    entries that name a predefined metric.

    Args:
        objects: Mapping of relative file paths to file contents in bytes.

    Returns:
        A populated `MetricLibrary` containing local metrics, remote and judged
        definitions, and failure/skip records.

    Raises:
        ValueError: If the evaluation config file does not parse to a dictionary.
    """
    name = next((n for n in CONFIG_NAMES if n in objects), None)
    if name is None:
        logger.warning(
            "No %s in the library; nothing to score with.",
            " or ".join(CONFIG_NAMES),
        )
        return MetricLibrary()

    # yaml.safe_load parses both YAML and JSON configs since JSON is a YAML subset.
    config = yaml.safe_load(objects[name].decode()) or {}
    if not isinstance(config, dict):
        raise ValueError(
            f"{name} must parse to a mapping, got {type(config).__name__}."
        )
    # Require string names to prevent non-string YAML values from raising
    # unhandled exceptions outside per-metric isolation guards.
    defined = {
        entry["name"]: entry
        for entry in (config.get("custom_metrics") or [])
        if isinstance(entry, dict)
        and isinstance(entry.get("name"), str)
        and entry["name"]
    }

    library = MetricLibrary()
    failures: list[tuple[str, str]] = []
    declined: list[tuple[str, str, str]] = []
    # Both kinds the eval service scores, in one list: they travel in one
    # call, so a service name has to be unique across them, not within each.
    sent: list[RemoteMetric] = []
    seen: set[str] = set()
    taken: dict[str, str] = {}
    for metric in config.get("metrics_to_run") or []:
        # Validate metric names as strings to prevent invalid YAML entries from
        # crashing downstream lookups outside the per-metric error guard.
        if not isinstance(metric, str) or not metric:
            logger.warning(
                "Skipping metric %r: a metric name must be a string.", metric
            )
            continue
        if metric in seen:
            continue
        seen.add(metric)
        entry = defined.get(metric)
        # Check validation rejections before skip conditions to match agents-cli
        # precedence and ensure configuration errors are captured in run summaries.
        rejected = (
            _compute_rejection_reason(metric, entry)
            if entry is not None
            else ""
        )
        if rejected:
            logger.warning("Metric %r rejected: %s.", metric, rejected)
            failures.append((metric, rejected))
            continue
        reason, kind = _compute_skip_reason(metric, entry)
        if reason:
            logger.info("Skipping metric %r: %s.", metric, reason)
            declined.append((metric, reason, kind))
            continue
        if (
            entry is None
        ):  # Type guard: missing entries are handled by _compute_skip_reason above.
            continue
        try:
            loaded: RemoteMetric
            # A function beats a prompt: `agents-cli` reaches its function
            # branch before it ever builds a judge, so an entry carrying both
            # runs the function there and here.
            if not _defines_function(entry):
                loaded = _load_judge_metric(metric, entry)
            elif _is_remote(entry):
                loaded = _load_remote_metric(metric, entry, objects)
            else:
                library[metric] = _load_metric(metric, entry, objects)
                continue
            # Guard against case-insensitive name collisions across both kinds:
            # the eval service normalizes names, so two clashing entries would
            # overwrite results and cross-contaminate thresholds and contracts.
            if clash := taken.get(loaded.service_name):
                # Drop both colliding metrics because the eval service result
                # cannot be disambiguated between them.
                sent[:] = [m for m in sent if m.name != clash]
                failures.append(
                    (
                        clash,
                        f"it and {metric!r} are the same metric to the eval "
                        f"service, which names both {loaded.service_name!r}",
                    )
                )
                raise ValueError(
                    f"metric {metric!r} and {clash!r} are the same metric to "
                    f"the eval service, which names both {loaded.service_name!r}."
                )
            taken[loaded.service_name] = metric
            sent.append(loaded)
        # Catch SystemExit in case a standalone script metric invokes argparse
        # at module scope.
        except (Exception, SystemExit) as exc:
            # Isolate load failures per metric so a single broken file does not
            # abort the entire evaluation run.
            logger.exception("Metric %r did not load.", metric)
            failures.append((metric, f"{type(exc).__name__}: {exc}"))
    library.failures = tuple(failures)
    library.declined = tuple(declined)
    library.remote = tuple(m for m in sent if isinstance(m, RemoteCodeMetric))
    library.judged = tuple(m for m in sent if isinstance(m, RemoteJudgeMetric))
    return library


def _resolve_predefined(name: str) -> str | None:
    """Resolves a metric name to a supported predefined built-in.

    Resolution order:
    1. Check for an exact case-insensitive match in registered predefined metrics.
    2. Match against versioned variants (`<name>_v<N>`) and select the highest version.

    Args:
        name: Name of the metric to resolve.

    Returns:
        The canonical predefined metric name if resolved; otherwise None.
    """
    from agentplatform._genai._evals_constant import (
        SUPPORTED_PREDEFINED_METRICS,
    )

    lowered = name.lower()
    if lowered in SUPPORTED_PREDEFINED_METRICS:
        return lowered
    matches = [
        match
        for match in (
            re.fullmatch(rf"{re.escape(lowered)}_v(\d+)", candidate)
            for candidate in SUPPORTED_PREDEFINED_METRICS
        )
        if match
    ]
    if not matches:
        return None
    return max(matches, key=lambda m: int(m.group(1))).group(0)


def _compute_rejection_reason(name: str, entry: dict[str, Any]) -> str:
    """Determines why agents-cli would reject a metric entry.

    Validation checks mirror agents-cli validation order so diagnostics match.

    Args:
        name: Name of the metric being validated.
        entry: Configuration dictionary for the custom metric.

    Returns:
        An error message describing the rejection reason, or an empty string if valid.
    """
    if _RUBRIC_GROUP_KEY in entry:
        return (
            "it sets rubric_group_name, which agents-cli rejects: grade a case's "
            "rubric_groups with a managed rubric metric instead"
        )
    resolved = _resolve_predefined(name)
    # Prevent custom metric definitions from using reserved built-in names,
    # which the eval service executes instead of the custom definition.
    defines_body = _defines_function(entry) or _is_judged(entry)
    if defines_body:
        if name.lower() == resolved:
            return (
                "its name is reserved by the eval service, which would ignore this "
                "definition and run the built-in instead"
            )
        if resolved:
            logger.warning(
                "Metric %r shares a name with the built-in %r. The published "
                "definition overrides it.",
                name,
                resolved,
            )
    elif resolved is None and not _is_sdk_computed(name):
        return (
            "it defines neither a function nor a prompt_template, and its "
            "name is not a built-in it could be parameterizing"
        )
    # Check conflicting function keys by presence to mirror agents-cli validation,
    # which rejects entries defining both regardless of their values.
    if "custom_function" in entry and "custom_function_file" in entry:
        return "it sets both custom_function and custom_function_file"
    execution = entry.get("execution", LOCAL_EXECUTION)
    if execution not in EXECUTIONS:
        # Reject invalid execution values only for local function keys, matching
        # agents-cli behavior where remote_custom_function ignores the execution key.
        if any(k in entry for k in _LOCAL_FUNCTION_KEYS):
            return (
                f"its execution is {execution!r}, which agents-cli rejects: "
                f"expected one of {list(EXECUTIONS)}"
            )
        # Warn on unrecognized execution settings for remote functions to surface
        # likely configuration typos.
        logger.warning(
            "Metric %r sets execution %r, which is ignored for a metric the "
            "eval service runs. Expected one of %s.",
            name,
            execution,
            list(EXECUTIONS),
        )
    return ""


def _defines_function(entry: dict[str, Any]) -> bool:
    """Checks whether the entry defines a custom function.

    Args:
        entry: Metric configuration dictionary.

    Returns:
        True if the entry contains any recognized function key; False otherwise.
    """
    return any(k in entry for k in _FUNCTION_KEYS)


def _is_judged(entry: dict[str, Any]) -> bool:
    """Checks whether a metric entry carries a prompt template to judge by.

    Args:
        entry: Metric configuration dictionary.

    Returns:
        True if the entry contains any recognized judge key; False otherwise.
    """
    return any(k in entry for k in _JUDGE_KEYS)


def _is_remote(entry: dict[str, Any]) -> bool:
    """Checks whether a metric is configured for remote execution.

    Args:
        entry: Metric configuration dictionary.

    Returns:
        True if configured to run remotely on the eval service; False for local execution.
    """
    return (
        any(k in entry for k in _REMOTE_KEYS)
        or entry.get("execution") == REMOTE_EXECUTION
    )


def _compute_skip_reason(
    name: str, entry: dict[str, Any] | None
) -> tuple[str, str]:
    """Computes the reason and category for skipping an unrunnable metric entry.

    Args:
        name: Name of the metric.
        entry: Metric configuration dictionary, or None if not defined in custom_metrics.

    Returns:
        A tuple of `(skip_reason, execution_kind)`, or `('', '')` if the metric is runnable.
    """
    if entry is None:
        return (
            "custom_metrics does not define it, so it names a predefined metric",
            PREDEFINED_KIND,
        )
    if _defines_function(entry) or _is_judged(entry):
        return ("", "")
    if _is_remote(entry):
        return (
            "it asks for remote execution but names no function to run",
            PREDEFINED_KIND,
        )
    return (
        "it only parameterizes a metric computed elsewhere",
        PREDEFINED_KIND,
    )


def _load_judge_metric(name: str, entry: dict[str, Any]) -> RemoteJudgeMetric:
    """Builds a judged metric from a prompt-only entry, as agents-cli builds it.

    Uses `LLMMetric.model_validate(entry)`, the call agents-cli makes, so both
    tools accept and refuse the same settings. The service name is read off the
    SDK metric, as the remote code loader reads it off `CodeExecutionMetric`.

    Args:
        name: Metric name as spelled in the eval config.
        entry: Metric configuration dictionary.

    Returns:
        The judged metric, carrying the validated SDK `LLMMetric`.

    Raises:
        ValueError: If the SDK refuses the entry, the eval service would error
            it on every session, or the threshold is not finite.
    """
    metric = LLMMetric.model_validate(entry)
    if dead := _compute_dead_judge_reason(metric):
        raise ValueError(f"metric {name!r} {dead}.")
    # A prompt has no `EXPECTED` literal, so the contract findings cluster on
    # comes from the config, with the whitespace rule `_extract_expected` applies.
    declared = entry.get("expected")
    expected = (
        declared.strip()
        if isinstance(declared, str) and declared.strip()
        else _build_default_expected(name)
    )
    return RemoteJudgeMetric(
        name=name,
        service_name=metric.name or name,
        expected=expected,
        metric=metric,
        threshold=_extract_threshold(name, entry)
        if "threshold" in entry
        else None,
    )


def _compute_dead_judge_reason(metric: LLMMetric) -> str:
    """Computes why the eval service would error this judge on every session.

    Each shape was measured to fail every case, leaving the metric without a
    verdict and failing the run. Refusing it at load names the problem before a
    sweep stalls; agents-cli would fail these entries too, only later.

    Args:
        metric: Validated SDK judge metric.

    Returns:
        The reason, or an empty string if the service can run the judge.
    """
    # Measured live: 400 "Variable agent_eval_data is required but not
    # provided". A template reads the conversation as `{agent_data}`, not under
    # the `agent_eval_data` key a remote code metric reads the same turns by.
    if "{agent_eval_data}" in (metric.prompt_template or ""):
        return (
            "names {agent_eval_data} in its prompt_template, which the eval "
            "service cannot render: the conversation is {agent_data}"
        )
    if metric.return_raw_output:
        return (
            "sets return_raw_output, which the eval service rejects as an unknown "
            "field -- and a verdict needs the numeric score it would suppress"
        )
    return ""


def _resolve_function_source(
    name: str, entry: dict[str, Any], objects: Mapping[str, bytes]
) -> tuple[bytes, str]:
    """Resolves a metric function's source code and origin identifier.

    Args:
        name: Name of the metric.
        entry: Metric configuration dictionary.
        objects: Mapping of relative file paths to file contents in bytes.

    Returns:
        A tuple of `(source_bytes, origin_description)`.

    Raises:
        ValueError: If both inline and file sources are present, or if the specified
            function file is not in `objects`.
    """
    inline = entry.get("remote_custom_function", entry.get("custom_function"))
    filename = entry.get("custom_function_file")
    # Check key presence rather than truthiness to match agents-cli validation,
    # which forbids defining both keys even if one value is empty or null.
    if "custom_function" in entry and "custom_function_file" in entry:
        raise ValueError(
            f"metric {name!r} sets both custom_function and custom_function_file."
        )
    if isinstance(inline, str):
        return inline.encode(), f"<custom_function:{name}>"
    if isinstance(filename, str) and filename in objects:
        return objects[filename], filename
    raise ValueError(
        f"metric {name!r} names {filename!r}, which is not published."
    )


def _load_remote_metric(
    name: str, entry: dict[str, Any], objects: Mapping[str, bytes]
) -> RemoteCodeMetric:
    """Loads and validates a remote metric definition without executing it locally.

    Validation steps:
    1. Parse source code via AST to catch syntax errors early.
    2. Confirm `evaluate()` exists and is synchronous.
    3. Ensure `evaluate()` returns a numerical score rather than a dictionary mapping.
    4. Construct a temporary SDK `CodeExecutionMetric` to resolve the service name.

    Args:
        name: Name of the metric.
        entry: Metric configuration dictionary.
        objects: Mapping of relative file paths to file contents in bytes.

    Returns:
        A validated `RemoteCodeMetric` definition.

    Raises:
        ValueError: If `evaluate()` is missing, async, returns a score mapping, or has invalid thresholds.
    """
    source, origin = _resolve_function_source(name, entry, objects)
    tree = ast.parse(source, filename=origin)
    evaluate = next(
        (
            node
            for node in tree.body
            if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
            and node.name == "evaluate"
        ),
        None,
    )
    if evaluate is None:
        raise ValueError(f"metric {name!r} defines no evaluate() function.")
    # The eval service executes evaluate synchronously; an unawaited coroutine
    # would fail every evaluation session.
    if isinstance(evaluate, ast.AsyncFunctionDef):
        raise ValueError(
            f"metric {name!r} defines evaluate() as async, which is never awaited."
        )
    if _returns_score_mapping(evaluate):
        raise ValueError(
            f"metric {name!r} returns a score mapping, which the eval service "
            "scores 0.0 on every session -- a remote metric must return a number."
        )
    decoded = source.decode()
    return RemoteCodeMetric(
        name=name,
        service_name=CodeExecutionMetric(
            name=name, custom_function=decoded
        ).name
        or name,
        expected=_extract_expected_literal(name, tree),
        source=decoded,
        threshold=_extract_threshold(name, entry),
    )


def _extract_threshold(name: str, entry: dict[str, Any]) -> float:
    """Extracts and validates the passing score threshold for a metric.

    Non-finite values (such as NaN or Inf) are rejected because comparison against
    them always fails, corrupting test results.

    Args:
        name: Name of the metric.
        entry: Metric configuration dictionary.

    Returns:
        The finite float threshold value.

    Raises:
        ValueError: If the threshold value is not finite.
    """
    value = float(entry.get("threshold", 1.0))
    if not math.isfinite(value):
        raise ValueError(
            f"metric {name!r} sets a non-finite threshold {value!r}."
        )
    return value


def _returns_score_mapping(
    evaluate: ast.FunctionDef | ast.AsyncFunctionDef,
) -> bool:
    """Checks whether `evaluate` visibly returns a `{"score": ...}` mapping.

    The eval service scores mapping returns as 0.0, falsely failing every session.
    Only direct return statements in `evaluate` are inspected as a heuristic guard.

    Args:
        evaluate: The AST function definition of `evaluate`.

    Returns:
        True if any return statement visibly returns a score dictionary; False otherwise.
    """
    for node in _list_return_statements(evaluate):
        # Check both branches of conditional expressions for score mappings.
        candidates = (
            [node.value.body, node.value.orelse]
            if isinstance(node.value, ast.IfExp)
            else [node.value]
        )
        if any(_is_score_mapping(value) for value in candidates):
            return True
    return False


def _list_return_statements(
    fn: ast.FunctionDef | ast.AsyncFunctionDef,
) -> list[ast.Return]:
    """Finds top-level return statements in a function, excluding nested scopes.

    Nested functions, lambdas, and classes are ignored so helpers returning mappings
    do not cause false rejections if the main function returns a scalar.

    Args:
        fn: Function AST definition node.

    Returns:
        List of AST `Return` nodes within the function's own scope.
    """
    found: list[ast.Return] = []
    stack: list[ast.AST] = list(ast.iter_child_nodes(fn))
    while stack:
        node = stack.pop()
        if isinstance(
            node,
            ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef | ast.Lambda,
        ):
            continue
        if isinstance(node, ast.Return) and node.value is not None:
            found.append(node)
        stack.extend(ast.iter_child_nodes(node))
    return found


def _is_score_mapping(value: ast.expr | None) -> bool:
    """Checks whether an AST expression represents a `{"score": ...}` dictionary.

    Args:
        value: AST expression to inspect.

    Returns:
        True if the expression matches a score dictionary or `dict(score=...)` call; False otherwise.
    """
    if isinstance(value, ast.Dict):
        return any(
            isinstance(k, ast.Constant) and k.value == "score"
            for k in value.keys
        )
    if isinstance(value, ast.Call) and isinstance(value.func, ast.Name):
        if value.func.id != "dict":
            return False
        if any(kw.arg == "score" for kw in value.keywords):
            return True
        # Inspect positional arguments for nested mappings in dict(...) calls.
        return any(_is_score_mapping(arg) for arg in value.args)
    return False


def _extract_expected_literal(name: str, tree: ast.Module) -> str:
    """Extracts the `EXPECTED` contract string from module AST without execution.

    Scans top-level assignments in reverse order to match Python's last-assignment-wins
    semantics when resolving module attributes.

    Args:
        name: Name of the metric.
        tree: Parsed AST module of the metric.

    Returns:
        The expected behavior string literal, or a generated default if none is found.
    """
    value: ast.expr | None
    for node in reversed(tree.body):
        if isinstance(node, ast.Assign):
            targets, value = node.targets, node.value
        elif isinstance(node, ast.AnnAssign):
            targets, value = [node.target], node.value
        else:
            continue
        if not any(
            isinstance(t, ast.Name) and t.id == "EXPECTED" for t in targets
        ):
            continue
        # The final assignment determines module behavior; stop immediately if
        # it is not a valid non-empty string.
        if isinstance(value, ast.Constant) and isinstance(value.value, str):
            if text := value.value.strip():
                return text
        break
    return _build_default_expected(name)


def _load_metric(
    name: str, entry: dict[str, Any], objects: Mapping[str, bytes]
) -> CodeMetric:
    """Compiles and loads a local code metric from source bytes.

    Args:
        name: Name of the metric.
        entry: Metric configuration dictionary.
        objects: Mapping of relative file paths to file contents in bytes.

    Returns:
        An instantiated and wrapped `CodeMetric`.

    Raises:
        ValueError: If the loaded module does not define a callable `evaluate()` function.
    """
    source, origin = _resolve_function_source(name, entry, objects)
    module = _exec_module(name, source, origin)
    evaluate = getattr(module, "evaluate", None)
    if not callable(evaluate):
        raise ValueError(f"metric {name!r} defines no evaluate() function.")
    return CodeMetric(
        name=name,
        expected=_extract_expected(name, module),
        evaluate=evaluate,
        model_modules=_list_model_modules(source, origin),
        threshold=float(entry.get("threshold", 1.0)),
    )


def _list_model_modules(source: bytes, origin: str) -> tuple[str, ...]:
    """Identifies model client libraries imported by metric source code.

    Inspects imports rather than call sites to reliably catch model usage despite
    dynamic invocation patterns.

    Args:
        source: Metric Python source code as bytes.
        origin: Filename or origin description for error reporting.

    Returns:
        Sorted tuple of detected model module root or package names.
    """
    try:
        tree = ast.parse(source, filename=origin)
    except SyntaxError:  # pragma: no cover -- already compiled above
        return ()
    found = {
        root
        for node in ast.walk(tree)
        for name in _list_imported_names(node)
        for root in _MODEL_MODULES
        if name == root or name.startswith(f"{root}.")
    }
    return tuple(sorted(found))


def _list_imported_names(node: ast.AST) -> tuple[str, ...]:
    """Extracts imported module paths from an AST import node.

    Args:
        node: AST node to inspect.

    Returns:
        Tuple of imported module names.
    """
    if isinstance(node, ast.Import):
        return tuple(alias.name for alias in node.names)
    if isinstance(node, ast.ImportFrom) and node.module and not node.level:
        # Include both parent module and qualified submodules to match root names.
        return (node.module, *(f"{node.module}.{a.name}" for a in node.names))
    return ()


def _extract_expected(name: str, module: Any) -> str:
    """Extracts expected behavior contract from an imported metric module.

    Findings cluster on this text to generate insight labels.

    Args:
        name: Name of the metric.
        module: Executed Python module object.

    Returns:
        The stripped `EXPECTED` string if defined, or a generated default contract.
    """
    declared = getattr(module, "EXPECTED", None)
    if isinstance(declared, str) and declared.strip():
        return declared.strip()
    return _build_default_expected(name)


def _build_default_expected(name: str) -> str:
    """Builds a fallback expected behavior contract when `EXPECTED` is missing.

    Args:
        name: Name of the metric.

    Returns:
        A default expected contract string based on the metric name.
    """
    logger.info(
        "Metric %r declares no EXPECTED; insights from it will be labelled from "
        "its name alone.",
        name,
    )
    return f"The agent satisfies the {name!r} metric."


def _exec_module(name: str, source: bytes, origin: str) -> Any:
    """Executes metric source code within an isolated module registered in `sys.modules`.

    Registering in `sys.modules` enables type hint and serialization resolution,
    while hashing the source prevents name collisions across evaluation sweeps.

    Args:
        name: Metric identifier name.
        source: Python source code bytes.
        origin: Source filename or origin description.

    Returns:
        The executed `types.ModuleType` instance.

    Raises:
        BaseException: If compilation or module execution fails, unregistering the module.
    """
    digest = hashlib.sha256(source).hexdigest()[:16]
    module_name = f"aqa_metric_{name}_{digest}"
    module = types.ModuleType(module_name)
    module.__file__ = origin
    sys.modules[module_name] = module
    try:
        exec(compile(source, origin, "exec"), module.__dict__)  # noqa: S102 - executes trusted metric code from deployment bucket, not request or model input
    except BaseException:
        sys.modules.pop(module_name, None)
        raise
    return module
