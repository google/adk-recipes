# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Tests for the System 1 and System 2 eval metrics, datasets, and runner."""

from __future__ import annotations

import json
import sys
import time
import types
from pathlib import Path

import pytest
import yaml
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from tests.eval.voice import voice_checks

ROOT = Path(__file__).resolve().parents[2]
DATASETS = ROOT / "tests" / "eval" / "datasets"
# The eval scripts import each other by module name, as they do when run.
sys.path[:0] = [str(ROOT / "tests" / "eval"), str(ROOT / "scripts")]
import history  # noqa: E402


def _ensure_app_package() -> None:
    if "app" in sys.modules:
        return
    try:
        import app  # noqa: F401
    except ModuleNotFoundError:
        pkg = types.ModuleType("app")
        pkg.__path__ = [str(ROOT / "app")]
        sys.modules["app"] = pkg


_ensure_app_package()
from app.store.models import Base, ThreadRecord  # noqa: E402
from app.threads.model import (  # noqa: E402
    Instruction,
    InstructionKind,
    ThreadHandle,
    ThreadState,
)


@pytest.fixture(autouse=True)
def _repo_root(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.chdir(ROOT)
    history._entries.cache_clear()


@pytest.fixture
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    db = tmp_path / "threads.db"
    engine = create_engine(f"sqlite:///{db}")
    Base.metadata.create_all(engine)
    monkeypatch.setenv("THREAD_DB_URL", f"sqlite+aiosqlite:///{db}")
    voice_checks._threads_cached.cache_clear()

    def add(handle: ThreadHandle) -> None:
        with Session(engine) as session:
            session.add(
                ThreadRecord(
                    thread_id=handle.thread_id,
                    principal_id=handle.owner,
                    state=handle.state.value,
                    data=handle.to_dict(),
                )
            )
            session.commit()
        voice_checks._threads_cached.cache_clear()

    return add


def trace(case_id: str, *turns: list[dict]) -> dict:
    return {
        "eval_case_id": case_id,
        "agent_data": {
            "turns": [
                {
                    "turn_index": i,
                    "events": [{"content": {"role": "model", "parts": parts}}],
                }
                for i, parts in enumerate(turns)
            ]
        },
    }


def call(name: str, **args) -> dict:
    return {"function_call": {"name": name, "args": args}}


def response(name: str, payload: dict) -> dict:
    return {"function_response": {"name": name, "response": payload}}


def thread(
    repo: str, state: ThreadState, *kinds: InstructionKind, **fields
) -> ThreadHandle:
    return ThreadHandle(
        thread_id=f"thread-{repo}",
        goal="goal",
        project=f"code/{repo}" if repo else "scratch/2026-01-01-goal",
        harness=fields.pop("harness", "claude"),
        branch=f"agent/thread-{repo}",
        state=state,
        instructions=[Instruction(k, "text", 1) for k in kinds],
        **fields,
    )


ALL_DATASETS = sorted(p.name for p in DATASETS.glob("*.json"))


@pytest.mark.parametrize("dataset", ALL_DATASETS)
def test_every_case_has_embedded_expectations(dataset: str):
    cases = json.loads((DATASETS / dataset).read_text())["eval_cases"]
    assert cases
    missing = [
        c["eval_case_id"]
        for c in cases
        if not isinstance(c.get("expectations"), dict)
        or voice_checks._expect(c) is None
    ]
    assert not missing


def test_approval_requires_consent_in_that_turn():
    dispatch = call("start_thread", goal="Reply to the school", project="")
    approve = call("approve", thread_id="thread-general")

    early = trace("gated_send", [dispatch], [], [approve], [])
    assert voice_checks.handoff_alignment(early)["score"] == 0.0
    assert voice_checks.contract_invariants(early)["score"] == 0.0

    on_consent = trace(
        "gated_send",
        [dispatch, {"text": "On it."}],
        [{"text": "I'll tell you when it's drafted."}],
        [
            {
                "text": "A reply to Ms. Rivera at Lincoln saying Maya can go on the field trip."
            }
        ],
        [approve, {"text": "Sent."}],
    )
    assert voice_checks.handoff_alignment(on_consent)["score"] == 1.0
    assert voice_checks.voice_checks(on_consent)["score"] == 1.0
    assert voice_checks.contract_invariants(on_consent)["score"] == 1.0
    assert voice_checks.tool_trajectory(on_consent)["score"] == 1.0


def test_publishing_requires_consent_in_that_turn():
    dispatch = call(
        "start_thread",
        goal="Fix the rounding bug",
        project="code/payments-api",
    )
    publish = call("publish_thread", thread_id="thread-payments-api")
    early = trace("publish_consent", [dispatch], [], [publish], [])
    assert (
        "publish_thread without consent in turn 2"
        in (voice_checks.contract_invariants(early)["explanation"])
    )
    confirmed = trace(
        "publish_consent",
        [dispatch],
        [],
        [{"text": "Push the rounding fix?"}],
        [publish],
    )
    assert voice_checks.contract_invariants(confirmed)["score"] == 1.0


def test_invariants_flag_redispatch_unknown_ids_and_polling():
    dispatch = call("start_thread", goal="Find a dentist", project="")
    result = voice_checks.handoff_alignment(
        trace(
            "question_answered",
            [dispatch],
            [
                call("read_thread", thread_id="thread-x"),
                call("read_thread", thread_id="thread-x"),
            ],
            [dispatch],
        )
    )
    assert result["score"] == 0.0
    for problem in (
        "2 start_thread calls",
        "unknown thread_id thread-x",
        "polled read_thread",
    ):
        assert problem in result["explanation"]


def test_current_is_graded_by_the_thread_it_reached():
    steer = call(
        "message_thread", thread_id="current", instruction="Kid-friendly"
    )
    steer["function_call"]["id"] = "c1"
    reached = {
        "function_response": {
            "id": "c1",
            "name": "message_thread",
            "response": {"result": {"ok": True, "thread_id": "thread-940"}},
        }
    }
    result = voice_checks.handoff_alignment(
        trace("thread_follow_up", [steer, reached])
    )
    assert "thread-940" not in result["explanation"]
    assert "unknown thread_id" not in result["explanation"]


def test_quick_reads_use_look_not_a_thread():
    cal = trace(
        "free_at_three",
        [
            call("look", app="calendar", window="tomorrow"),
            {"text": "Checking."},
        ],
    )
    assert voice_checks.handoff_alignment(cal)["score"] == 1.0

    bad_cal = trace(
        "free_at_three",
        [
            call(
                "start_thread",
                goal="Check tomorrow's calendar",
                project="code/payments-api",
            ),
            {"text": "Checking your calendar."},
        ],
    )
    res = voice_checks.handoff_alignment(bad_cal)
    assert res["score"] == 0.0
    assert "missing look" in res["explanation"]
    assert "called forbidden start_thread" in res["explanation"]


def _with(expectations: dict, *turns: list[dict]) -> dict:
    return {**trace("inline_case", *turns), "expectations": expectations}


@pytest.mark.parametrize(
    ("snapshot", "score"),
    [
        (
            thread(
                "payments-api",
                ThreadState.COMPLETED,
                InstructionKind.GOAL,
                InstructionKind.FOLLOWUP,
            ),
            1.0,
        ),
        (
            thread("payments-api", ThreadState.EXECUTING, InstructionKind.GOAL),
            0.0,
        ),
        (
            thread(
                "payments-api",
                ThreadState.COMPLETED,
                InstructionKind.GOAL,
                InstructionKind.FOLLOWUP,
                harness="antigravity",
            ),
            0.0,
        ),
    ],
)
def test_thread_outcome_reads_the_final_snapshot(store, snapshot, score):
    store(snapshot)
    outcome = {
        "project": "code/payments-api",
        "state": "completed",
        "instructions": ["goal", "followup"],
        "harness": "claude",
    }
    assert (
        voice_checks.thread_outcome(_with({"outcome": outcome}))["score"]
        == score
    )


def test_each_case_reads_its_own_store_and_harness_log(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.delenv("THREAD_DB_URL", raising=False)
    monkeypatch.setenv("EVAL_RUN_DIR", str(tmp_path))
    case_dir = tmp_path / "inline_case"
    engine = create_engine(f"sqlite:///{case_dir / 'threads.db'}")
    case_dir.mkdir()
    Base.metadata.create_all(engine)
    handle = thread("payments-api", ThreadState.COMPLETED, InstructionKind.GOAL)
    with Session(engine) as session:
        session.add(
            ThreadRecord(
                thread_id=handle.thread_id,
                principal_id=handle.owner,
                state=handle.state.value,
                data=handle.to_dict(),
            )
        )
        session.commit()
    voice_checks._threads_cached.cache_clear()
    want = {
        "outcome": {"project": "code/payments-api", "state": "completed"},
        "harness_prompt": {"contains": ["rounding"]},
    }
    instance = _with(
        want, [call("message_thread", thread_id="thread-payments-api")]
    )
    assert voice_checks.thread_outcome(instance)["score"] == 1.0
    assert voice_checks.contract_invariants(instance)["score"] == 1.0
    assert (
        "never started" in voice_checks.harness_context(instance)["explanation"]
    )
    log = case_dir / "server-1" / "agent" / "state" / "calls.jsonl"
    log.parent.mkdir(parents=True)
    log.write_text(json.dumps({"prompt": "Fix the rounding bug."}) + "\n")
    assert voice_checks.harness_context(instance)["score"] == 1.0
    other = {**instance, "eval_case_id": "another_case"}
    assert voice_checks.harness_context(other)["score"] == 0.0


def test_spoken_compliance_checks_format_paths_and_mentions():
    want = {"mentions": ["backoff"]}
    clean = _with(want, [{"text": "It went with exponential backoff."}])
    assert voice_checks.spoken_compliance(clean)["score"] == 1.0

    noisy = _with(
        want,
        [{"text": "See **retry.py** in /workspace/src/retry.py at https://x"}],
    )
    result = voice_checks.spoken_compliance(noisy)
    assert result["score"] == 0.0
    assert "never said 'backoff'" in result["explanation"]
    assert "raw file path" in result["explanation"]
    assert "markdown" in result["explanation"]


@pytest.mark.parametrize(
    "spoken, flagged",
    [
        (
            "Thread one two zero bravo six six alpha alpha is building the docs.",
            True,
        ),
        ("thread-3e2bdd8b is ready.", True),
        ("Approving thread nine oh one now.", True),
        ("The thread added backoff to the payments retry thread.", False),
        ("The payments thread is done, with exponential backoff.", False),
    ],
)
def test_spoken_compliance_flags_thread_ids(spoken: str, flagged: bool):
    result = voice_checks.spoken_compliance(_with({}, [{"text": spoken}]))
    assert ("said a thread ID aloud" in result["explanation"]) is flagged


@pytest.mark.parametrize("dataset", ALL_DATASETS)
def test_every_case_starts_from_fixtures_that_exist(dataset: str):
    fixtures = set(yaml.safe_load(history.FIXTURES.read_text()) or {})
    missing = [
        (case["eval_case_id"], name)
        for case in json.loads((DATASETS / dataset).read_text())["eval_cases"]
        for name in history.case_history_names(case["eval_case_id"])
        if name not in fixtures
    ]
    assert not missing, f"history fixtures missing from {history.FIXTURES}"


@pytest.mark.parametrize("dataset", ALL_DATASETS)
def test_every_expected_tool_is_registered_on_root_agent(
    dataset: str, monkeypatch: pytest.MonkeyPatch
):
    from app.agent import root_agent
    from app.tools.memory_tools import episode_tools

    monkeypatch.setenv("GOOGLE_CLOUD_AGENT_ENGINE_ID", "eval-fake-engine")
    monkeypatch.setenv("GOOGLE_CLOUD_AGENT_ENGINE_LOCATION", "us-central1")
    registered = {
        getattr(t, "name", None) or getattr(t, "__name__", None)
        for t in [*root_agent.tools, *episode_tools()]
    }
    cases = json.loads((DATASETS / dataset).read_text())["eval_cases"]
    for case in cases:
        exp = case.get("expectations") or {}
        tools = exp.get("tools") or {}
        names: set[str] = set()
        for key in ("required", "forbidden", "order"):
            names.update(tools.get(key) or [])
        for key in (
            "args",
            "args_all",
            "args_contain",
            "args_exclude",
            "max_calls",
            "min_calls",
        ):
            names.update((exp.get(key) or {}).keys())
        kickoff = (exp.get("delivery") or {}).get("kickoff") or {}
        if kickoff.get("tool"):
            names.add(kickoff["tool"])
        if dataset == "system2.json":
            names.discard("run_case")
        unknown = names - registered
        assert not unknown, f"{dataset}:{case['eval_case_id']} -> {unknown}"


@pytest.mark.parametrize("dataset", ALL_DATASETS)
def test_no_dataset_turn_starts_with_unemitted_bracket_tag(dataset: str):
    import re

    emitted_prefixes = ("[APP CONNECTED]", "[SCREEN]")
    for case in json.loads((DATASETS / dataset).read_text())["eval_cases"]:
        cid = case["eval_case_id"]
        texts: list[str] = []
        if prompt := case.get("prompt"):
            for part in prompt.get("parts") or []:
                if isinstance(part, dict) and part.get("text"):
                    texts.append(part["text"])
        for turn in (case.get("agent_data") or {}).get("turns") or []:
            for ev in turn.get("events") or []:
                for part in (ev.get("content") or {}).get("parts") or []:
                    if isinstance(part, dict) and part.get("text"):
                        texts.append(part["text"])
        for text in texts:
            stripped = text.lstrip()
            if re.match(r"^\[[A-Z0-9_ -]+\]", stripped):
                assert stripped.startswith(emitted_prefixes), (
                    f"{dataset}:{cid} starts with unemitted bracket tag: {stripped[:60]!r}"
                )


def test_history_focus_fixture_sets_initial_screen_focus(
    monkeypatch: pytest.MonkeyPatch,
):
    import sitecustomize

    from app.auth.identity import dev_user_id
    from app.voice.focus import clear_focus, get_focus

    clear_focus()
    monkeypatch.setenv(
        "EVAL_HISTORY", "alex,alex_portland,alex_portland_packing"
    )
    loaded = history.current()
    assert loaded.focus == {
        "view": "thread",
        "project": "projects/portland-trip",
        "thread_id": "thread-941",
    }
    sitecustomize._install_history()
    focus = get_focus(dev_user_id())
    assert (focus["view"], focus["project"], focus["thread_id"]) == (
        "thread",
        "projects/portland-trip",
        "thread-941",
    )


def test_every_fixture_thread_is_a_valid_snapshot():
    import seed_eval_store

    fixtures = yaml.safe_load(history.FIXTURES.read_text())
    for name in fixtures:
        for spec in history.load([name]).threads:
            handle = seed_eval_store.thread_from_fixture(spec)
            assert handle.thread_id == spec["thread_id"]
            assert (handle.ended_at is None) == ("ended" not in spec)


@pytest.mark.parametrize(
    ("spec", "seconds"),
    [("90s", 90), ("2m", 120), ("5h", 18000), ("3d", 259200)],
)
def test_fixture_times_are_relative_to_now(spec: str, seconds: int):
    assert abs(time.time() - history.seconds_ago(spec) - seconds) < 2
    with pytest.raises(ValueError):
        history.seconds_ago("three days")


def test_ids_are_matched_and_restating_is_caught():
    want = {
        "tools": {"required": ["share_context"]},
        "args": {"share_context": {"from_thread_id": "thread-831"}},
        "args_exclude": {"share_context": ["thread-833"]},
    }
    by_id = call(
        "share_context",
        from_thread_id="thread-831",
        to_target="payments-api",
        instruction="Ship both as one release",
    )
    assert voice_checks.handoff_alignment(_with(want, [by_id]))["score"] == 1.0
    restated = call(
        "share_context",
        from_thread_id="thread-831",
        to_target="code/payments-api",
        instruction="Ship refunds, disputes, and thread-833's key rotation",
    )
    result = voice_checks.handoff_alignment(_with(want, [restated]))
    assert (
        result["score"] == 0.0
        and "restate 'thread-833'" in result["explanation"]
    )


def test_max_calls_and_argument_words():
    want = {
        "tools": {"required": ["message_thread"]},
        "max_calls": {"message_thread": 1},
        "args_contain": {"message_thread": {"instruction": ["backoff"]}},
    }
    steer = call(
        "message_thread",
        thread_id="thread-821",
        instruction="backoff",
    )
    known = [
        {
            "function_response": {
                "name": "list_threads",
                "response": {"result": "thread-821 thread-822"},
            }
        }
    ]
    once = _with(want, [*known, steer])
    assert voice_checks.handoff_alignment(once)["score"] == 1.0
    twice = _with(
        want, [*known, steer, call("message_thread", thread_id="thread-822")]
    )
    assert (
        "2 message_thread calls"
        in voice_checks.handoff_alignment(twice)["explanation"]
    )


def test_forbidden_mentions():
    stale = _with(
        {"forbid_mentions": ["Tahoe"]},
        [{"text": "Hi! Still planning the Tahoe trip?"}],
    )
    assert (
        "said 'Tahoe'" in voice_checks.spoken_compliance(stale)["explanation"]
    )


def test_harness_context_reads_what_the_coding_agent_was_told(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setenv("EVAL_RUN_DIR", str(tmp_path))
    case = _with({"harness_prompt": {"contains": ["cart fixture"]}})
    assert "never started" in voice_checks.harness_context(case)["explanation"]
    log = tmp_path / "server-1" / "agent" / "state" / "calls.jsonl"
    log.parent.mkdir(parents=True)
    log.write_text(
        json.dumps({"prompt": "Thread: fix it. Only fails on Safari."}) + "\n"
    )
    result = voice_checks.harness_context(case)
    assert (
        result["score"] == 0.0
        and "lacks 'cart fixture'" in result["explanation"]
    )
    log.write_text(
        json.dumps({"prompt": "Safari only; see the cart fixture."}) + "\n"
    )
    assert voice_checks.harness_context(case)["score"] == 1.0


def test_a_case_carries_its_rubrics_as_its_reference():
    import run_eval

    case = {
        "eval_case_id": "memory_edits__ho",
        "agent_data": {"turns": []},
    }
    graded = run_eval.with_rubrics(case)
    text = graded["reference"]["response"]["parts"][0]["text"]
    assert text.startswith(
        "1. Saves that Leo is allergic to peanuts (remember)."
    )
    assert "\n2. " in text
    assert "reference" in run_eval.with_rubrics({"eval_case_id": "gated_send"})
    assert "reference" not in run_eval.with_rubrics(
        {"eval_case_id": "unregistered_case_without_rubrics"}
    )


def test_the_rubric_judge_and_custom_metrics_compile_cleanly():
    import run_eval
    from google.agents.cli.eval.eval_utils import prepare_eval_metrics

    assert run_eval.RUBRICS_METRIC in run_eval.metrics(rubrics=True)
    without = run_eval.metrics(rubrics=False)
    assert (
        run_eval.RUBRICS_METRIC not in without
        and "handoff_alignment" in without
        and "spoken_compliance" in without
    )
    metrics_s1, local_s1, _ = prepare_eval_metrics(str(run_eval.CONFIG), None)
    assert local_s1 >= 3
    assert any(getattr(m, "name", "") == "rubrics" for m in metrics_s1)


def test_run_eval_builds_native_agents_cli_commands():
    import run_eval

    focus_cmd = run_eval.build_eval_command(suite="system1")
    assert focus_cmd[:6] == [
        "uv",
        "run",
        "agents-cli",
        "eval",
        "run",
        "--mode",
    ]
    assert "adk_live" in focus_cmd
    assert any(p.endswith("eval_config_system1.yaml") for p in focus_cmd)
    assert any(p.endswith("system1.json") for p in focus_cmd)

    sys2_cmd = run_eval.build_eval_command(
        suite="system2", url="http://127.0.0.1:18081"
    )
    assert "adk" in sys2_cmd
    assert any(p.endswith("eval_config_system2.yaml") for p in sys2_cmd)
    assert any(p.endswith("system2.json") for p in sys2_cmd)
    assert "--app-name" in sys2_cmd and "system2_agent" in sys2_cmd


def test_cases_start_from_their_own_histories():
    import run_eval

    focus = run_eval.DATASETS_DIR / "system1.json"
    assert history.case_history_names("free_at_three") == ["alex"]
    assert history.case_history_names("dropped_call") == [
        "alex",
        "alex_dropped_call",
    ]
    assert history.case_history_names("session_start") == ["alex", "alex_away"]
    # So one server can't serve the whole set: each case gets its own.
    with pytest.raises(SystemExit, match="different histories"):
        run_eval.dataset_history(focus)


def test_each_case_runs_from_its_own_dataset_history_and_store(tmp_path: Path):
    import run_eval

    focus = run_eval.DATASETS_DIR / "system1.json"
    case = json.loads(focus.read_text(encoding="utf-8"))["eval_cases"][0]
    case_id = case["eval_case_id"]

    dataset = run_eval.case_dataset(case, tmp_path)
    assert dataset == tmp_path / "cases" / f"{case_id}.json"
    (written,) = json.loads(dataset.read_text(encoding="utf-8"))["eval_cases"]
    assert written == run_eval.with_rubrics(case)

    env = run_eval.case_env({"KEEP": "1"}, case_id, tmp_path)
    run_dir = tmp_path / case_id
    assert run_dir.is_dir()
    assert env == {
        "KEEP": "1",
        "EVAL_HISTORY": ",".join(history.case_history_names(case_id)),
        "EVAL_RUN_DIR": str(run_dir),
        "THREAD_DB_URL": f"sqlite+aiosqlite:///{run_dir}/threads.db",
        "VOICE_EVAL_FAKES": "1",
    }

    generate = run_eval.generate_command(
        dataset, tmp_path / "traces" / f"{case_id}.json", "http://127.0.0.1:1"
    )
    assert generate[:5] == ["uv", "run", "agents-cli", "eval", "generate"]
    assert generate[generate.index("--url") + 1] == "http://127.0.0.1:1"
    assert generate[generate.index("--dataset") + 1] == str(dataset)
    grade = run_eval.grade_command(
        tmp_path / "traces", run_eval.CONFIG, tmp_path, skip_metrics="rubrics"
    )
    assert grade[grade.index("--traces") + 1] == str(tmp_path / "traces")
    assert "rubrics" not in grade[grade.index("--metrics") + 1].split(",")


def test_the_report_averages_trials_and_keeps_held_out_cases_apart(
    tmp_path: Path,
):
    import run_eval

    def graded(case: str, trial: int, **scores: float) -> None:
        out = tmp_path / case / f"trial-{trial}"
        out.mkdir(parents=True)
        metrics = {
            name: {"score": score, "explanation": "why" if score < 1 else "ok"}
            for name, score in scores.items()
        }
        (out / "results_1.json").write_text(
            json.dumps(
                {
                    "eval_case_results": [
                        {
                            "response_candidate_results": [
                                {"metric_results": metrics}
                            ]
                        }
                    ]
                }
            )
        )

    graded("recall_old_conversation", 1, rubrics=1.0, tool_trajectory=1.0)
    graded("recall_old_conversation", 2, rubrics=0.5, tool_trajectory=0.0)
    graded("recall_old_conversation__ho", 1, rubrics=0.0, tool_trajectory=1.0)
    report = run_eval.write_report(tmp_path)
    summary = json.loads((tmp_path / "summary.json").read_text())
    assert summary["overall"]["dev"] == {
        "rubrics": 0.75,
        "tool_trajectory": 0.5,
    }
    assert summary["overall"]["held_out"]["rubrics"] == 0.0
    assert (
        "## Held out" in report
        and "| recall_old_conversation | 0.75 | 0.50 |" in report
    )
