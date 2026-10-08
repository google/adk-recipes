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

"""Publish, list, and delete code-metric libraries for an AQuA deployment.

Pre-validates metric modules locally before upload so errors are caught early.
`validate` reimplements the agent loader because the CLI runs standalone
without the agent package installed. GCS operations delegate to
:mod:`ambient_quality_cli.gcs` with metric-specific remediation messages.
"""

from __future__ import annotations

import ast
import contextlib
import dataclasses
import math
import posixpath
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ambient_quality_cli import gcs

if TYPE_CHECKING:
    from collections.abc import Iterator

LIBRARY_PREFIX = "current/metrics/"
"""GCS prefix where the metric library lives."""

_REMEDIATION = gcs.Remediation(
    forbidden=(
        "Publishing needs roles/storage.objectAdmin on the bucket -- add "
        "yourself to the module's metrics_writers, or ask whoever owns the "
        "deployment."
    ),
    missing="Deploy AQuA before publishing to it.",
)

PUBLISHABLE_SUFFIXES = (".py", ".yaml", ".yml", ".json")

CONFIG_NAMES = ("eval_config.yaml", "eval_config.yml", "eval_config.json")

_REMOTE_KEYS = ("remote_custom_function",)
"""Configuration keys identifying functions executed by the evaluation service."""

REMOTE_EXECUTION = "remote"
EXECUTIONS = ("local", REMOTE_EXECUTION)
"""Supported execution modes in agents-cli configuration."""
_JUDGE_KEYS = ("prompt_template",)

_RUBRIC_GROUP_KEY = "rubric_group_name"
"""Configuration key for rubric groups, which agents-cli rejects on custom metrics."""

_MODEL_MODULES = (
    "google.genai",
    "google.generativeai",
    "vertexai",
    "openai",
    "anthropic",
    "litellm",
)
"""Modules indicating LLM client imports; see `library._MODEL_MODULES`."""


@dataclasses.dataclass(frozen=True)
class Metric:
    """A validated metric and its expected behavior."""

    name: str
    source_path: str
    expected: str
    model_modules: tuple[str, ...]
    judged: bool = False
    """Whether the eval service's model, not a function, scores this metric."""
    judge_samples: int = 1
    scores_only: bool = False
    reads_prompt: bool = False
    """A judged metric whose template reads `{prompt}`, set only on single-turn sessions."""
    """A judged metric with no `threshold`: it reports scores and files no findings."""
    """Autorater calls per session for a judged metric (`judge_model_sampling_count`)."""


@dataclasses.dataclass(frozen=True)
class Report:
    """Validation report listing runnable and skipped metrics."""

    metrics: tuple[Metric, ...]
    skipped: tuple[tuple[str, str], ...]
    """Tuples of (name, reason) for skipped metrics."""

    def list_costly_metrics(self) -> tuple[Metric, ...]:
        return tuple(m for m in self.metrics if m.model_modules)

    def list_judged_metrics(self) -> tuple[Metric, ...]:
        return tuple(m for m in self.metrics if m.judged)


class ValidationError(Exception):
    """Raised when a metric library fails local validation."""


def validate(objects: dict[str, bytes]) -> Report:
    """Validates metric files and generates a validation report.

    Args:
        objects: Mapping of relative file paths to raw file bytes.

    Returns:
        Report listing runnable metrics and skipped entries with reasons.

    Raises:
        ValidationError: If configuration is missing, malformed, defines invalid
            metrics, or contains no runnable metrics.
    """
    import yaml

    name = next((n for n in CONFIG_NAMES if n in objects), "")
    if not name:
        raise ValidationError(
            f"no eval config: expected one of {', '.join(CONFIG_NAMES)}."
        )
    try:
        config = yaml.safe_load(objects[name].decode()) or {}
    except yaml.YAMLError as exc:
        raise ValidationError(f"{name} is not valid YAML: {exc}") from exc
    if not isinstance(config, dict):
        raise ValidationError(
            f"{name} must be a mapping, not a list or a scalar."
        )

    defined = {
        entry.get("name"): entry
        for entry in config.get("custom_metrics") or []
        if isinstance(entry, dict)
    }
    metrics: list[Metric] = []
    skipped: list[tuple[str, str]] = []
    for selected in config.get("metrics_to_run") or []:
        if any(m.name == selected for m in metrics):
            continue
        entry = defined.get(selected)
        if entry is not None and (
            rejected := _compute_rejection_reason(selected, entry)
        ):
            raise ValidationError(f"metric {selected!r}: {rejected}.")
        reason = _compute_skip_reason(entry)
        if reason:
            skipped.append((selected, reason))
            continue
        metrics.append(_validate_metric(selected, defined[selected], objects))
    if not metrics:
        raise ValidationError(
            "no metric in this library can be run by AQuA. Publishing it would "
            "leave the deployment with nothing to score with."
        )
    return Report(tuple(metrics), tuple(skipped))


_LOCAL_FUNCTION_KEYS = ("custom_function_file", "custom_function")
"""Configuration keys identifying functions executed locally by the caller."""

_FUNCTION_KEYS = (*_REMOTE_KEYS, *_LOCAL_FUNCTION_KEYS)
"""Complete set of configuration keys indicating executable metric functions."""


def _compute_skip_reason(entry: dict[str, Any] | None) -> str:
    """Determines why a metric entry cannot be executed.

    Mirrors the agent loader: a function or a judge prompt is runnable, because
    the eval service runs the one and judges by the other. An entry carrying
    both runs the function, since agents-cli checks `custom_function` before
    `prompt_template`.

    Args:
        entry: Metric configuration mapping, or None if undefined.

    Returns:
        Human-readable reason string if the metric cannot run, or an empty string if runnable.
    """
    if entry is None:
        return (
            "custom_metrics does not define it, so it names a predefined metric"
        )
    # Validate runnable entries so operators can publish libraries made only of
    # metrics the eval service runs or judges.
    if any(k in entry for k in (*_FUNCTION_KEYS, *_JUDGE_KEYS)):
        return ""
    if _is_remote(entry):
        return "it asks for remote execution but names no function to run"
    return "it defines no local function"


def _is_remote(entry: dict[str, Any]) -> bool:
    """Checks whether the evaluation service executes this metric remotely.

    Args:
        entry: Metric configuration mapping.

    Returns:
        True if the metric specifies remote execution, False otherwise.
    """
    return (
        any(k in entry for k in _REMOTE_KEYS)
        or entry.get("execution") == REMOTE_EXECUTION
    )


def _returns_score_mapping(tree: ast.Module) -> bool:
    """Checks whether evaluate() explicitly returns a {"score": ...} mapping.

    Mirrors tools.metrics.library._returns_score_mapping to ensure consistency
    with the agent loader, preventing publication of metrics that fail scoring.

    Args:
        tree: Parsed AST module of the metric file.

    Returns:
        True if evaluate() visibly returns a score dictionary, False otherwise.
    """
    for node in tree.body:
        if not isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            continue
        if node.name != "evaluate":
            continue
        for inner in _list_return_statements(node):
            # A ternary expression may return the mapping in either branch.
            candidates = (
                [inner.value.body, inner.value.orelse]
                if isinstance(inner.value, ast.IfExp)
                else [inner.value]
            )
            if any(_is_score_mapping(v) for v in candidates):
                return True
    return False


def _is_score_mapping(value: ast.expr | None) -> bool:
    """Determines if an AST expression represents a {"score": ...} mapping.

    Args:
        value: AST expression to inspect, or None.

    Returns:
        True if the expression represents a dictionary containing a "score" key.
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
        # Handle dict({"score": ...}) constructor calls with positional mapping.
        return any(_is_score_mapping(arg) for arg in value.args)
    return False


def _list_return_statements(
    fn: ast.FunctionDef | ast.AsyncFunctionDef,
) -> list[ast.Return]:
    """Finds top-level return statements directly within a function body.

    Excludes return statements nested inside inner functions, classes, or lambdas.

    Args:
        fn: AST function node to inspect.

    Returns:
        List of return statement AST nodes belonging directly to the function.
    """
    found: list[ast.Return] = []
    stack: list[ast.AST] = list(ast.iter_child_nodes(fn))
    while stack:
        node = stack.pop()
        # Skip nested scopes so returns inside helper functions or classes are ignored.
        if isinstance(
            node,
            ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef | ast.Lambda,
        ):
            continue
        if isinstance(node, ast.Return) and node.value is not None:
            found.append(node)
        stack.extend(ast.iter_child_nodes(node))
    return found


def _resolve_predefined(name: str) -> str | None:
    """Resolves a metric name to a registered built-in metric identifier.

    Args:
        name: Metric name to resolve.

    Returns:
        Canonical built-in metric name, latest version match, or None if unregistered.
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

    Mirrors tools.metrics.library._compute_rejection_reason so validation errors
    surface at publication time before sweeps execute.

    Args:
        name: Name of the metric.
        entry: Metric configuration mapping.

    Returns:
        Rejection reason string if invalid, or an empty string if accepted.
    """
    execution = entry.get("execution", "local")
    # Validate execution mode only on custom_function where agents-cli inspects the key.
    if (
        any(k in entry for k in _LOCAL_FUNCTION_KEYS)
        and execution not in EXECUTIONS
    ):
        return (
            f"its execution is {execution!r}, which agents-cli rejects: expected "
            f"one of {list(EXECUTIONS)}"
        )
    if not any(k in entry for k in _FUNCTION_KEYS) and not any(
        k in entry for k in _JUDGE_KEYS
    ):
        if _resolve_predefined(name) is None:
            return (
                "it defines neither a function nor a prompt_template, and its "
                "name is not a built-in it could be parameterizing"
            )
    if _RUBRIC_GROUP_KEY in entry:
        return (
            "it sets rubric_group_name, which agents-cli rejects: grade a case's "
            "rubric_groups with a managed rubric metric instead"
        )
    if name.lower() == _resolve_predefined(name):
        return (
            "its name is reserved by the eval service, which would ignore this "
            "definition and run the built-in instead"
        )
    return ""


def _validate_metric(
    name: str, entry: dict[str, Any], objects: dict[str, bytes]
) -> Metric:
    """Parses and validates a single metric entry from source bytes.

    Delegates execution mode checks to _compute_rejection_reason as the single
    source of truth.

    Args:
        name: Metric identifier.
        entry: Metric configuration mapping.
        objects: Mapping of relative file paths to raw file bytes.

    Returns:
        Validated Metric instance.

    Raises:
        ValidationError: If the source is missing, syntax is invalid, evaluate()
            is missing, or remote execution constraints are violated.
    """
    # A function beats a prompt, as in the loader and in `agents-cli`.
    if not any(k in entry for k in _FUNCTION_KEYS):
        return _validate_judge_metric(name, entry)
    inline = entry.get("remote_custom_function", entry.get("custom_function"))
    filename = entry.get("custom_function_file")
    # Check key presence to match agents-cli validation semantics.
    if "custom_function" in entry and "custom_function_file" in entry:
        raise ValidationError(
            f"metric {name!r} sets both custom_function and custom_function_file."
        )
    if isinstance(inline, str):
        source, origin = inline.encode(), f"<custom_function:{name}>"
    elif isinstance(filename, str) and filename in objects:
        source, origin = objects[filename], filename
    else:
        raise ValidationError(
            f"metric {name!r} names {filename!r}, which is not in this directory."
        )

    try:
        tree = ast.parse(source, filename=origin)
    except SyntaxError as exc:
        raise ValidationError(f"{origin} does not compile: {exc}") from exc

    # Parse AST without executing code to prevent arbitrary execution during validation.
    if not any(
        isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef)
        and node.name == "evaluate"
        for node in tree.body
    ):
        raise ValidationError(
            f"{origin} defines no module-level evaluate() function."
        )

    # Apply remote execution constraints to match evaluation service requirements.
    if _is_remote(entry):
        if any(
            isinstance(n, ast.AsyncFunctionDef) and n.name == "evaluate"
            for n in tree.body
        ):
            raise ValidationError(
                f"metric {name!r} defines evaluate() as async, which is never awaited."
            )
        _validate_threshold(name, entry)
        # The evaluation service scores dictionary returns as 0.0; remote metrics must return numbers.
        if _returns_score_mapping(tree):
            raise ValidationError(
                f"metric {name!r} returns a score mapping, which the eval service "
                "scores 0.0 on every session -- a remote metric must return a number."
            )

    return Metric(
        name=name,
        source_path=origin,
        expected=_extract_expected_literal(name, tree),
        model_modules=_list_model_modules(tree),
    )


def _validate_judge_metric(name: str, entry: dict[str, Any]) -> Metric:
    """Validates a prompt-only entry the way agents-cli builds it.

    Uses `LLMMetric.model_validate(entry)`, the call agents-cli makes, so a
    blank template or an out-of-range sampling count is refused here in the
    SDK's own words, as it would be refused there.

    Args:
        name: Metric identifier.
        entry: Metric configuration mapping.

    Returns:
        Validated Metric instance marked as judged.

    Raises:
        ValidationError: If the SDK refuses the entry, the eval service would
            error it on every session, or the threshold is not finite.
    """
    from agentplatform._genai.types import LLMMetric

    try:
        metric = LLMMetric.model_validate(entry)
    except Exception as exc:
        raise ValidationError(f"metric {name!r}: {exc}") from exc
    if dead := _compute_dead_judge_reason(metric):
        raise ValidationError(f"metric {name!r} {dead}.")
    _validate_threshold(name, entry)
    # A prompt has no `EXPECTED` literal, so the contract comes from the config,
    # stripped as `_extract_expected_literal` strips the literal.
    declared = entry.get("expected")
    expected = (
        declared.strip()
        if isinstance(declared, str) and declared.strip()
        else f"The agent satisfies the {name!r} metric."
    )
    return Metric(
        name=name,
        source_path=f"<prompt_template:{name}>",
        expected=expected,
        model_modules=(),
        judged=True,
        judge_samples=metric.judge_model_sampling_count or 1,
        scores_only="threshold" not in entry,
        reads_prompt="{prompt}" in (metric.prompt_template or ""),
    )


def _compute_dead_judge_reason(metric: Any) -> str:
    """Computes why the eval service would error this judge on every session.

    Mirrors `tools.metrics.library._compute_dead_judge_reason`: each shape was
    measured to fail every case, so the sweep would fail and the window stall.

    Args:
        metric: Validated SDK judge metric.

    Returns:
        The reason, or an empty string if the service can run the judge.
    """
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


def _validate_threshold(name: str, entry: dict[str, Any]) -> None:
    """Refuses a threshold the sweep could not compare a score against.

    Args:
        name: Metric identifier.
        entry: Metric configuration mapping.

    Raises:
        ValidationError: If the threshold is not a finite number.
    """
    threshold = entry.get("threshold", 1.0)
    try:
        finite = math.isfinite(float(threshold))
    except (TypeError, ValueError):
        finite = False
    if not finite:
        raise ValidationError(
            f"metric {name!r} sets a non-finite threshold {threshold!r}."
        )


def _extract_expected_literal(name: str, tree: ast.Module) -> str:
    """Extracts the EXPECTED module-level constant string from metric source.

    Args:
        name: Metric name used in the default fallback string.
        tree: Parsed AST module of the metric.

    Returns:
        Configured EXPECTED string literal, or default fallback description.
    """
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        names = [t.id for t in node.targets if isinstance(t, ast.Name)]
        if names.count("EXPECTED") and isinstance(node.value, ast.Constant):
            declared = node.value.value
            if isinstance(declared, str) and declared.strip():
                return declared.strip()
    return f"The agent satisfies the {name!r} metric."


def _list_model_modules(tree: ast.Module) -> tuple[str, ...]:
    """Identifies model client libraries imported by a metric module.

    Args:
        tree: Parsed AST module to scan.

    Returns:
        Sorted tuple of imported model library root package names.
    """
    found = {
        root
        for node in ast.walk(tree)
        for imported in _list_imported_names(node)
        for root in _MODEL_MODULES
        if imported == root or imported.startswith(f"{root}.")
    }
    return tuple(sorted(found))


def _list_imported_names(node: ast.AST) -> tuple[str, ...]:
    if isinstance(node, ast.Import):
        return tuple(alias.name for alias in node.names)
    if isinstance(node, ast.ImportFrom) and node.module and not node.level:
        return (node.module, *(f"{node.module}.{a.name}" for a in node.names))
    return ()


# --------------------------------------------------------------------------
# Reading a directory, and the bucket
# --------------------------------------------------------------------------


def read_directory(root: Path) -> dict[str, bytes]:
    """Reads publishable files under a directory into memory.

    Args:
        root: Directory path to scan for publishable files.

    Returns:
        Dictionary mapping relative POSIX file paths to file contents.

    Raises:
        ValidationError: If root is not a directory.
    """
    if not root.is_dir():
        raise ValidationError(f"{root} is not a directory.")
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in sorted(_list_publishable_files(root))
    }


def _list_publishable_files(root: Path) -> Iterator[Path]:
    for path in root.rglob("*"):
        if not path.is_file() or path.suffix not in PUBLISHABLE_SUFFIXES:
            continue
        if "__pycache__" in path.parts or path.name.startswith("."):
            continue
        yield path


def list_objects(bucket: str, token: str) -> list[dict[str, Any]]:
    """Lists all metric library objects under `LIBRARY_PREFIX` in `bucket`.

    Args:
        bucket: GCS bucket name.
        token: OAuth2 access token.

    Returns:
        List of object metadata dictionaries from GCS.

    Raises:
        ValidationError: If bucket listing fails.
    """
    with _to_validation_error():
        return gcs.list_objects(bucket, token, LIBRARY_PREFIX, _REMEDIATION)


def download(bucket: str, token: str, names: list[str]) -> dict[str, bytes]:
    """Downloads named objects from the metric library.

    Args:
        bucket: GCS bucket name.
        token: OAuth2 access token.
        names: Full object paths to download.

    Returns:
        Mapping of relative file paths to their byte contents.

    Raises:
        ValidationError: If any object download fails.
    """
    with _to_validation_error():
        return {
            name[len(LIBRARY_PREFIX) :].lstrip("/"): gcs.read(
                bucket, token, name, _REMEDIATION
            )
            for name in names
        }


def upload(bucket: str, token: str, relative: str, body: bytes) -> None:
    with _to_validation_error():
        gcs.upload(
            bucket,
            token,
            posixpath.join(LIBRARY_PREFIX, relative),
            body,
            _REMEDIATION,
        )


def delete(bucket: str, token: str, name: str) -> None:
    with _to_validation_error():
        gcs.delete(bucket, token, name, _REMEDIATION)


def _raise_for_gcs(response: Any, bucket: str) -> None:
    """Translates GCS error responses into metric-specific `ValidationError`.

    Args:
        response: HTTP response object.
        bucket: GCS bucket name.

    Raises:
        ValidationError: Containing actionable remediation for 403/404 errors.
    """
    with _to_validation_error():
        gcs.raise_for_status(response, bucket, _REMEDIATION)


@contextlib.contextmanager
def _to_validation_error() -> Iterator[None]:
    """Converts internal `GcsError` exceptions into CLI `ValidationError`.

    Yields:
        None.

    Raises:
        ValidationError: Wrapped error message from `GcsError`.
    """
    try:
        yield
    except gcs.GcsError as exc:
        raise ValidationError(str(exc)) from exc


def compute_mirror_plan(
    local: dict[str, bytes], remote: list[dict[str, Any]]
) -> tuple[list[str], list[str]]:
    """Computes uploads and deletions required to synchronize local metrics with GCS.

    Ensures deleted local files are removed remotely so retired metrics stop scoring.

    Args:
        local: Mapping of relative local file paths to bytes.
        remote: List of remote object metadata dictionaries from GCS.

    Returns:
        Tuple of (upload_paths, deletion_paths), with configuration files ordered last.
    """
    wanted = {posixpath.join(LIBRARY_PREFIX, name) for name in local}
    present = {item["name"] for item in remote}
    # Order configuration files last so partial failures leave existing configurations referencing valid files.
    uploads = sorted(local, key=lambda name: (name in CONFIG_NAMES, name))
    return uploads, sorted(present - wanted)
