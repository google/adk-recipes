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

"""Child-process worker that evaluates a single model-written jq expression.

The `jq` bindings hold the GIL, cannot be interrupted in-process, and can loop
indefinitely or exhaust memory on arbitrary expressions. Running each evaluation
in a dedicated subprocess isolates those hazards: `jq.run_jq` terminates the
worker on timeout, and `set_resource_limits` caps its address space and CPU
time.

Protocol: stdin receives a JSON request line followed by the trajectory JSON;
stdout emits a single JSON reply (see `run_request`). This module avoids
importing `ambient_quality_agent` to minimize subprocess startup overhead.

The worker clears its environment before it runs jq, so jq's `$ENV` and `env`
see no variables, including those the parent passes to start the interpreter.
"""

from __future__ import annotations

import json
import os
import re
import resource
import sys
import time
from collections.abc import Iterator
from typing import Any

MEMORY_LIMIT_BYTES = 1 << 30
"""Address-space limit per worker process.

Evaluating ``.`` on a 49 MB trajectory JSON peaks at roughly 280 MB of address
space, so 1 GiB accommodates trajectories several times larger while bounding
memory growth from runaway expressions."""

CPU_LIMIT_SECONDS = 12
"""CPU-time limit per worker process.

Set above the parent's 10 s wall-clock timeout so the parent normally terminates
the worker first, while ensuring the kernel stops the process if the parent
cannot."""

# Wrap the model's expression in parentheses. Leading and trailing newlines
# prevent a `# comment` from commenting out the closing parenthesis, and the
# extra blank line handles a trailing backslash line continuation.
_WRAP_EXPRESSION = "(\n{expr}\n\n)"

# Matches jq's syntax error when `_WRAP_EXPRESSION` places a top-level directive
# inside parentheses. Checked against the first error line only so quoted source
# text cannot false-match; the suffix varies across bundled jq versions.
_DIRECTIVE_ERROR = re.compile(
    r"syntax error, unexpected (?:import|include|module)\b"
)

# Formats each result of the wrapped expression as `[is_string, text]` so
# character offsets match `(... | tojson)[a:b]`.
_RENDER_RESULTS = (
    _WRAP_EXPRESSION + ' | [type == "string", '
    '(if type == "string" then . else tojson end)]'
)


def run_request(
    request: dict[str, Any], trajectory_text: str
) -> dict[str, Any]:
    """Evaluate a jq request against `trajectory_text`.

    1. Compile `expr` inside `_WRAP_EXPRESSION`, where top-level ``import``,
       ``include``, and ``module`` directives fail with a syntax error so jq
       cannot load modules or data files from local disk.
    2. Only after ruling out directives, compile `expr` unwrapped. This
       rejects an expression that compiles only because it closes and reopens
       the wrapper's parentheses, such as ``1), (2``, and makes syntax error
       locations refer to the model's expression rather than the wrapper.
    3. Run the compiled program and collect its output with
       `_collect_query_output`.

    Args:
        request: Request dict containing ``expr`` and the query limits
            ``max_chars``, ``count_chars``, and ``count_seconds`` (see
            `_collect_query_output`).
        trajectory_text: Serialized trajectory JSON.

    Returns:
        The dict returned by `_collect_query_output` (which includes
        ``"error"`` and partial ``"text"`` on a runtime error), or
        ``{"error": ...}`` on a compile error.
    """
    import jq

    expr = str(request.get("expr") or "")
    program: Any = None
    wrapped_error: ValueError | None = None
    try:
        program = jq.compile(_RENDER_RESULTS.format(expr=expr))
    except ValueError as exc:
        if _DIRECTIVE_ERROR.search(str(exc).split("\n", 1)[0]):
            return {"error": f"jq compile error: {exc}"}
        wrapped_error = exc
    # Compile unwrapped even when the wrapped compile succeeds: an expression
    # such as `1), (2` is valid only because it closes and reopens the
    # wrapper's parentheses. On failure, error locations then also refer to
    # the model's expression rather than the wrapper.
    try:
        jq.compile(expr)
    except ValueError as exc:
        return {"error": f"jq compile error: {exc}"}
    if wrapped_error is not None:
        return {"error": f"jq compile error: {wrapped_error}"}
    return _collect_query_output(
        iter(program.input_text(trajectory_text)),
        max_chars=int(request.get("max_chars") or 0),
        count_chars=int(request.get("count_chars") or 0),
        count_seconds=float(request.get("count_seconds") or 0),
    )


def _collect_query_output(
    results: Iterator[list[Any]],
    *,
    max_chars: int,
    count_chars: int,
    count_seconds: float,
) -> dict[str, Any]:
    """Join rendered results into capped text and measure any omitted output.

    1. Accumulate whole results (one per line) while within `max_chars` so
       pagination can resume at a result boundary; if the first result alone
       exceeds `max_chars`, return its first `max_chars` characters.
    2. Once the cap is reached (and at least two results are read), drain
       remaining results without storing text until `count_chars` or
       `count_seconds` is exceeded. Keeping `count_seconds` below the parent's
       timeout ensures a slow generator still returns its collected prefix.

    Args:
        results: Lazy jq iterator yielding ``[is_string, text]`` pairs from
            `_RENDER_RESULTS`.
        max_chars: Maximum characters to return in ``text``.
        count_chars: Total character count at which post-truncation counting
            stops.
        count_seconds: Wall-clock seconds after which post-truncation counting
            stops.

    Returns:
        Dictionary with ``text``, ``truncated``, ``single_string``,
        ``total_chars`` (newline-joined character count), ``total_capped``
        (True if counting stopped early, making totals lower bounds),
        ``results_returned`` (whole results in ``text``), ``results_total``,
        ``first_cut`` (True if ``text`` is a prefix of result 0),
        ``first_is_string``, and ``first_chars``. If evaluation raises
        mid-stream, returns ``{"error": ..., "text": ...}``.
    """
    deadline = time.monotonic() + count_seconds
    pieces: list[str] = []
    kept = 0
    full = False
    total = 0
    count = 0
    capped = False
    first_is_string = False
    first_text = ""
    try:
        for is_string, rendered in results:
            text = str(rendered)
            if not count:
                first_is_string, first_text = bool(is_string), text
            size = len(text) + (1 if count else 0)
            # Stop appending once a result exceeds the cap so `pieces` remains
            # a contiguous prefix of results.
            if not full and kept + size <= max_chars:
                pieces.append(text)
                kept += size
            else:
                full = True
            total += size
            count += 1
            # Always read at least two results to distinguish a single string
            # from a multi-result stream.
            if (
                count >= 2
                and full
                and (total > count_chars or time.monotonic() > deadline)
            ):
                capped = True
                break
    except ValueError as exc:
        return {
            "error": f"jq error: {exc}",
            "text": "\n".join(pieces) if pieces else first_text[:max_chars],
        }
    first_cut = count > 0 and not pieces
    return {
        "text": first_text[:max_chars] if first_cut else "\n".join(pieces),
        "truncated": len(pieces) < count,
        "single_string": count == 1 and first_is_string,
        "total_chars": total,
        "total_capped": capped,
        "results_returned": len(pieces),
        "results_total": count,
        "first_cut": first_cut,
        "first_is_string": first_is_string,
        "first_chars": len(first_text),
    }


def set_resource_limits() -> None:
    """Cap the process's address space and CPU time, and disable core dumps.

    Clamps each limit to the process's existing hard limit. Disables core dumps
    because hitting `RLIMIT_AS` (abort) or `RLIMIT_CPU` (SIGXCPU) would
    otherwise write a core file up to the size of the address space.
    """
    for kind, soft, hard in (
        (resource.RLIMIT_AS, MEMORY_LIMIT_BYTES, MEMORY_LIMIT_BYTES),
        (resource.RLIMIT_CPU, CPU_LIMIT_SECONDS, CPU_LIMIT_SECONDS + 1),
        (resource.RLIMIT_CORE, 0, 0),
    ):
        current_hard = resource.getrlimit(kind)[1]
        if current_hard == resource.RLIM_INFINITY:
            resource.setrlimit(kind, (soft, hard))
        else:
            resource.setrlimit(
                kind, (min(soft, current_hard), min(hard, current_hard))
            )


def main() -> None:
    """Read one request from stdin and write its reply to stdout."""
    # `clear` calls `unsetenv`, which also empties the C `environ` libjq reads.
    os.environ.clear()
    set_resource_limits()
    request = json.loads(sys.stdin.readline())
    reply = run_request(request, sys.stdin.read())
    sys.stdout.write(json.dumps(reply, ensure_ascii=False))


if __name__ == "__main__":
    main()
