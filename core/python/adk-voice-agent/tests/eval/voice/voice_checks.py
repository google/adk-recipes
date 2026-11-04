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

"""Trace-native voice and System 1 -> System 2 handoff metrics for `agents-cli eval run`."""

from __future__ import annotations

import contextlib
import fnmatch
import glob
import json
import os
import re
import sqlite3
import sys
from functools import cache
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from history import (
    EXPECTATIONS,
    case_history_names,
    expectations_for,
)
from history import (
    load as load_history,
)

_STATUS_TOOLS = {"read_thread", "list_threads"}
_CONSENT_TOOLS = {"approve", "publish_thread"}
_CURRENT_THREAD = {"", "current", "this", "active"}


def spoken_compliance(instance: dict, **kwargs: Any) -> dict:
    del kwargs
    exp = _expect(instance) or {}
    text = " ".join(_speech(instance))
    if not text.strip():
        return {"score": 0.0, "explanation": "the agent said nothing"}
    problems = [
        label
        for label, pattern in (
            ("markdown", r"\*\*|`|^\s*[-*] "),
            ("raw URL", r"https?://"),
            ("raw file path", r"/(?:workspace|Users|home|tmp|var|etc)/\S+"),
            ("raw diff", r"@@ -|diff --git|\+\+\+ b/"),
            (
                "stack trace",
                r"Traceback \(most recent call last\)|site-packages/",
            ),
        )
        if re.search(pattern, text, re.MULTILINE)
    ]
    lower = text.lower()
    if _SPOKEN_THREAD_ID.search(lower):
        problems.append("said a thread ID aloud")
    for group in exp.get("mentions", []):
        options = (
            [str(w) for w in group]
            if isinstance(group, list | tuple)
            else [str(group)]
        )
        if options and not any(opt.lower() in lower for opt in options):
            label = options[0] if len(options) == 1 else " / ".join(options)
            problems.append(f"never said '{label}'")
    problems += [
        f"said '{w}'"
        for w in exp.get("forbid_mentions", [])
        if str(w).lower() in lower
    ]
    return _result(problems, "clean speech")


def handoff_alignment(instance: dict, **kwargs: Any) -> dict:
    del kwargs
    exp = _expect(instance)
    if exp is None:
        return _missing(instance)
    traj_problems, names = _trajectory_problems(exp, _calls(instance))
    inv_problems = _invariant_problems(instance, exp, _calls(instance))
    problems = [*traj_problems, *inv_problems]
    return _result(
        problems, f"handoff aligned ({', '.join(names) or 'no tools'})"
    )


def voice_checks(instance: dict, **kwargs: Any) -> dict:
    handoff = handoff_alignment(instance, **kwargs)
    spoken = spoken_compliance(instance, **kwargs)
    problems: list[str] = []
    if handoff["score"] < 1.0:
        problems.append(handoff["explanation"])
    if spoken["score"] < 1.0:
        problems.append(spoken["explanation"])
    return _result(
        problems, f"{handoff['explanation']}; {spoken['explanation']}"
    )


evaluate = voice_checks


def delivery(instance: dict, **kwargs: Any) -> dict:
    del kwargs
    want = (_expect(instance) or {}).get("delivery") or {}
    if not want:
        return {"score": 1.0, "explanation": "no delivery expected"}
    events = _ordered(instance)
    problems: list[str] = []
    if kickoff_want := want.get("kickoff"):
        tool = kickoff_want.get("tool", "start_thread")
        said = _kickoff(events, tool)
        if said is None:
            problems.append(f"never called {tool}, so there was no kickoff")
        else:
            problems += [
                f"kickoff: {p}" for p in _speech_problems(said, kickoff_want)
            ]
    for turn, turn_want in (want.get("turns") or {}).items():
        said = " ".join(
            text
            for t, kind, text in events
            if kind == "text" and _in_turns(t, str(turn))
        )
        problems += [
            f"turn {turn}: {p}" for p in _speech_problems(said, turn_want)
        ]
    return _result(problems, "delivery as expected")


def tool_trajectory(instance: dict, **kwargs: Any) -> dict:
    del kwargs
    exp, calls = _expect(instance), _calls(instance)
    if exp is None:
        return _missing(instance)
    problems, names = _trajectory_problems(exp, calls)
    return _result(problems, f"called {names}")


def contract_invariants(instance: dict, **kwargs: Any) -> dict:
    del kwargs
    exp, calls = _expect(instance), _calls(instance)
    if exp is None:
        return _missing(instance)
    problems = _invariant_problems(instance, exp, calls)
    return _result(problems, "all invariants held")


def thread_outcome(instance: dict, **kwargs: Any) -> dict:
    del kwargs
    exp = _expect(instance)
    if exp is None:
        return _missing(instance)
    want = dict(exp.get("outcome") or {})
    if not want:
        return {"score": 1.0, "explanation": "no outcome expected"}
    db = _thread_db(instance)
    if db is None:
        return handoff_alignment(instance)
    key = "thread_id" if "thread_id" in want else "project"
    target = str(want.pop(key))
    threads = [
        t
        for t in _threads_cached(db)
        if (
            t.get("thread_id") == target
            if key == "thread_id"
            else _project_matches(str(t.get("project") or ""), target)
        )
    ]
    if needle := str(want.pop("goal_contains", "")).lower():
        threads = [
            t for t in threads if needle in str(t.get("goal") or "").lower()
        ]
    if not threads:
        return {"score": 0.0, "explanation": f"no thread with {key}={target}"}
    thread = max(threads, key=lambda t: t.get("created_at") or 0)
    problems = (
        [f"{len(threads)} threads on {target}"] if len(threads) > 1 else []
    )
    states = want.pop("state", None)
    if states and thread["state"] not in (
        [states] if isinstance(states, str) else states
    ):
        problems.append(f"state {thread['state']} (want {states})")
    kinds = want.pop("instructions", None)
    actual_kinds = [i["kind"] for i in thread.get("instructions") or []]
    if kinds and actual_kinds != kinds:
        problems.append(f"instructions {actual_kinds} (want {kinds})")
    if want.pop("files_changed", None) and not thread.get("files_changed"):
        problems.append("no files changed")
    problems += [
        f"{k} {thread.get(k)!r} (want {v!r})"
        for k, v in want.items()
        if thread.get(k) != v
    ]
    summary = f"{thread['thread_id']} {thread['state']} on {thread.get('harness')}, instructions {actual_kinds}"
    return _result(problems, summary)


def harness_context(instance: dict, **kwargs: Any) -> dict:
    del kwargs
    exp = _expect(instance)
    if exp is None:
        return _missing(instance)
    want = exp.get("harness_prompt") or {}
    if not want:
        return {"score": 1.0, "explanation": "no harness prompt expected"}
    calls = _harness_calls(instance)
    if calls:
        prompts = "\n".join(c.get("prompt") or "" for c in calls).lower()
    elif os.environ.get("EVAL_RUN_DIR"):
        prompts = ""
    else:
        prompts = "\n".join(
            json.dumps(c["args"])
            for c in _calls(instance)
            if c["name"] in {"start_thread", "message_thread", "share_context"}
        ).lower()
    if not prompts:
        return {
            "score": 0.0,
            "explanation": "the coding agent was never started",
        }
    problems = [
        f"prompt lacks '{p}'"
        for p in want.get("contains", [])
        if p.lower() not in prompts
    ]
    problems += [
        f"prompt has '{p}'"
        for p in want.get("excludes", [])
        if p.lower() in prompts
    ]
    return _result(problems, "the coding agent got the context it needed")


def _trajectory_problems(
    exp: dict[str, Any], calls: list[dict[str, Any]]
) -> tuple[list[str], list[str]]:
    tools = exp.get("tools") or {}
    required = tools.get("required") or tools.get("require") or []
    forbidden = tools.get("forbidden") or tools.get("forbid") or []
    order_spec = tools.get("order") or required
    names = [c["name"] for c in calls]
    problems = [f"missing {t}" for t in required if t not in names]
    problems += [f"called forbidden {t}" for t in forbidden if t in names]
    order = [t for t in order_spec if t in names]
    if [names.index(t) for t in order] != sorted(names.index(t) for t in order):
        problems.append(f"expected order {order}")
    for tool, want in (exp.get("args") or {}).items():
        if not any(
            c["name"] == tool and _args_match(c["args"], want, tool=tool)
            for c in calls
        ):
            problems.append(f"no {tool} call with {want}")
    for tool, want in (exp.get("args_all") or {}).items():
        problems += [
            f"a {tool} call without {want}"
            for c in calls
            if c["name"] == tool and not _args_match(c["args"], want, tool=tool)
        ]
    for tool, want in (exp.get("args_contain") or {}).items():
        if not any(
            c["name"] == tool and _args_contain(c["args"], want) for c in calls
        ):
            problems.append(f"no {tool} call whose arguments contain {want}")
    for tool, phrases in (exp.get("args_exclude") or {}).items():
        said = " ".join(
            json.dumps(c["args"]).lower() for c in calls if c["name"] == tool
        )
        if isinstance(phrases, dict):
            phrase_list = [w for v in phrases.values() for w in _words(v)]
        else:
            phrase_list = _words(phrases)
        problems += [
            f"{tool} arguments restate '{p}'"
            for p in phrase_list
            if p.lower() in said
        ]
    return problems, names


def _invariant_problems(
    instance: dict, exp: dict[str, Any], calls: list[dict[str, Any]]
) -> list[str]:
    consent = set(exp.get("consent_turns") or [])
    tools = exp.get("tools") or {}
    required = tools.get("required") or tools.get("require") or []
    limits = {
        "start_thread": exp.get(
            "dispatches", 1 if "start_thread" in required else 0
        ),
        **(exp.get("max_calls") or {}),
    }
    known = _known_thread_ids(instance, exp, calls)
    problems: list[str] = []
    for c in calls:
        if c["name"] in _CONSENT_TOOLS and c["turn"] not in consent:
            problems.append(f"{c['name']} without consent in turn {c['turn']}")
        thread_id = c["args"].get("thread_id")
        if (
            thread_id
            and thread_id.strip().lower() not in _CURRENT_THREAD
            and thread_id not in known
        ):
            problems.append(f"unknown thread_id {thread_id}")
    for tool, most in limits.items():
        count = sum(c["name"] == tool for c in calls)
        if count > most:
            problems.append(f"{count} {tool} calls (at most {most})")
    for tool, least in (exp.get("min_calls") or {}).items():
        count = sum(c["name"] == tool for c in calls)
        if count < least:
            problems.append(f"{count} {tool} calls (at least {least})")
    reads = [
        (c["turn"], c["name"], json.dumps(c["args"], sort_keys=True))
        for c in calls
    ]
    repeats = {r for r in reads if r[1] in _STATUS_TOOLS and reads.count(r) > 1}
    problems += [
        f"polled {name} in turn {turn}" for turn, name, _ in sorted(repeats)
    ]
    return problems


# Matches spoken IDs such as "thread-3e2bdd8b" or "thread nine oh one".
_ID_TOKEN = (
    r"(?:zero|oh|one|two|three|four|five|six|seven|eight|nine|ten|twenty|thirty|forty|"
    r"fifty|sixty|seventy|eighty|ninety|hundred|alpha|bravo|charlie|delta|echo|foxtrot|[a-f]|\d+)"
)
_SPOKEN_THREAD_ID = re.compile(
    rf"\bthread[- ]?(?:(?=[0-9a-f]*\d)[0-9a-f]{{3,}}\b|(?:{_ID_TOKEN}[\s-]+){{2,}})"
)
_TRACE_THREAD_ID = re.compile(r"\bthread-[0-9a-zA-Z_-]{3,}\b")


def _turns(instance: dict) -> list[dict]:
    return [
        t
        for t in (instance.get("agent_data") or {}).get("turns") or []
        if isinstance(t, dict)
    ]


def _parts(instance: dict):
    for i, turn in enumerate(_turns(instance)):
        for event in turn.get("events") or []:
            content = event.get("content") or {}
            for part in content.get("parts") or []:
                if isinstance(part, dict):
                    yield turn.get("turn_index", i), content.get("role"), part


def _calls(instance: dict) -> list[dict[str, Any]]:
    # Thread tools accept 'current' (the thread on screen); grade the thread the call reached.
    reached = {
        fr.get("id"): result["thread_id"]
        for fr in _responses(instance)
        if isinstance(result := (fr.get("response") or {}).get("result"), dict)
        and result.get("thread_id")
    }
    calls = []
    for turn, _, part in _parts(instance):
        fc = part.get("function_call")
        if not (isinstance(fc, dict) and fc.get("name")):
            continue
        args = dict(fc.get("args") or {})
        if (
            str(args.get("thread_id", "-")).strip().lower() in _CURRENT_THREAD
            and fc.get("id") in reached
        ):
            args["thread_id"] = reached[fc["id"]]
        calls.append({"turn": turn, "name": fc["name"], "args": args})
    return calls


def _responses(instance: dict) -> list[dict[str, Any]]:
    return [
        fr
        for _, _, part in _parts(instance)
        if isinstance(fr := part.get("function_response"), dict)
    ]


def _speech(instance: dict) -> list[str]:
    texts = [
        part["text"]
        for _, role, part in _parts(instance)
        if role == "model" and isinstance(part.get("text"), str)
    ]
    if not texts:
        for cand in instance.get("responses") or []:
            resp = (cand or {}).get("response") or {}
            for part in resp.get("parts") or []:
                if isinstance(part, dict) and isinstance(part.get("text"), str):
                    texts.append(part["text"])
    return texts


def _ordered(instance: dict) -> list[tuple[int, str, Any]]:
    events: list[tuple[int, str, Any]] = []
    for turn, role, part in _parts(instance):
        if role == "model" and isinstance(part.get("text"), str):
            events.append((turn, "text", part["text"]))
        elif isinstance(fc := part.get("function_call"), dict):
            events.append((turn, "call", fc.get("name")))
    return events


def _in_turns(turn: int, spec: str) -> bool:
    return spec == "*" or str(turn) in {p.strip() for p in spec.split(",")}


def _kickoff(events: list[tuple[int, str, Any]], tool: str) -> str | None:
    """First utterance in the turn that first calls `tool` (before any fast result in the same turn)."""
    turn = next((t for t, k, v in events if k == "call" and v == tool), None)
    if turn is None:
        return None
    return next((v for t, k, v in events if t == turn and k == "text"), "")


def _speech_problems(said: str, want: dict[str, Any]) -> list[str]:
    lower = said.lower()
    problems: list[str] = []
    words = len(said.split())
    if (most := want.get("max_words")) is not None and words > most:
        problems.append(f"{words} words (at most {most}): {said[:120]!r}")
    if (most := want.get("max_sentences")) is not None:
        count = len(_sentences(said))
        if count > most:
            problems.append(
                f"{count} sentences (at most {most}): {said[:160]!r}"
            )
    for group in want.get("mentions") or []:
        options = _words(group)
        if not any(o.lower() in lower for o in options):
            problems.append(f"never said '{' / '.join(options)}'")
    problems += [
        f"said '{w}'" for w in want.get("forbid") or [] if w.lower() in lower
    ]
    for label, spec in (
        ("mirrored", want.get("mirrors")),
        ("named", want.get("names")),
    ):
        if not spec:
            continue
        groups = spec.get("groups") or [[o] for o in spec.get("options") or []]
        hits = [
            _words(g)[0]
            for g in groups
            if any(_says(o, lower) for o in _words(g))
        ]
        least, most = spec.get("min", 0), spec.get("max", len(groups))
        if not least <= len(hits) <= most:
            problems.append(
                f"{label} {len(hits)} of {[_words(g)[0] for g in groups]} "
                f"(want {least} to {most}): {hits}"
            )
    if lead_spec := want.get("leads_with"):
        problems += _lead_problems(lower, lead_spec)
    return problems


def _first_said(lower: str, groups: list[Any]) -> int | None:
    found = [
        m.start()
        for g in groups
        for o in _words(g)
        if (m := re.search(rf"(?<!\w){re.escape(o.lower())}(?!\w)", lower))
    ]
    return min(found) if found else None


def _lead_problems(lower: str, spec: dict[str, Any]) -> list[str]:
    lead = _first_said(lower, spec.get("lead") or [])
    other = _first_said(lower, spec.get("others") or [])
    names = [_words(g)[0] for g in spec.get("lead") or []]
    if lead is None:
        return [f"never led with any of {names}"]
    if other is not None and other < lead:
        return [f"said {lower[other : other + 30]!r} before any of {names}"]
    return []


_ABBREVIATIONS = re.compile(
    r"\b(?:a\.m|p\.m|e\.g|i\.e|vs|mr|mrs|ms|dr|st)\.", re.I
)


def _says(phrase: str, lower: str) -> bool:
    return bool(re.search(rf"(?<!\w){re.escape(phrase.lower())}(?!\w)", lower))


def _sentences(text: str) -> list[str]:
    plain = _ABBREVIATIONS.sub(lambda m: m.group(0).replace(".", ""), text)
    return [s for s in re.split(r"(?<=[.!?])\s+", plain.strip()) if s.strip()]


def _known_thread_ids(
    instance: dict, exp: dict[str, Any], calls: list[dict[str, Any]]
) -> set[str]:
    known: set[str] = set()
    if (db := _thread_db(instance)) is not None:
        with contextlib.suppress(
            OSError,
            sqlite3.Error,
            KeyError,
            json.JSONDecodeError,
        ):
            known.update(
                t["thread_id"]
                for t in _threads_cached(db)
                if t.get("thread_id")
            )
    for fr in _responses(instance):
        known.update(_TRACE_THREAD_ID.findall(json.dumps(fr)))
    for c in calls:
        if c["name"] == "start_thread":
            known.add(_thread_id_for(c["args"].get("project")))
    case_id = str(instance.get("eval_case_id") or "")
    with contextlib.suppress(OSError, KeyError, ValueError):
        names = (
            list(exp["history"])
            if "history" in exp
            else case_history_names(case_id)
        )
        for t in load_history(names).threads:
            if tid := t.get("thread_id"):
                known.add(str(tid))
    for want in (exp.get("args") or {}).values():
        if isinstance(want, dict):
            for k in (
                "thread_id",
                "from_thread_id",
                "to_target",
            ):
                if k in want:
                    known.update(_words(want[k]))
    if isinstance(outcome := exp.get("outcome"), dict):
        if tid := outcome.get("thread_id"):
            known.add(str(tid))
        if project := outcome.get("project"):
            known.add(_thread_id_for(project))
    return known


_DEFAULTS: dict[str, dict[str, Any]] = {
    "start_thread": {"project": ""},
    "look": {"query": "", "window": "today"},
}
_NEW_SCRATCH = {"", "scratch", "workspace", "general"}


def _project_key(value: Any) -> str:
    # `code/<repo>` and `projects/<name>` compare by bare name because a bare name resolves to either.
    raw = str("" if value is None else value).strip().lower()
    raw = raw.removeprefix("~/").strip("/")
    if raw in _NEW_SCRATCH:
        return "scratch"
    head, _, rest = raw.partition("/")
    return rest if head in ("code", "projects") and rest else raw


def _project_matches(actual: Any, want: Any) -> bool:
    got, wanted = _project_key(actual), _project_key(want)
    return (
        got == wanted
        or (wanted == "scratch" and got.startswith("scratch/"))
        or ("*" in wanted and fnmatch.fnmatchcase(got, wanted))
    )


def _thread_id_for(project: Any) -> str:
    key = _project_key(project)
    return f"thread-{'general' if key == 'scratch' else key.rsplit('/', 1)[-1]}"


def _args_match(args: dict, want: dict, *, tool: str = "") -> bool:
    defaults = _DEFAULTS.get(tool, {})
    if tool == "start_thread":
        return all(
            _project_matches(args.get(k, ""), want[k])
            if k == "project"
            else _norm(args.get(k, defaults.get(k)))
            == _norm(want.get(k, defaults.get(k)))
            for k in want
        )
    return all(
        _norm(args.get(k, defaults.get(k))) == _norm(v) for k, v in want.items()
    )


def _args_contain(args: dict, want: dict) -> bool:
    return all(
        all(w.lower() in json.dumps(args.get(k, "")).lower() for w in _words(v))
        for k, v in want.items()
    )


def _norm(value: Any) -> Any:
    if isinstance(value, list | tuple):
        return sorted(str(v).lower() for v in value)
    return str("" if value is None else value).lower()


def _words(value: Any) -> list[str]:
    return (
        [str(v) for v in value]
        if isinstance(value, list | tuple)
        else [str(value)]
    )


def _expect(instance: dict) -> dict | None:
    inline = instance.get("expectations")
    looked_up = expectations_for(str(instance.get("eval_case_id") or ""))
    if isinstance(inline, dict) and isinstance(looked_up, dict):
        return {**looked_up, **inline}
    if isinstance(inline, dict):
        return inline
    return looked_up


def _case_dir(instance: dict) -> Path | None:
    run_dir, case_id = (
        os.environ.get("EVAL_RUN_DIR", ""),
        str(instance.get("eval_case_id") or ""),
    )
    if not run_dir or not case_id:
        return None
    path = Path(run_dir) / case_id
    return path if path.is_dir() else None


def _harness_calls(instance: dict) -> list[dict]:
    run_dir = _case_dir(instance) or os.environ.get("EVAL_RUN_DIR", "")
    if not run_dir:
        return []
    calls: list[dict] = []
    for path in sorted(
        glob.glob(f"{run_dir}/server-*/agent/state/calls.jsonl")
    ):
        with open(path, encoding="utf-8") as f:
            calls += [json.loads(line) for line in f if line.strip()]
    return calls


def _thread_db(instance: dict) -> str | None:
    if (case_dir := _case_dir(instance)) is not None:
        db = case_dir / "threads.db"
        return str(db) if db.is_file() else None
    url = os.environ.get("THREAD_DB_URL", "")
    return url.split(":///", 1)[1] if url.startswith("sqlite") else None


@cache
def _threads_cached(db_path: str) -> tuple[dict, ...]:
    with sqlite3.connect(f"file:{db_path}?mode=ro", uri=True) as conn:
        rows = conn.execute("SELECT data FROM threads").fetchall()
    return tuple(
        json.loads(data) if isinstance(data, str) else data for (data,) in rows
    )


def _result(problems: list[str], ok: str) -> dict:
    if problems:
        return {"score": 0.0, "explanation": "; ".join(problems)}
    return {"score": 1.0, "explanation": ok}


def _missing(instance: dict) -> dict:
    return {
        "score": 0.0,
        "explanation": f"no expectations for '{instance.get('eval_case_id')}' in {EXPECTATIONS}",
    }
