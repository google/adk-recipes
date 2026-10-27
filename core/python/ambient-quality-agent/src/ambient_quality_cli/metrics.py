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

"""The CLI's half of publishing a code-metric library: reading it and showing the report.

The engine validates a library with the loader that runs it and stores it
(`ambient_quality_agent.tools.metrics.publishing`), behind the `metrics/*`
command routes. The CLI reads the directory, sends it in one request, and
prints the report the engine returns.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ambient_quality_shared.protocol import MAX_BODY_BYTES, MetricFile

if TYPE_CHECKING:
    from collections.abc import Iterator

PUBLISHABLE_SUFFIXES = (".py", ".yaml", ".yml", ".json")
"""The files a library is made of; the engine refuses any other
(`publishing.PUBLISHABLE_SUFFIXES`)."""


class ValidationError(Exception):
    """Raised when a directory cannot be sent as a metric library."""


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
        # Relative to `root`, so a library kept under a dot directory (such
        # as `~/.config`) still publishes.
        parts = path.relative_to(root).parts
        if "__pycache__" in parts or any(p.startswith(".") for p in parts):
            continue
        yield path


def encode_files(local: dict[str, bytes]) -> list[MetricFile]:
    """Encodes a library's files for the publish route.

    Args:
        local: Each file's content, by its path in the library.

    Returns:
        The files, base64-encoded.

    Raises:
        ValidationError: If the directory has no publishable file, or the
            request would exceed the routes' body cap.
    """
    if not local:
        raise ValidationError(
            f"no {', '.join(PUBLISHABLE_SUFFIXES)} file in the directory."
        )
    files = [
        MetricFile(path=path, content=base64.b64encode(data).decode("ascii"))
        for path, data in sorted(local.items())
    ]
    # Measured with `dry_run` and default JSON spacing, so the body the route
    # reads is at most this size.
    size = len(json.dumps({"files": files, "dry_run": False}))
    if size > MAX_BODY_BYTES:
        raise ValidationError(
            f"the library is {-(-size // 1024)} KiB encoded; AQuA accepts at most "
            f"{MAX_BODY_BYTES // 1024} KiB. Keep test data and fixtures out of "
            "the metrics directory."
        )
    return files


def render_report_lines(report: dict[str, Any]) -> list[str]:
    """Formats the engine's report on a library for the terminal.

    Args:
        report: The `report` a publish or list route returns: `metrics` and
            `skipped`.

    Returns:
        Lines to print, in order.
    """
    metrics = report.get("metrics") or []
    skipped = report.get("skipped") or []
    lines = [f"{len(metrics)} metric(s) AQuA will run:"]
    lines += [
        f"  {m['name']}  ({m.get('source_path', '')})  {m.get('expected', '')}"
        for m in metrics
    ]
    if skipped:
        lines.append(f"{len(skipped)} skipped, which AQuA cannot run:")
        lines += [f"  {s['name']}: {s.get('reason', '')}" for s in skipped]
    costly = [m for m in metrics if m.get("model_modules")]
    if costly:
        lines.append(
            f"WARNING: {len(costly)} metric(s) import a model client. Each costs "
            "one model call per session per sweep, on the deployment's "
            "credentials, up to the agent's evaluation cap of sessions:"
        )
        lines += [
            f"  {m['name']}: {', '.join(m['model_modules'])}" for m in costly
        ]
    judged = [m for m in metrics if m.get("judged")]
    if judged:
        samples = {m["name"]: int(m.get("judge_samples") or 1) for m in judged}
        names = ", ".join(
            name if count == 1 else f"{name} ({count} samples)"
            for name, count in samples.items()
        )
        lines.append(
            f"{len(judged)} metric(s) are judged by the eval service's model, "
            f"{sum(samples.values())} model call(s) per session per sweep: "
            f"{names}"
        )
        lines += [
            f"  {m['name']}: reads {{prompt}}, which is set only on single-turn "
            "sessions, as in agents-cli; it errors on multi-turn ones"
            for m in judged
            if m.get("reads_prompt")
        ]
        lines += [
            f"  {m['name']}: scores only -- no threshold, so it files no findings"
            for m in judged
            if m.get("scores_only")
        ]
    return lines
