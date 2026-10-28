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

"""The published code-metric library: what `agents-cli aqua metrics` does, and what a run reads.

The CLI talks to AQuA only through its API, so it needs no grant on the metrics
bucket: it sends a library to the command routes, and the engine validates it
with the loader that will run it (`library.load_library`) and writes it into the
metrics store (`objects.store.metrics_store_factory`) with its own credentials --
the metrics bucket in a deployment, `.aqua/metrics/` in a standalone run.

Each attached agent has its own library, under ``<agent key>/current/metrics/``
(`build_library_prefix`). The library at `LIBRARY_PREFIX`, the instance's, is
the default agent's while its own prefix is empty, as the instance's goal and
memories are (`documents.agent_scope`). It is read whole or not at all: mixing
its files with the agent's would make a library nobody published. A publish
mirrors the directory the CLI sent:

1. Validate the library; refuse it if the eval config is missing or does not
   parse, if a metric it runs does not load, or if nothing in it can run.
2. Write every file, the eval config last, so that a failure part-way leaves
   the previous config naming files that still exist.
3. Delete the published files missing from the directory, after the writes,
   so that a failure part-way leaves the previous library intact. For the
   default agent, delete the instance's library too, which would otherwise
   come back if the agent's own were emptied.

A dry run stops after step 1 and returns the plan, so the CLI can ask before
it deletes anything.

Validating executes each local metric's module, as every run does. Whoever can
publish can therefore run code in the engine; publishing is restricted to
those who can call AQuA's API.
"""

from __future__ import annotations

import base64
import binascii
import logging
import posixpath
from typing import TYPE_CHECKING, Any

import yaml
from ambient_quality_agent.tools.documents import agent_scope
from ambient_quality_agent.tools.metrics.library import (
    CONFIG_NAMES,
    MetricLibrary,
    load_library,
)
from ambient_quality_agent.tools.objects import store as objects_store

if TYPE_CHECKING:
    from ambient_quality_agent.tools.objects.store import ObjectStore

logger = logging.getLogger(__name__)

LIBRARY_PREFIX = "current/metrics/"
"""Prefix of the instance's library in the metrics store, and the suffix of
each agent's."""

PUBLISHABLE_SUFFIXES = (".py", ".yaml", ".yml", ".json")
"""The files a library is made of; the CLI sends no others."""

_NO_STORE = (
    "This deployment has no metrics bucket, so there is nowhere to publish "
    "metrics. Apply the Terraform that creates it and set "
    "AQA_METRICS_GCS_BUCKET, or run AQuA standalone."
)


class PublishError(ValueError):
    """A library this module refuses, with the reason for the caller."""


def build_library_prefix(agent_key: str) -> str:
    """Builds the prefix an agent's library is published under.

    Args:
        agent_key: Key of the agent; empty when no agent is resolved.

    Returns:
        ``<agent key>/current/metrics/``, with slashes in the key replaced so it
        stays one path segment; `LIBRARY_PREFIX` for an empty key.
    """
    if not agent_key:
        return LIBRARY_PREFIX
    return f"{agent_key.replace('/', '_')}/{LIBRARY_PREFIX}"


def resolve_fallback_prefix(agent_key: str) -> str:
    """Resolves the library an agent reads while its own is empty.

    Args:
        agent_key: Key of the agent; empty when no agent is resolved.

    Returns:
        `LIBRARY_PREFIX` for the default agent, else an empty string.

    Raises:
        Exception: Whatever reading the attachments raised, so that the
            default agent is not read as another one.
    """
    if agent_key and agent_scope.inherits_instance_documents(agent_key):
        return LIBRARY_PREFIX
    return ""


def load_published_library(store: ObjectStore, agent_key: str) -> MetricLibrary:
    """Loads an agent's published library, as a run scores with it.

    Args:
        store: The metrics store.
        agent_key: Key of the run's agent; empty when no agent is resolved.

    Returns:
        The library; empty when nothing is published for the agent.

    Raises:
        Exception: Whatever reading the store or the attachments raised.
    """
    prefix, objects = _read_agent_library(
        store, agent_key, resolve_fallback_prefix(agent_key)
    )
    logger.info(
        "Metric library: read %d object(s) from %s",
        len(objects),
        store.uri(prefix),
    )
    return load_library(objects)


def publish(files: Any, *, agent_key: str, dry_run: bool) -> dict[str, Any]:
    """Validates a library and mirrors it into the agent's prefix.

    Args:
        files: Entries of `{"path": ..., "content": ...}`, where `content` is
            the file base64-encoded.
        agent_key: Key of the agent the library is for; empty when no agent
            is resolved.
        dry_run: Whether to only validate and plan, writing nothing.

    Returns:
        `{"location", "report", "uploads", "deletions", "inherited_deletions",
        "published"}`, or `{"error": ...}` when the library is refused, there
        is no metrics store, or the attachments cannot be read. `uploads` are
        paths relative to the library; `deletions`, the agent's files not
        present in the directory, and `inherited_deletions`, the instance's
        library for the default agent, are full object names.
    """
    store = objects_store.metrics_store_factory()
    if store is None:
        return {"error": _NO_STORE}
    try:
        objects = _decode_files(files)
        report = _validate_library(objects)
    except PublishError as exc:
        return {"error": f"Refusing to publish: {exc}"}
    try:
        fallback = resolve_fallback_prefix(agent_key)
    except Exception as exc:
        return {"error": _format_unreadable_attachments_error(exc)}
    prefix = build_library_prefix(agent_key)
    wanted = {prefix + path for path in objects}
    deletions = sorted(set(store.list_names(prefix)) - wanted)
    inherited = store.list_names(fallback) if fallback else []
    uploads = sorted(objects, key=lambda path: (path in CONFIG_NAMES, path))
    if not dry_run:
        for path in uploads:
            store.write_bytes(prefix + path, objects[path])
        for name in [*deletions, *inherited]:
            store.delete(name)
    return {
        "location": store.uri(prefix),
        "report": report,
        "uploads": uploads,
        "deletions": deletions,
        "inherited_deletions": inherited,
        "published": not dry_run,
    }


def list_published(*, agent_key: str) -> dict[str, Any]:
    """Lists an agent's library and validates it as a run would load it.

    Args:
        agent_key: Key of the agent; empty when no agent is resolved.

    Returns:
        `{"location", "inherited", "files", "report"}`, with `report` empty
        when nothing is published and `inherited` true when the default agent
        reads the instance's library; `{"location", "inherited", "files",
        "error"}` when the library is broken; or `{"error": ...}` when there
        is no metrics store or the attachments cannot be read.
    """
    store = objects_store.metrics_store_factory()
    if store is None:
        return {"error": _NO_STORE}
    try:
        fallback = resolve_fallback_prefix(agent_key)
    except Exception as exc:
        return {"error": _format_unreadable_attachments_error(exc)}
    prefix, objects = _read_agent_library(store, agent_key, fallback)
    listing: dict[str, Any] = {
        "location": store.uri(prefix),
        "inherited": prefix != build_library_prefix(agent_key),
        "files": [
            {"path": path, "size": len(data)}
            for path, data in sorted(objects.items())
        ],
    }
    if not objects:
        return {**listing, "report": {}}
    try:
        return {**listing, "report": _validate_library(objects)}
    except PublishError as exc:
        return {**listing, "error": f"The published library is broken: {exc}"}


def destroy(*, agent_key: str, dry_run: bool) -> dict[str, Any]:
    """Deletes an agent's published library.

    For the default agent, this also deletes the instance's library, which it
    reads while its own is empty.

    Args:
        agent_key: Key of the agent; empty when no agent is resolved.
        dry_run: Whether to only list what would be deleted.

    Returns:
        `{"location", "deletions", "deleted"}`, or `{"error": ...}` when there
        is no metrics store or the attachments cannot be read.
    """
    store = objects_store.metrics_store_factory()
    if store is None:
        return {"error": _NO_STORE}
    try:
        fallback = resolve_fallback_prefix(agent_key)
    except Exception as exc:
        return {"error": _format_unreadable_attachments_error(exc)}
    prefix = build_library_prefix(agent_key)
    deletions = store.list_names(prefix)
    if fallback:
        deletions += store.list_names(fallback)
    if not dry_run:
        for name in deletions:
            store.delete(name)
    return {
        "location": store.uri(prefix),
        "deletions": deletions,
        "deleted": not dry_run,
    }


def _read_agent_library(
    store: ObjectStore, agent_key: str, fallback: str
) -> tuple[str, dict[str, bytes]]:
    """Reads the library an agent scores with.

    Args:
        store: The metrics store.
        agent_key: Key of the agent; empty when no agent is resolved.
        fallback: The library read while the agent's own is empty
            (`resolve_fallback_prefix`); empty for none.

    Returns:
        The prefix read, and each file's content by its path below it: the
        agent's own library, or `fallback` when the agent's is empty and
        `fallback` is not.
    """
    prefix = build_library_prefix(agent_key)
    objects = _read_library(store, prefix)
    if not objects and fallback:
        inherited = _read_library(store, fallback)
        if inherited:
            return fallback, inherited
    return prefix, objects


def _format_unreadable_attachments_error(exc: Exception) -> str:
    """Formats the error answered when the default agent cannot be resolved.

    Args:
        exc: What reading the attachments raised.

    Returns:
        The error message.
    """
    logger.warning("metrics: could not read the attachments: %s", exc)
    return (
        "Could not read the attached agents to tell whether this is the "
        f"default agent: {type(exc).__name__}: {exc}"
    )


def _read_library(store: ObjectStore, prefix: str) -> dict[str, bytes]:
    """Reads every file under a prefix, keyed by its path in the library.

    Args:
        store: The metrics store.
        prefix: Prefix of the library.

    Returns:
        Each file's content, by its path below `prefix`.
    """
    objects: dict[str, bytes] = {}
    for name in store.list_names(prefix):
        path = name.removeprefix(prefix).lstrip("/")
        data = store.read_bytes(name)
        # A directory placeholder has no path (the root) or ends in a slash;
        # a file deleted since the listing has no content.
        if path and not path.endswith("/") and data is not None:
            objects[path] = data
    return objects


def _decode_files(files: Any) -> dict[str, bytes]:
    """Decodes the files of a library as the CLI sent them.

    Args:
        files: Entries of `{"path": ..., "content": <base64>}`.

    Returns:
        Each file's content, by its normalized path in the library.

    Raises:
        PublishError: If `files` is not a list, or an entry is malformed, has
            a path outside the library or of a kind a library does not hold,
            repeats a path, or does not decode.
    """
    if not isinstance(files, list):
        raise PublishError(f"files is not a JSON list: {files!r}")
    objects: dict[str, bytes] = {}
    for entry in files:
        if not isinstance(entry, dict):
            raise PublishError(f"file entry is not a JSON object: {entry!r}")
        path = _validate_path(entry.get("path"))
        content = entry.get("content")
        if not isinstance(content, str):
            raise PublishError(f"{path}: content is not a string")
        if path in objects:
            raise PublishError(f"{path} is sent twice")
        try:
            objects[path] = base64.b64decode(content, validate=True)
        except binascii.Error as exc:
            raise PublishError(f"{path}: content is not base64: {exc}") from exc
    return objects


def _validate_path(path: Any) -> str:
    """Checks that a file path stays inside the library and is a library file.

    Args:
        path: Library-relative path as sent.

    Returns:
        The normalized path.

    Raises:
        PublishError: If the path is not a relative POSIX path inside the
            library, holds a control character, or does not end in one of
            `PUBLISHABLE_SUFFIXES`.
    """
    if not isinstance(path, str) or not path:
        raise PublishError(f"file path is not a non-empty string: {path!r}")
    # A store cannot name an object with a control character in it, and a
    # refusal here comes before any file is written.
    if any(ord(char) < 0x20 or ord(char) == 0x7F for char in path):
        raise PublishError(f"{path!r}: a path cannot hold control characters")
    normalized = posixpath.normpath(path)
    if (
        "\\" in path
        or posixpath.isabs(path)
        or normalized == ".."
        or normalized.startswith("../")
    ):
        raise PublishError(f"{path}: not a path inside the library")
    if not normalized.endswith(PUBLISHABLE_SUFFIXES):
        raise PublishError(
            f"{path}: a library holds only {', '.join(PUBLISHABLE_SUFFIXES)} "
            "files"
        )
    return normalized


def _validate_library(objects: dict[str, bytes]) -> dict[str, Any]:
    """Loads a library as a run would and reports what it would run.

    Args:
        objects: Each file's content, by its path in the library.

    Returns:
        The report: see `_build_report`.

    Raises:
        PublishError: If there is no eval config or it does not parse, a
            metric it runs did not load, or no metric can run.
    """
    if not any(name in objects for name in CONFIG_NAMES):
        raise PublishError(
            f"no eval config: expected one of {', '.join(CONFIG_NAMES)}."
        )
    try:
        library = load_library(objects)
    # A config of the wrong shape, such as `metrics_to_run: 5`, raises a
    # TypeError from the loader rather than a ValueError.
    except (ValueError, TypeError, yaml.YAMLError) as exc:
        raise PublishError(f"{type(exc).__name__}: {exc}") from exc
    if library.failures:
        raise PublishError(
            "; ".join(
                f"metric {name!r}: {why}" for name, why in library.failures
            )
        )
    report = _build_report(library)
    if not report["metrics"]:
        raise PublishError(
            "no metric in this library can be run by AQuA. Publishing it would "
            "leave the deployment with nothing to score with."
        )
    return report


def _build_report(library: MetricLibrary) -> dict[str, Any]:
    """Turns a loaded library into the report the CLI prints.

    Args:
        library: The loaded library, with no failures.

    Returns:
        `metrics`, each with its `name`, `source_path`, `expected`,
        `model_modules`, and for a judged metric `judged`, `judge_samples`,
        `scores_only` and `reads_prompt`; and `skipped`, each `name` and
        `reason`.
    """
    metrics: list[dict[str, Any]] = [
        {
            "name": metric.name,
            "source_path": metric.source_path,
            "expected": metric.expected,
            "model_modules": list(metric.model_modules),
            "judged": False,
        }
        for metric in [*library.values(), *library.remote]
    ]
    metrics += [
        {
            "name": metric.name,
            "source_path": f"<prompt_template:{metric.name}>",
            "expected": metric.expected,
            "model_modules": [],
            "judged": True,
            "judge_samples": metric.metric.judge_model_sampling_count or 1,
            "scores_only": metric.threshold is None,
            "reads_prompt": "{prompt}" in (metric.metric.prompt_template or ""),
        }
        for metric in library.judged
    ]
    return {
        "metrics": metrics,
        "skipped": [
            {"name": name, "reason": reason}
            for name, reason, _ in library.declined
        ],
    }
