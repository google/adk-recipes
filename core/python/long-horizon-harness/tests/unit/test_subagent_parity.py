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

"""Parity net for the delegate+agent -> subagent merge.

Derives the expected parameter set by introspecting the real OLD callables
rather than hand-listing, so a parameter neither reviewer nor author thought
to name still shows up as a failure instead of silently vanishing.
"""

from __future__ import annotations

import inspect

import pytest

import horizon.subagents.subagent as subagent_mod
from horizon.subagents import delegate as old_delegate_mod
from horizon.subagents import spawn as old_spawn_mod
from horizon.subagents.subagent import _SUPPORTED_ACTIONS, subagent

pytestmark = pytest.mark.asyncio

# `spawn` is not an action on the merged tool: background=True replaces it.
OLD_AGENT_ACTIONS = {"status", "result", "wait", "cancel", "list"}

# timeout_s can't use a single literal default: delegate (blocking) resolves
# to 120.0, agent (background/lifecycle) resolves to 300.0/120.0 depending on
# action. subagent's own default is None so each path picks its own effective
# default; see test_blocking_timeout_effective_default_is_120s and
# test_background_timeout_effective_default_is_300s below for the behavioral
# assertion the raw-default check below can't express.
SPECIAL_CASE_PARAMS = {"timeout_s"}

# Parameters intentionally not carried over, with the reason. Empty today.
JUSTIFIED_DROPS: dict[str, str] = {}


def test_every_old_param_survives():
    new = inspect.signature(subagent).parameters
    for old in (old_delegate_mod.delegate, old_spawn_mod.agent):
        for name, param in inspect.signature(old).parameters.items():
            if (
                name in {"tool_context", "action"}
                or name in JUSTIFIED_DROPS
                or name in SPECIAL_CASE_PARAMS
            ):
                continue
            assert name in new, f"{old.__name__}.{name} dropped"
            if param.default is not inspect.Parameter.empty:
                assert new[name].default == param.default, (
                    f"{old.__name__}.{name} default changed: "
                    f"{param.default!r} -> {new[name].default!r}"
                )


async def test_blocking_timeout_effective_default_is_120s(monkeypatch):
    # A background=False call with no timeout_s must reach delegate() with
    # 120.0 (delegate's own blocking default), not None (delegate requires
    # a float and has no internal None-fallback).
    captured = {}

    async def fake_delegate(**kwargs):
        captured.update(kwargs)
        return {"success": True}

    monkeypatch.setattr(subagent_mod, "delegate", fake_delegate)
    await subagent(goal="x")
    assert captured["timeout_s"] == 120.0


async def test_background_timeout_effective_default_is_300s(monkeypatch):
    # A background=True call with no timeout_s must forward None to agent(),
    # so spawn.py's own _DEFAULT_TIMEOUT_S (300.0) applies. Forwarding 120.0
    # here (delegate's default) was the actual regression this test guards:
    # background children silently ran with 40% of their prior budget.
    captured = {}

    async def fake_agent(**kwargs):
        captured.update(kwargs)
        return {"success": True}

    monkeypatch.setattr(subagent_mod, "agent", fake_agent)
    await subagent(goal="x", background=True)
    assert captured["timeout_s"] is None


def test_every_old_action_is_reachable():
    assert OLD_AGENT_ACTIONS <= set(_SUPPORTED_ACTIONS)


def test_profile_param_survives_with_the_default():
    # The deny-by-default child archetype (child_guard.py enforces it) is the
    # highest-value parameter this merge must not lose.
    new = inspect.signature(subagent).parameters
    assert "profile" in new
    assert new["profile"].default is None


def test_task_ids_param_survives():
    # The fleet `wait` primitive: spawn N, wait for the next one, repeat.
    new = inspect.signature(subagent).parameters
    assert "task_ids" in new
    assert new["task_ids"].default is None


async def test_subagent_module_importable_and_callable():
    # Smoke check that the merged tool is a real async callable before the
    # behavioral tests below exercise it.
    assert inspect.iscoroutinefunction(subagent)
