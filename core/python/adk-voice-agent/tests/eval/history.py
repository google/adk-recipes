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

"""Loader for `voice/history.yaml` fixtures and per-case expectations in `datasets/*.json`."""

from __future__ import annotations

import json
import os
import re
import time
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path
from typing import Any

import yaml

EVAL_DIR = Path(__file__).resolve().parent
FIXTURES = EVAL_DIR / "voice" / "history.yaml"
DATASETS = EVAL_DIR / "datasets"
EXPECTATIONS = Path(os.getenv("VOICE_EVAL_EXPECTATIONS") or DATASETS)
DEFAULT_HISTORY = ("alex",)
HOLDOUT_SUFFIX = "__ho"

_UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}


def seconds_ago(spec: str | float | None) -> float:
    if spec is None:
        return time.time()
    if isinstance(spec, int | float):
        return time.time() - float(spec)
    match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([smhd])\s*", str(spec))
    if not match:
        raise ValueError(
            f"Not a relative time: {spec!r} (use e.g. 90s, 2m, 5h, 3d)"
        )
    return time.time() - float(match[1]) * _UNITS[match[2]]


@dataclass
class History:
    threads: list[dict[str, Any]] = field(default_factory=list)
    sessions: list[dict[str, Any]] = field(default_factory=list)
    profile: dict[str, Any] = field(default_factory=dict)
    memories: list[dict[str, Any]] = field(default_factory=list)
    disconnected: list[str] = field(default_factory=list)
    focus: dict[str, str] = field(default_factory=dict)


def load(names: list[str] | tuple[str, ...]) -> History:
    fixtures = yaml.safe_load(FIXTURES.read_text()) or {}
    history = History()
    for name in names:
        if name not in fixtures:
            raise KeyError(f"No history fixture '{name}' in {FIXTURES}")
        part = fixtures[name] or {}
        history.threads += part.get("threads") or []
        history.sessions += part.get("sessions") or []
        history.profile.update(part.get("profile") or {})
        history.memories += part.get("memories") or []
        history.disconnected += part.get("disconnected") or []
        if part.get("focus"):
            history.focus = dict(part["focus"])
    return history


def case_history_names(case_id: str) -> list[str]:
    expectations = expectations_for(case_id) or {}
    return list(expectations.get("history", DEFAULT_HISTORY))


def expectations_for(case_id: str) -> dict[str, Any] | None:
    entries = _entries(str(EXPECTATIONS))
    return entries.get(case_id) or entries.get(
        case_id.removesuffix(HOLDOUT_SUFFIX)
    )


@cache
def _entries(path: str) -> dict[str, Any]:
    target = Path(path)
    if target.is_dir():
        merged: dict[str, Any] = {}
        for dataset in sorted(target.glob("*.json")):
            data = json.loads(dataset.read_text(encoding="utf-8"))
            for case in data.get("eval_cases") or []:
                cid = case.get("eval_case_id")
                exp = case.get("expectations")
                if cid and isinstance(exp, dict):
                    entry = dict(exp)
                    if "rubrics" in case and "rubrics" not in entry:
                        entry["rubrics"] = case["rubrics"]
                    merged.setdefault(cid, entry)
        return merged
    if target.suffix == ".json" and target.is_file():
        data = json.loads(target.read_text(encoding="utf-8"))
        return {
            c["eval_case_id"]: dict(c["expectations"])
            for c in data.get("eval_cases") or []
            if c.get("eval_case_id") and isinstance(c.get("expectations"), dict)
        }
    if target.is_file():
        return yaml.safe_load(target.read_text(encoding="utf-8")) or {}
    return {}


def current() -> History:
    raw = os.environ.get("EVAL_HISTORY")
    names = DEFAULT_HISTORY if raw is None else [n for n in raw.split(",") if n]
    return load(names)
