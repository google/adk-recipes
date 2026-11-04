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

"""Unit tests for the session reviewer's jq tool and its child-process worker.

The jq expressions run for real. All session data is synthetic.
"""

from __future__ import annotations

import concurrent.futures
import json
import os
import pathlib
import re
import shlex
import subprocess
import sys
import threading
from typing import Any

import pytest
from ambient_quality_agent.tools.agent_revision import AgentRevision
from ambient_quality_agent.tools.review.agentic_reviewer import trajectory_json
from ambient_quality_agent.tools.review.agentic_reviewer.tools import (
    _jq_worker,
    jq,
)
from google.genai import types

from tests.test_agentic_reviewer import WORKER, _build_case


def test_the_query_declaration_takes_one_jq_expression() -> None:
    declaration = jq.build_query_declaration()

    assert declaration.name == jq.QUERY_TOOL
    assert (declaration.description or "").startswith("A standard jq tool.")
    assert f"Output stops after {jq.QUERY_OUTPUT_CHARS:,} characters." in (
        declaration.description or ""
    )
    assert declaration.parameters == types.Schema(
        type=types.Type.OBJECT,
        properties={
            "expr": types.Schema(
                type=types.Type.STRING, description="A jq expression."
            )
        },
        required=["expr"],
    )


PAGED_INSTRUCTION = "".join(
    f"line {index:05d} of the rules\n" for index in range(3_000)
)


def _build_query_tool(instruction: str = PAGED_INSTRUCTION) -> jq.JqQueryTool:
    """Build a `JqQueryTool` for the sample case with the given worker instruction.

    Args:
        instruction: Worker agent instruction.

    Returns:
        Initialized `JqQueryTool`.
    """
    revisions = [AgentRevision(agent_id=WORKER, instruction=instruction)]
    return jq.JqQueryTool(
        trajectory_json.build_trajectory_json(_build_case(), revisions)
    )


def test_query_trajectory_pages_a_long_string_at_30000_chars() -> None:
    tool = _build_query_tool()
    cap = jq.QUERY_OUTPUT_CHARS
    total = len(PAGED_INSTRUCTION)

    first = tool.query_trajectory(".agents[0].instruction").text
    text, note = first.rsplit("\n", 1)
    assert cap == 30_000
    assert text == PAGED_INSTRUCTION[:cap]
    assert note == (
        f"[truncated: returned chars 0-{cap} of {total}; continue with "
        f"`.agents[0].instruction[{cap}:]`]"
    )
    second = tool.query_trajectory(f".agents[0].instruction[{cap}:]").text
    text, note = second.rsplit("\n", 1)
    assert text == PAGED_INSTRUCTION[cap : 2 * cap]
    assert note.startswith(
        f"[truncated: returned chars {cap}-{2 * cap} of chars {cap}-{total}"
    )


def test_query_trajectory_pages_many_results_at_30000_chars() -> None:
    tool = _build_query_tool()
    expr = '.agents[0].instruction | scan(".*rules.*")'

    first = tool.query_trajectory(expr).text
    *results, note = first.split("\n")
    assert len("\n".join(results)) <= 30_000
    assert len("\n".join(results)) > 29_000
    match = re.fullmatch(
        r"\[truncated: returned results 0-(\d+) of 3000 \(\d+ chars\); "
        r"continue with `(.*)`\]",
        note,
    )
    assert match is not None
    last = int(match[1])
    assert results[-1] == f"line {last:05d} of the rules"
    second = tool.query_trajectory(match[2]).text
    assert second.split("\n", 1)[0] == f"line {last + 1:05d} of the rules"
    assert f"returned results {last + 1}-" in second.rsplit("\n", 1)[1]


def test_concurrent_jq_paging_keeps_each_continuation_absolute() -> None:
    tool = _build_query_tool()
    # Every line matches each pattern, so each result set spans three pages.
    expressions = [
        f'.agents[0].instruction | scan(".*{word}.*")'
        for word in ("line", "of", "the", "rules")
    ]

    def page_twice(expr: str) -> tuple[int, str]:
        first = tool.query_trajectory(expr).text.rsplit("\n", 1)[1]
        match = re.search(
            r"returned results 0-(\d+) .*continue with `(.*)`", first
        )
        assert match is not None, first
        second = tool.query_trajectory(match[2]).text.rsplit("\n", 1)[1]
        return int(match[1]), second

    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        pages = list(pool.map(page_twice, expressions))

    for last, note in pages:
        assert note.startswith(f"[truncated: returned results {last + 1}-"), (
            note
        )


@pytest.mark.parametrize(
    ("expr", "text"),
    [
        (
            "[.turns[].events[] | select(.content.parts[0].text) | .event_id]",
            '["t0.e0","t0.e3"]',
        ),
        ("empty", "(no results)"),
        ('""', "(one empty string)"),
    ],
)
def test_query_trajectory_reads_the_trajectory_json(
    expr: str, text: str
) -> None:
    output = _build_query_tool("Short.").query_trajectory(expr)

    assert not output.is_error
    assert output.text == text


def test_a_jq_error_is_an_error_result() -> None:
    output = _build_query_tool("Short.").query_trajectory(".agents[")

    assert output.is_error
    assert "jq compile error" in output.text


LONG_INSTRUCTION = "".join(f"{index:09d}\n" for index in range(9_000))
"""A 90,000-char instruction whose every character offset is easy to check."""


def _query_pages(
    first_expr: str, *, pages: int, instruction: str = LONG_INSTRUCTION
) -> list[str]:
    """Follow the continuation of each truncation note on one tool instance.

    Args:
        first_expr: The first jq expression.
        pages: Number of queries to run.
        instruction: The worker's instruction, at `.agents[0].instruction`.

    Returns:
        The text of each query, in order.
    """
    tool = _build_query_tool(instruction)
    outputs = [tool.query_trajectory(first_expr).text]
    while len(outputs) < pages:
        outputs.append(tool.query_trajectory(_extract_quoted(outputs[-1])).text)
    return outputs


def _extract_quoted(output: str, after: str = "continue with") -> str:
    """Extract the expression a truncation note quotes after some words.

    Args:
        output: A tool output ending in a truncation note.
        after: The words before the quoted expression.

    Returns:
        The expression between the backticks.
    """
    note = output.rsplit("\n[truncated: ", 1)[1]
    return note.split(f"{after} `", 1)[1].split("`", 1)[0]


def _split_note(output: str) -> tuple[str, str]:
    """Split a tool output into its text and its truncation note.

    Args:
        output: A tool output ending in a truncation note.

    Returns:
        The text before the note, and the note.
    """
    text, note = output.rsplit("\n[truncated: ", 1)
    return text, "[truncated: " + note


def test_a_truncated_slice_continues_in_absolute_offsets() -> None:
    cap = jq.QUERY_OUTPUT_CHARS

    output = _build_query_tool(LONG_INSTRUCTION).query_trajectory(
        ".agents[0].instruction[100:50000]"
    )

    text, note = _split_note(output.text)
    assert text == LONG_INSTRUCTION[100 : 100 + cap]
    assert note == (
        f"[truncated: returned chars 100-{100 + cap} of chars "
        f"100-50000; continue with `.agents[0].instruction[{100 + cap}:50000]`]"
    )


def test_a_string_from_an_expression_that_is_not_a_path_continues_in_parentheses() -> (
    None
):
    cap = jq.QUERY_OUTPUT_CHARS
    expr = ".agents[0].instruction | ascii_downcase"

    outputs = _query_pages(expr, pages=3)

    text, note = _split_note(outputs[0])
    assert text == LONG_INSTRUCTION[:cap]
    assert note == (
        f"[truncated: returned chars 0-{cap} of {len(LONG_INSTRUCTION)}; "
        f"continue with `({expr})[{cap}:]`]"
    )
    text, note = _split_note(outputs[1])
    assert text == LONG_INSTRUCTION[cap : 2 * cap]
    assert note == (
        f"[truncated: returned chars {cap}-{2 * cap} of chars "
        f"{cap}-{len(LONG_INSTRUCTION)}; continue with `({expr})[{2 * cap}:]`]"
    )
    assert outputs[2] == LONG_INSTRUCTION[2 * cap :]


def test_many_results_end_at_a_result_boundary_and_continue_flat() -> None:
    expr = '.agents[0].instruction | split("\\n")[]'
    lines = LONG_INSTRUCTION.split("\n")
    # Each 9-digit line takes 10 chars with its separator, and the first one
    # needs none, so 3,000 whole lines fit in 29,999 chars.
    per_page = 3_000

    outputs = _query_pages(expr, pages=3)

    for page, output in enumerate(outputs[:2]):
        text, note = _split_note(output)
        start = page * per_page
        assert text == "\n".join(lines[start : start + per_page])
        scope = f" from result {start}" if start else ""
        assert note == (
            f"[truncated: returned results {start}-{start + per_page - 1} of "
            f"{len(lines)} ({90000 - start * 10} chars{scope}); continue with "
            f"`[{expr}][{start + per_page}:][]`]"
        )
    assert outputs[2] == "\n".join(lines[2 * per_page :])


def test_an_unbounded_query_reports_a_lower_bound() -> None:
    output = _build_query_tool("Short.").query_trajectory('repeat("abc")')

    text, note = _split_note(output.text)
    assert set(text.split("\n")) == {"abc"}
    count = len(text.split("\n"))
    assert note.startswith(
        f"[truncated: returned results 0-{count - 1} of more than "
    )
    assert note.endswith(
        f' chars); continue with `[repeat("abc")][{count}:][]`]'
    )


def test_a_large_string_result_inside_a_stream_is_read_in_slices() -> None:
    cap = jq.QUERY_OUTPUT_CHARS
    expr = '"a", .agents[0].instruction, "b"'
    tool = _build_query_tool(LONG_INSTRUCTION)

    first = tool.query_trajectory(expr).text
    second = tool.query_trajectory(_extract_quoted(first)).text
    third = tool.query_trajectory(
        _extract_quoted(second, "read the rest with")
    ).text
    rest = f"([{expr}][1])[{cap}:]"
    following = f"[{expr}][2:][]"

    assert _split_note(first) == (
        "a",
        f"[truncated: returned results 0-0 of 3 (90004 chars); continue with "
        f"`[{expr}][1:][]`]",
    )
    assert _split_note(second) == (
        LONG_INSTRUCTION[:cap],
        f"[truncated: result 1 alone is 90000 chars, returned its chars "
        f"0-{cap}; read the rest with `{rest}`, then continue with "
        f"`{following}`]",
    )
    end = 2 * cap
    assert _split_note(third) == (
        LONG_INSTRUCTION[cap:end],
        f"[truncated: returned chars {cap}-{end} of chars {cap}-90000; "
        f"continue with `([{expr}][1])[{end}:]`]",
    )
    assert tool.query_trajectory(_extract_quoted(second)).text == "b"


def test_one_large_value_pages_concatenate_to_its_tojson_text() -> None:
    import jq as jq_bindings

    cap = jq.QUERY_OUTPUT_CHARS
    trajectory = {
        "items": [
            {"name": f"café 😀 {index}\u007f", "score": index / 7}
            for index in range(1_500)
        ]
    }
    value_json = str(
        jq_bindings.compile(".items | tojson")
        .input_text(json.dumps(trajectory, ensure_ascii=False))
        .first()
    )
    # jq escapes DEL where Python's encoder does not, so the pages must come
    # from jq for their offsets to match `tojson` slices.
    assert "\\u007f" in value_json
    assert 2 * cap < len(value_json) <= 3 * cap
    tool = jq.JqQueryTool(trajectory)

    outputs = [tool.query_trajectory(".items").text]
    while "[truncated: " in outputs[-1]:
        outputs.append(tool.query_trajectory(_extract_quoted(outputs[-1])).text)

    first, first_note = _split_note(outputs[0])
    assert first_note == (
        f"[truncated: returned chars 0-{cap} of the value's {len(value_json)} "
        f"JSON chars; continue with `(.items | tojson)[{cap}:]`, or list its "
        f"keys with `.items | keys`]"
    )
    assert len(outputs) == 3
    assert first + _split_note(outputs[1])[0] + outputs[2] == value_json


@pytest.mark.parametrize(
    ("expr", "note"),
    [
        (
            ".a[5:]",
            "returned chars 5-15 of chars 5-45; continue with `.a[15:]`]",
        ),
        (
            ".a[:50]",
            "returned chars 0-10 of chars 0-40; continue with `.a[10:50]`]",
        ),
        (
            '.["k"].b',
            'returned chars 0-10 of 40; continue with `.["k"].b[10:]`]',
        ),
        (
            ".a[1][2]",
            "returned chars 0-10 of 40; continue with `.a[1][2][10:]`]",
        ),
        (".a[:]", "returned chars 0-10 of 40; continue with `(.a[:])[10:]`]"),
        (
            ".a[0:5] | .",
            "returned chars 0-10 of 40; continue with `(.a[0:5] | .)[10:]`]",
        ),
        (
            ".a[-5:]",
            "returned chars 0-10 of 40; continue with `(.a[-5:])[10:]`]",
        ),
        (
            ".a[1:2][0:5]",
            "returned chars 0-10 of 40; continue with `(.a[1:2][0:5])[10:]`]",
        ),
    ],
)
def test_the_truncation_note_slices_a_plain_path_and_parenthesizes_the_rest(
    expr: str, note: str
) -> None:
    reply = {"text": "x" * 10, "single_string": True, "total_chars": 40}

    rendered = jq.render_truncation_note(expr, reply)

    assert rendered.text == "[truncated: " + note
    # A plain path carries its own offsets, so only other expressions are
    # tracked.
    assert bool(rendered.continuations) == ("(" in note)


def test_a_continuation_of_an_expression_with_a_comment_still_runs() -> None:
    expr = ".a[] # every item \\"
    reply = {
        "text": "1\n2",
        "truncated": True,
        "single_string": False,
        "total_chars": 5,
        "results_returned": 2,
        "results_total": 3,
    }

    note = jq.render_truncation_note(expr, reply)

    (continuation,) = note.continuations
    assert continuation == "[.a[] # every item \\\n\n][2:][]"
    assert note.continuations[continuation] == jq.PageOrigin("results", expr, 2)
    assert _run_worker_request(continuation, {"a": [1, 2, 3]})["text"] == "3"


def _run_worker_request(expr: str, trajectory: Any) -> dict[str, Any]:
    """Run one request in-process in the worker, with small caps.

    Args:
        expr: The jq expression.
        trajectory: The trajectory JSON, as a decoded value.

    Returns:
        The worker's reply.
    """
    return _jq_worker.run_request(
        {
            "expr": expr,
            "max_chars": 100,
            "count_chars": 1000,
            "count_seconds": 1,
        },
        json.dumps(trajectory),
    )


def test_a_runtime_error_after_the_first_result_keeps_the_first() -> None:
    reply = _run_worker_request('.s, error("boom")', {"s": "kept"})

    assert reply == {"error": "jq error: boom", "text": "kept"}


@pytest.mark.parametrize(
    ("expr", "fields"),
    [
        (
            ".a[]",
            {
                "text": "\n".join(["x" * 30] * 3),
                "truncated": True,
                "single_string": False,
                "total_chars": 4 * 31 - 1,
                "total_capped": False,
                "results_returned": 3,
                "results_total": 4,
                "first_cut": False,
                "first_is_string": True,
                "first_chars": 30,
            },
        ),
        (
            ".b, .a",
            {
                "text": '{"k":"' + "y" * 94,
                "truncated": True,
                "single_string": False,
                "total_chars": 108 + 1 + 133,
                "total_capped": False,
                "results_returned": 0,
                "results_total": 2,
                "first_cut": True,
                "first_is_string": False,
                "first_chars": 108,
            },
        ),
    ],
)
def test_worker_replies_count_whole_results(
    expr: str, fields: dict[str, Any]
) -> None:
    reply = _run_worker_request(
        expr, {"a": ["x" * 30] * 4, "b": {"k": "y" * 100}}
    )

    assert reply == fields


def test_a_capped_count_stops_after_the_second_result() -> None:
    """A third read would raise, so the reply shows whether it happened."""
    reply = _run_worker_request(
        '.s, .s, error("third result read")', {"s": "x" * 600}
    )

    assert "error" not in reply
    assert reply["total_capped"]
    assert reply["results_total"] == 2


def test_a_compile_error_points_into_the_models_expression() -> None:
    reply = _run_worker_request(".turns[", {})

    assert reply["error"].startswith("jq compile error:")
    assert "line 1, column 7" in reply["error"]
    assert "tojson" not in reply["error"]


@pytest.mark.parametrize("expr", ["1), (2", '.s), ("forged"', "1) | (2"])
def test_an_expression_that_escapes_the_wrapper_is_a_compile_error(
    expr: str,
) -> None:
    reply = _run_worker_request(expr, {"s": "kept"})

    assert reply["error"].startswith("jq compile error:")


@pytest.mark.parametrize(
    ("expr", "text"),
    [
        ("def f: .s; f", "kept"),
        (". as $x | $x.n", '{"k":[1,2]}'),
        (".n # the trailing comment", '{"k":[1,2]}'),
        ("def f: .n.k[]; f # comment", "1\n2"),
        (".s # a comment that a backslash continues \\", "kept"),
    ],
)
def test_definitions_bindings_and_comments_survive_the_rendering(
    expr: str, text: str
) -> None:
    reply = _run_worker_request(expr, {"s": "kept", "n": {"k": [1, 2]}})

    assert reply["text"] == text
    assert not reply["truncated"]


def test_a_runaway_jq_expression_is_stopped_after_the_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(jq, "QUERY_TIMEOUT_SECONDS", 1)

    output = _build_query_tool("Short.").query_trajectory("last(range(1e15))")

    assert output.is_error
    assert "ran longer than 1 s" in output.text


def test_the_jq_worker_sees_no_environment(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    monkeypatch.setenv("AQA_TEST_SECRET", "not for jq")
    # Prepends so a shared-library interpreter keeps its own libpython path.
    monkeypatch.setenv("LD_LIBRARY_PATH", str(tmp_path), prepend=os.pathsep)

    output = _build_query_tool("Short.").query_trajectory(
        '$ENV | keys - ["LC_CTYPE"]'
    )

    assert output.text == "[]"


def test_a_shared_library_interpreter_starts_the_jq_worker(
    monkeypatch: pytest.MonkeyPatch, tmp_path: pathlib.Path
) -> None:
    # Stands in for a shared-library Python build, whose dynamic loader finds
    # libpython only through `LD_LIBRARY_PATH`.
    interpreter = tmp_path / "python"
    interpreter.write_text(
        "#!/bin/sh\n"
        f'case "${{LD_LIBRARY_PATH:-}}" in {shlex.quote(str(tmp_path))}*) ;;\n'
        "  *) echo 'error while loading shared libraries: libpython3.so:"
        " cannot open shared object file' >&2; exit 127 ;;\n"
        "esac\n"
        f'exec {shlex.quote(sys.executable)} "$@"\n',
        encoding="utf-8",
    )
    interpreter.chmod(0o755)
    monkeypatch.setattr(sys, "executable", str(interpreter))
    monkeypatch.setenv("LD_LIBRARY_PATH", str(tmp_path), prepend=os.pathsep)

    output = _build_query_tool("Short.").query_trajectory("1 + 1")

    assert not output.is_error, output.text
    assert output.text == "2"


def test_a_jq_worker_that_fails_is_reported_as_an_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Any
) -> None:
    broken = tmp_path / "worker.py"
    broken.write_text("raise SystemExit('worker exploded')\n", encoding="utf-8")
    monkeypatch.setattr(jq, "_JQ_WORKER", broken)

    output = _build_query_tool("Short.").query_trajectory("1")

    assert output.is_error
    assert output.text == "jq worker failed: worker exploded"


def test_a_jq_worker_that_fails_silently_reports_its_exit_code(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def run_worker(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess:
        return subprocess.CompletedProcess(args, 3, "", "")

    monkeypatch.setattr(jq.subprocess, "run", run_worker)

    output = _build_query_tool("Short.").query_trajectory("1")

    assert output.is_error
    assert output.text == "jq worker failed: exit code 3"


def test_a_string_doubling_expression_stops_at_the_memory_limit() -> None:
    output = _build_query_tool("Short.").query_trajectory(
        '"a" | reduce range(30) as $i (.; . + .) | length'
    )

    assert output.is_error
    assert output.text == (
        "jq error: the expression needed more than "
        f"{_jq_worker.MEMORY_LIMIT_BYTES >> 20} MiB of memory or "
        f"{_jq_worker.CPU_LIMIT_SECONDS} s of CPU time and was stopped; "
        "simplify it"
    )


def test_the_worker_cpu_limit_is_above_the_wall_clock_timeout() -> None:
    assert _jq_worker.CPU_LIMIT_SECONDS > jq.QUERY_TIMEOUT_SECONDS


@pytest.mark.parametrize(
    ("returncode", "stderr"),
    [
        (-9, ""),
        (-24, ""),
        (1, "Traceback (most recent call last):\nMemoryError\n"),
    ],
    ids=["killed", "cpu-limit", "python-memory-error"],
)
def test_a_worker_past_a_resource_limit_is_an_error_result(
    monkeypatch: pytest.MonkeyPatch, returncode: int, stderr: str
) -> None:
    def run_worker(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess:
        return subprocess.CompletedProcess(args, returncode, "", stderr)

    monkeypatch.setattr(jq.subprocess, "run", run_worker)

    output = _build_query_tool("Short.").query_trajectory("1")

    assert output.is_error
    assert output.text.startswith("jq error: the expression needed more than")


@pytest.mark.parametrize(
    "error",
    [OSError("pipe broken"), UnicodeDecodeError("utf-8", b"\xff", 0, 1, "bad")],
    ids=["os-error", "unicode-error"],
)
def test_a_worker_pipe_failure_is_an_error_result(
    monkeypatch: pytest.MonkeyPatch, error: Exception
) -> None:
    def fail(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess:
        raise error

    monkeypatch.setattr(jq.subprocess, "run", fail)

    output = _build_query_tool("Short.").query_trajectory("1")

    assert output.is_error
    assert output.text == f"jq worker failed: {error}"


def test_one_semaphore_bounds_the_jq_workers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Holds every fake worker until the bound is reached, so the check does
    not depend on timing."""
    slots = threading.BoundedSemaphore(2)
    monkeypatch.setattr(jq, "_WORKER_SLOTS", slots)
    condition = threading.Condition()
    release = threading.Event()
    running = 0
    peak = 0

    def run_worker(
        argv: list[str], **kwargs: Any
    ) -> subprocess.CompletedProcess:
        nonlocal running, peak
        with condition:
            running += 1
            peak = max(peak, running)
            condition.notify_all()
        assert release.wait(timeout=10)
        with condition:
            running -= 1
        stdout = json.dumps({"text": "1", "results_total": 1})
        return subprocess.CompletedProcess(argv, 0, stdout, "")

    monkeypatch.setattr(jq.subprocess, "run", run_worker)
    tool = _build_query_tool("Short.")
    with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
        submitted = [pool.submit(tool.query_trajectory, "1") for _ in range(4)]
        with condition:
            assert condition.wait_for(lambda: running == 2, timeout=10)
        # Both slots are held, so a third worker cannot start.
        assert not slots.acquire(blocking=False)
        release.set()
        results = [future.result(timeout=10) for future in submitted]

    assert peak == 2
    assert [result.text for result in results] == ["1"] * 4


@pytest.mark.parametrize(
    "expr",
    [
        'import "rules" as $rules {search: $DIR}; $rules',
        'import "rules" as rules {search: $DIR}; rules::x',
        'include "rules" {search: $DIR}; x',
        ' # a comment\n  include "rules" {search: $DIR}; x',
        'module {}; include "rules" {search: $DIR}; x',
    ],
    ids=["data-import", "module-import", "include", "after-comment", "module"],
)
def test_a_top_level_directive_is_a_compile_error(
    tmp_path: Any, expr: str
) -> None:
    """jq quotes the failing line of a module it loaded, so a loaded file
    would show its "local secret" in the error."""
    (tmp_path / "rules.json").write_text('"local secret" ]]', encoding="utf-8")
    (tmp_path / "rules.jq").write_text(
        'def x: "local secret" ||| ;\n', encoding="utf-8"
    )
    searched = expr.replace("$DIR", json.dumps(str(tmp_path)))
    tool = _build_query_tool("Short.")

    output = tool.query_trajectory(searched)

    assert output.is_error
    assert re.match(
        r"jq compile error: jq: error: syntax error, unexpected "
        r"(import|include|module) at <top-level>, line [23]",
        output.text,
    )
    assert "local secret" not in output.text
    assert "rules" not in output.text.split("\n", 1)[0]


def test_the_worker_caps_its_memory_and_cpu_and_writes_no_core() -> None:
    script = pathlib.Path(jq.__file__).with_name("_jq_worker.py")
    probe = (
        "import json, resource, runpy, sys\n"
        "runpy.run_path(sys.argv[1])['set_resource_limits']()\n"
        "print(json.dumps([resource.getrlimit(kind) for kind in (\n"
        "    resource.RLIMIT_AS, resource.RLIMIT_CPU, resource.RLIMIT_CORE)]))\n"
    )

    completed = subprocess.run(
        [sys.executable, "-I", "-c", probe, str(script)],
        capture_output=True,
        text=True,
        check=True,
    )

    memory, cpu, core = json.loads(completed.stdout)
    assert memory == [_jq_worker.MEMORY_LIMIT_BYTES] * 2
    assert cpu[0] == _jq_worker.CPU_LIMIT_SECONDS
    assert core == [0, 0]


@pytest.mark.parametrize("surrogate", ["\ud800", "\udc80"], ids=["high", "low"])
def test_a_lone_surrogate_in_the_trajectory_reads_as_a_replacement_character(
    surrogate: str,
) -> None:
    tool = jq.JqQueryTool(
        {"text": f"before {surrogate} after", "plain": "naïve 😀"}
    )

    assert tool.query_trajectory(".text | length").text == "14"
    assert tool.query_trajectory(".text").text == "before \ufffd after"
    assert tool.query_trajectory(".plain[5:6]").text == " "
    assert tool.query_trajectory(".plain[6:]").text == "😀"
