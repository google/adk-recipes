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

"""The before_tool_callback gate: allow / deny / ask + resume + grant recording."""

from __future__ import annotations

import pytest

from horizon.environment_context import set_active_environment
from horizon.guardrails.permission_guard import permission_guard
from horizon.guardrails.permission_rules import (
    PERMISSION_GRANTS_STATE_KEY,
    PERMISSION_OVERLAY_FILENAME,
    read_session_grants,
)

pytestmark = pytest.mark.asyncio


class _Tool:
    def __init__(self, name):
        self.name = name


class _FakeState:
    """ADK State stand-in: .get / __setitem__ but NOT a dict subclass (prod shape)."""

    def __init__(self):
        self._d: dict = {}

    def get(self, key, default=None):
        return self._d.get(key, default)

    def __getitem__(self, key):
        return self._d[key]

    def __setitem__(self, key, value):
        self._d[key] = value


class _FakeSandboxEnv:
    """Non-local env whose .lha overlay lives behind the async read/write interface."""

    def __init__(self, working_dir):
        self.working_dir = working_dir
        self._files: dict = {}

    async def read_file(self, path):
        key = str(path)
        if key not in self._files:
            raise FileNotFoundError(key)
        return self._files[key].encode("utf-8")

    async def write_file(self, path, content):
        self._files[str(path)] = (
            content if isinstance(content, str) else content.decode("utf-8")
        )


class _Actions:
    def __init__(self):
        self.skip_summarization = False


class _Ctx:
    """Minimal ToolContext stand-in."""

    def __init__(self, tool_confirmation=None):
        self.state: dict = {}
        self.actions = _Actions()
        self.tool_confirmation = tool_confirmation
        self.function_call_id = "fc-1"
        self.agent_name = "root"
        self._requested = None

    def request_confirmation(self, *, hint=None, payload=None):
        self._requested = {"hint": hint, "payload": payload}


class _TC:
    def __init__(self, confirmed, payload):
        self.confirmed = confirmed
        self.payload = payload


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    from horizon.environment import LocalEnvironment

    env = LocalEnvironment(working_dir=tmp_path)
    monkeypatch.setattr(env, "_auto_created", False, raising=False)
    env._working_dir = (
        tmp_path  # ensure working_dir is set without initialize()
    )
    set_active_environment(env)
    yield tmp_path


async def test_read_only_tool_allowed():
    ctx = _Ctx()
    assert (
        await permission_guard(
            tool=_Tool("read"), args={"path": "x"}, tool_context=ctx
        )
        is None
    )


async def test_web_research_allowed():
    ctx = _Ctx()
    assert (
        await permission_guard(
            tool=_Tool("web_research"), args={"query": "x"}, tool_context=ctx
        )
        is None
    )


async def test_load_skill_allowed():
    ctx = _Ctx()
    assert (
        await permission_guard(
            tool=_Tool("load_skill"),
            args={"skill_name": "policy"},
            tool_context=ctx,
        )
        is None
    )
    # The merged tool also covers what used to be a separate
    # load_skill_resource tool — same name, an extra arg.
    assert (
        await permission_guard(
            tool=_Tool("load_skill"),
            args={"skill_name": "policy", "resource": "references/x.md"},
            tool_context=ctx,
        )
        is None
    )


async def test_gws_read_with_fd_dup_and_pipe_allowed():
    # The headline sweep shape: export setup, a gws read capturing stderr (2>&1,
    # an fd-dup not a file write), piped to head — must pass without a prompt.
    ctx = _Ctx()
    cmd = (
        "export GWS_CONFIG=/workspace/lha/config/gws && "
        "gws gmail +triage --query is:unread --max 50 --format json 2>&1 | head -c 8000"
    )
    assert (
        await permission_guard(
            tool=_Tool("bash"), args={"command": cmd}, tool_context=ctx
        )
        is None
    )


async def test_gws_delete_in_chain_still_asks():
    # A benign gws read runs; a destructive gws delete in the same chain forces
    # an ask (command_safety H, per-segment).
    ctx = _Ctx()
    cmd = "gws gmail messages list --params '{}' && gws drive files delete --params '{}'"
    result = await permission_guard(
        tool=_Tool("bash"), args={"command": cmd}, tool_context=ctx
    )
    assert result is not None and result.get("confirmation_required") is True


async def test_readonly_diagnostic_with_cd_and_devnull_allowed():
    # The common diagnostic shape: cd into a dir, then read-only inspection with
    # stderr silenced via 2>/dev/null. `cd` is allowlisted and /dev/null is a
    # sink, so the whole chain must pass without a prompt.
    ctx = _Ctx()
    cmd = (
        "cd /workspace/proj 2>/dev/null && find . -iname '*.py' 2>/dev/null | head; "
        "grep -r foo . 2>/dev/null | head; ls -d out/* 2>/dev/null; "
        "tail -40 log.md 2>/dev/null"
    )
    assert (
        await permission_guard(
            tool=_Tool("bash"), args={"command": cmd}, tool_context=ctx
        )
        is None
    )


async def test_cd_then_destructive_still_asks():
    # Allowlisting `cd` must not let a destructive segment ride along — each
    # segment is gated independently.
    ctx = _Ctx()
    result = await permission_guard(
        tool=_Tool("bash"),
        args={"command": "cd /tmp && rm -rf ."},
        tool_context=ctx,
    )
    assert result is not None and result.get("confirmation_required") is True
    assert ctx._requested["payload"]["proposedRule"]["commandPrefix"] == ["rm"]


async def test_self_confirming_tool_skipped():
    ctx = _Ctx()
    assert (
        await permission_guard(
            tool=_Tool("clarify"), args={"question": "?"}, tool_context=ctx
        )
        is None
    )


async def test_sandbox_write_allowed():
    ctx = _Ctx()
    assert (
        await permission_guard(
            tool=_Tool("write"), args={"path": "x"}, tool_context=ctx
        )
        is None
    )


async def test_ask_dispatches_confirmation_first_pass():
    ctx = _Ctx()
    result = await permission_guard(
        tool=_Tool("bash"),
        args={"command": "bq rm -t x.y"},
        tool_context=ctx,
    )
    assert result is not None and result.get("confirmation_required") is True
    assert ctx._requested is not None
    payload = ctx._requested["payload"]
    assert payload["kind"] == "permission"
    assert payload["proposedRule"]["commandPrefix"] == ["bq rm"]
    assert ctx.actions.skip_summarization is True


async def test_resume_once_runs_without_grant():
    ctx = _Ctx(tool_confirmation=_TC(True, {"outcome": "proceed_once"}))
    result = await permission_guard(
        tool=_Tool("bash"), args={"command": "bq rm x"}, tool_context=ctx
    )
    assert result is None
    assert ctx.state.get(PERMISSION_GRANTS_STATE_KEY) in (None, [])


async def test_resume_session_records_grant():
    ctx = _Ctx(tool_confirmation=_TC(True, {"outcome": "proceed_always"}))
    result = await permission_guard(
        tool=_Tool("bash"), args={"command": "bq rm x"}, tool_context=ctx
    )
    assert result is None
    grants = ctx.state[PERMISSION_GRANTS_STATE_KEY]
    assert grants[0]["commandPrefix"] == ["bq rm"]


async def test_resume_without_outcome_defaults_to_session_grant():
    # Gemini Enterprise's generic A2A confirm only sends approve/deny (no rich
    # outcome). A confirmed call with no outcome defaults to a session-wide grant
    # so the user isn't re-prompted for the same tool every turn.
    ctx = _Ctx(tool_confirmation=_TC(True, {}))
    result = await permission_guard(
        tool=_Tool("bash"), args={"command": "bq rm x"}, tool_context=ctx
    )
    assert result is None
    grants = ctx.state[PERMISSION_GRANTS_STATE_KEY]
    assert grants[0]["commandPrefix"] == ["bq rm"]


async def test_resume_none_payload_defaults_to_session_grant():
    ctx = _Ctx(tool_confirmation=_TC(True, None))
    result = await permission_guard(
        tool=_Tool("bash"), args={"command": "bq rm x"}, tool_context=ctx
    )
    assert result is None
    assert ctx.state[PERMISSION_GRANTS_STATE_KEY][0]["commandPrefix"] == [
        "bq rm"
    ]


async def test_resume_always_persists_rule(_env):
    ctx = _Ctx(
        tool_confirmation=_TC(True, {"outcome": "proceed_always_and_save"})
    )
    result = await permission_guard(
        tool=_Tool("bash"), args={"command": "bq rm x"}, tool_context=ctx
    )
    assert result is None
    text = (_env / PERMISSION_OVERLAY_FILENAME).read_text()
    assert "bq rm" in text


async def test_resume_cancel_declines():
    ctx = _Ctx(tool_confirmation=_TC(False, None))
    result = await permission_guard(
        tool=_Tool("bash"), args={"command": "bq rm x"}, tool_context=ctx
    )
    assert result is not None and result.get("permission_denied") is True


async def test_chained_command_with_unsafe_segment_asks():
    ctx = _Ctx()
    result = await permission_guard(
        tool=_Tool("bash"),
        args={"command": "ls && bq rm x"},
        tool_context=ctx,
    )
    assert result is not None and result.get("confirmation_required") is True
    assert ctx._requested["payload"]["proposedRule"]["commandPrefix"] == [
        "bq rm"
    ]


async def test_quoted_interpreter_body_not_shredded():
    # A `sudo python3 -c "<multiline script>"` is a single opaque arg: the prompt
    # (here triggered by the sudo escalation) must offer one clean prefix
    # (`sudo python3` — the escalation is named, never unwrapped), not one fake
    # prefix per source line.
    ctx = _Ctx()
    cmd = (
        'sudo python3 -c "import json\nfor r in ranked:\n    print(r); x | y\n"'
    )
    result = await permission_guard(
        tool=_Tool("bash"), args={"command": cmd}, tool_context=ctx
    )
    assert result is not None and result.get("confirmation_required") is True
    payload = ctx._requested["payload"]
    assert payload["proposedRule"]["commandPrefix"] == ["sudo python3"]
    assert payload["choices"] == [
        "Yes, once",
        "Yes — allow `sudo python3` this session",
        "Always allow `sudo python3`",
        "Decline",
    ]


async def test_chained_duplicate_prefixes_deduped():
    ctx = _Ctx()
    result = await permission_guard(
        tool=_Tool("bash"),
        args={"command": "bq rm a && bq rm b"},
        tool_context=ctx,
    )
    assert result is not None and result.get("confirmation_required") is True
    assert ctx._requested["payload"]["proposedRule"]["commandPrefix"] == [
        "bq rm"
    ]


async def test_unbalanced_quote_still_asks():
    # split_segments under-splits a syntactically broken command, but the
    # shlex-based safety classifier can't parse it and forces an ask — fail-closed.
    ctx = _Ctx()
    result = await permission_guard(
        tool=_Tool("bash"),
        args={"command": 'echo " && bq rm secret'},
        tool_context=ctx,
    )
    assert result is not None and result.get("confirmation_required") is True


async def test_command_substitution_forces_ask():
    ctx = _Ctx()
    result = await permission_guard(
        tool=_Tool("bash"),
        args={"command": "ls $(bq rm x)"},
        tool_context=ctx,
    )
    assert result is not None and result.get("confirmation_required") is True


_BQ_BACKTICK_QUERY = (
    "bq --project_id=p query --use_legacy_sql=false --format=prettyjson '\n"
    "SELECT term FROM `bigquery-public-data.google_books_ngrams_2020.chi_sim_1`\n"
    "LIMIT 10\n'"
)


async def test_bq_query_with_backticks_does_not_prompt():
    # Prod regression: a BigQuery table ref is backtick-quoted inside a single-
    # quoted SQL string. Those backticks expand nothing, but the substitution net
    # matched them literally and prompted on every read-only query.
    ctx = _Ctx()
    result = await permission_guard(
        tool=_Tool("bash"),
        args={"command": _BQ_BACKTICK_QUERY},
        tool_context=ctx,
    )
    assert result is None
    assert ctx._requested is None


async def test_grant_honored_regardless_of_flag_position():
    # Approving `bq query …` must cover the same command with flags hoisted ahead
    # of the subcommand — otherwise the saved rule never matches and re-prompts.
    ctx = _Ctx()
    ctx.state[PERMISSION_GRANTS_STATE_KEY] = [
        {
            "toolName": "bash",
            "decision": "allow",
            "commandPrefix": ["bq query"],
        }
    ]
    result = await permission_guard(
        tool=_Tool("bash"),
        args={"command": _BQ_BACKTICK_QUERY},
        tool_context=ctx,
    )
    assert result is None
    assert ctx._requested is None


async def test_grant_scope_is_not_widened_by_leading_flags():
    # A flag before the subcommand used to collapse the auto-scope prefix to the
    # bare binary (`gcloud`), and a grant skips the command_safety demotion — so
    # approving one delete silently authorized every other `gcloud … delete`.
    save = _Ctx(tool_confirmation=_TC(True, {"outcome": "proceed_always"}))
    assert (
        await permission_guard(
            tool=_Tool("bash"),
            args={
                "command": "gcloud --project=p compute instances delete vm-1"
            },
            tool_context=save,
        )
        is None
    )
    assert save.state[PERMISSION_GRANTS_STATE_KEY][0]["commandPrefix"] == [
        "gcloud compute"
    ]

    later = _Ctx()
    later.state = save.state
    result = await permission_guard(
        tool=_Tool("bash"),
        args={"command": "gcloud --project=p storage rm gs://bucket/obj"},
        tool_context=later,
    )
    assert result is not None and result.get("confirmation_required") is True


async def test_bare_flag_value_is_never_mistaken_for_a_subcommand():
    # `-n default` is a flag and its value. Scoping the grant to `kubectl default`
    # would read as nonsense on the approval card while authorizing every kubectl
    # call in that namespace — so the prefix stays the honest, broad `kubectl`.
    save = _Ctx(tool_confirmation=_TC(True, {"outcome": "proceed_always"}))
    assert (
        await permission_guard(
            tool=_Tool("bash"),
            args={
                "command": "kubectl --context=prod -n default delete pod web-1"
            },
            tool_context=save,
        )
        is None
    )
    assert save.state[PERMISSION_GRANTS_STATE_KEY][0]["commandPrefix"] == [
        "kubectl"
    ]


async def test_flag_value_cannot_impersonate_a_granted_subcommand():
    # Mirror of the above on the matching side: a namespace literally named `get`
    # must not let `kubectl -n get delete …` satisfy a `kubectl get` grant.
    ctx = _Ctx()
    ctx.state[PERMISSION_GRANTS_STATE_KEY] = [
        {
            "toolName": "bash",
            "decision": "allow",
            "commandPrefix": ["kubectl get"],
        }
    ]
    result = await permission_guard(
        tool=_Tool("bash"),
        args={"command": "kubectl -n get delete pod web-1"},
        tool_context=ctx,
    )
    assert result is not None and result.get("confirmation_required") is True


async def test_real_substitution_still_asks_despite_grant():
    # The anti-obfuscation net stays: a grant on the outer binary must not let a
    # gated command ride along in argument position.
    ctx = _Ctx()
    ctx.state[PERMISSION_GRANTS_STATE_KEY] = [
        {"toolName": "bash", "decision": "allow", "commandPrefix": ["ls"]}
    ]
    result = await permission_guard(
        tool=_Tool("bash"),
        args={"command": "ls $(bq rm x)"},
        tool_context=ctx,
    )
    assert result is not None and result.get("confirmation_required") is True


async def test_grant_does_not_cover_sudo_escalation():
    # The flag-tolerant fallback must not unwrap `sudo`: a grant skips the
    # command_safety demotion, so approving `npm install` would otherwise silently
    # approve `sudo npm install` too.
    ctx = _Ctx()
    ctx.state[PERMISSION_GRANTS_STATE_KEY] = [
        {
            "toolName": "bash",
            "decision": "allow",
            "commandPrefix": ["npm install"],
        }
    ]
    result = await permission_guard(
        tool=_Tool("bash"),
        args={"command": "sudo npm install evil-pkg"},
        tool_context=ctx,
    )
    assert result is not None and result.get("confirmation_required") is True


async def test_ansi_c_quoting_cannot_hide_substitution_under_a_grant():
    # `$'\''` is ONE literal quote to bash. Mis-scanning it as a single-quoted run
    # desyncs quote state and hid the `$(…)` that follows — and a grant skips the
    # command_safety demotion that would otherwise still catch it.
    ctx = _Ctx()
    ctx.state[PERMISSION_GRANTS_STATE_KEY] = [
        {
            "toolName": "bash",
            "decision": "allow",
            "commandPrefix": ["bq query"],
        }
    ]
    result = await permission_guard(
        tool=_Tool("bash"),
        args={"command": r"""bq query $'\'' $(cat /etc/passwd)"""},
        tool_context=ctx,
    )
    assert result is not None and result.get("confirmation_required") is True


async def test_flagged_rule_prefix_still_matches_literally():
    # A hand-written overlay rule that names a flag must NOT be flag-tolerantly
    # widened — `git push --force` must not authorize a plain `git push`.
    from horizon.guardrails.permission_rules import _prefix_matches

    assert _prefix_matches("git push --force origin main", "git push --force")
    assert not _prefix_matches("git push origin main", "git push --force")


async def test_ask_payload_includes_choices_for_ge():
    ctx = _Ctx()
    await permission_guard(
        tool=_Tool("bash"),
        args={"command": "bq rm -t x.y"},
        tool_context=ctx,
    )
    payload = ctx._requested["payload"]
    # GE renders these; order must match the outcome mapping (Task 2).
    assert payload["choices"] == [
        "Yes, once",
        "Yes — allow `bq rm` this session",
        "Always allow `bq rm`",
        "Decline",
    ]
    assert "question" in payload  # GE uses this as the prompt text
    assert "outcomes" not in payload  # collapsed to the single choices shape


async def test_resume_ge_choice_label_records_session_grant():
    ctx = _Ctx(
        tool_confirmation=_TC(
            True, {"choice": "Yes — allow `bq rm` this session"}
        )
    )
    result = await permission_guard(
        tool=_Tool("bash"), args={"command": "bq rm x"}, tool_context=ctx
    )
    assert result is None
    assert ctx.state[PERMISSION_GRANTS_STATE_KEY][0]["commandPrefix"] == [
        "bq rm"
    ]


async def test_resume_ge_choice_index_maps_to_outcome():
    # GE may return the index rather than the label.
    ctx = _Ctx(
        tool_confirmation=_TC(True, {"choice": "2"})
    )  # index 2 = "Always allow"
    result = await permission_guard(
        tool=_Tool("bash"), args={"command": "bq rm x"}, tool_context=ctx
    )
    assert (
        result is None
    )  # proceed (and persists — covered by overlay test elsewhere)


async def test_resume_ge_decline_choice_denies():
    ctx = _Ctx(tool_confirmation=_TC(True, {"choice": "Decline"}))
    result = await permission_guard(
        tool=_Tool("bash"), args={"command": "bq rm x"}, tool_context=ctx
    )
    assert result is not None and result.get("permission_denied") is True


async def test_resume_legacy_outcome_key_still_tolerated():
    # Both clients now send {choice}; an explicit {outcome} (older client during a
    # deploy) is still mapped, so in-flight confirmations survive a rollout.
    ctx = _Ctx(tool_confirmation=_TC(True, {"outcome": "proceed_once"}))
    result = await permission_guard(
        tool=_Tool("bash"), args={"command": "bq rm x"}, tool_context=ctx
    )
    assert result is None
    assert ctx.state.get(PERMISSION_GRANTS_STATE_KEY) in (None, [])


async def test_session_grant_recorded_and_reapplied_on_non_dict_state():
    # Prod regression: ADK session State is NOT a dict subclass, so the old
    # isinstance(state, dict) gate silently dropped the grant and re-prompted
    # every turn. The grant must record on a State-like object and be honored next.
    state = _FakeState()
    ctx = _Ctx(tool_confirmation=_TC(True, {"outcome": "proceed_always"}))
    ctx.state = state
    first = await permission_guard(
        tool=_Tool("bash"), args={"command": "bq rm x"}, tool_context=ctx
    )
    assert first is None
    assert read_session_grants(state)[0]["commandPrefix"] == ["bq rm"]

    follow = _Ctx()  # no confirmation supplied
    follow.state = state
    result = await permission_guard(
        tool=_Tool("bash"), args={"command": "bq rm x"}, tool_context=follow
    )
    assert result is None  # honored without re-prompting
    assert follow._requested is None


async def test_always_allow_persists_across_turns_via_interface(tmp_path):
    # Under the sandbox backend the overlay lives in the sandbox, reached through
    # the env interface — a raw host-fs write would be lost. "Always allow" must survive
    # into a later turn (a fresh tool_context with no confirmation).
    sandbox = _FakeSandboxEnv(tmp_path / "sbx")
    set_active_environment(sandbox)

    save = _Ctx(
        tool_confirmation=_TC(True, {"outcome": "proceed_always_and_save"})
    )
    first = await permission_guard(
        tool=_Tool("routine"), args={"action": "create"}, tool_context=save
    )
    assert first is None

    later = _Ctx()  # different turn, no confirmation
    result = await permission_guard(
        tool=_Tool("routine"), args={"action": "create"}, tool_context=later
    )
    assert result is None
    assert later._requested is None


async def test_find_delete_downgrades_to_ask():
    # find is a safe prefix; command_safety must force an ask.
    from horizon.guardrails.permission_guard import _shell_decision
    from horizon.guardrails.permission_rules import DEFAULT_RULES

    decision, _deny, _prefixes = _shell_decision(
        list(DEFAULT_RULES), "bash", "find / -delete", None
    )
    assert decision == "ask_user"


async def test_plain_find_still_allows():
    from horizon.guardrails.permission_guard import _shell_decision
    from horizon.guardrails.permission_rules import DEFAULT_RULES

    decision, _deny, _ = _shell_decision(
        list(DEFAULT_RULES), "bash", "find . -name '*.py'", None
    )
    assert decision == "allow"


async def test_redirection_alone_no_longer_asks():
    ctx = _Ctx()
    assert (
        await permission_guard(
            tool=_Tool("bash"),
            args={"command": "echo hi > out.txt"},
            tool_context=ctx,
        )
        is None
    )
    assert ctx._requested is None


async def test_grant_overrides_command_safety_ask_next_turn():
    # "approve for session" must stick for a command_safety-flagged op: the
    # source-gated demotion skips command_safety when the allow is a grant.
    state = _FakeState()
    ctx = _Ctx(tool_confirmation=_TC(True, {"outcome": "proceed_always"}))
    ctx.state = state
    assert (
        await permission_guard(
            tool=_Tool("bash"),
            args={"command": "bq rm x"},
            tool_context=ctx,
        )
        is None
    )

    follow = _Ctx()
    follow.state = state
    result = await permission_guard(
        tool=_Tool("bash"), args={"command": "bq rm y"}, tool_context=follow
    )
    assert result is None  # grant covers it — no re-prompt
    assert follow._requested is None


async def test_yolo_auto_approves_ask():
    # YOLO mode with approval_mode=yolo auto-approves an ask_user decision without HITL.
    ctx = _Ctx()
    ctx.state["approval_mode"] = "yolo"
    result = await permission_guard(
        tool=_Tool("bash"),
        args={"command": "sudo apt-get update"},
        tool_context=ctx,
    )
    assert result is None
    assert ctx._requested is None


async def test_yolo_does_not_bypass_deny():
    # YOLO only short-circuits Layer D ask→allow; it must NOT turn a deny into allow.
    # A rule explicitly denying a tool still blocks, even with approval_mode=yolo.
    ctx = _Ctx()
    ctx.state["approval_mode"] = "yolo"
    ctx.state[PERMISSION_GRANTS_STATE_KEY] = [
        {
            "toolName": "bash",
            "commandPrefix": ["rm -rf"],
            "decision": "deny",
        }
    ]
    result = await permission_guard(
        tool=_Tool("bash"),
        args={"command": "rm -rf /tmp/x"},
        tool_context=ctx,
    )
    assert result is not None
    assert result.get("permission_denied") is True


async def test_yolo_with_headless_still_denies_non_shell():
    # Headless + YOLO: shell commands are already allowed by headless; non-shell
    # ask_user becomes deny (headless fail-closed, same behavior with or without YOLO).
    from horizon.guardrails.permission_guard import set_headless_mode

    ctx = _Ctx()
    ctx.state["approval_mode"] = "yolo"
    tok = set_headless_mode(True)
    try:
        result = await permission_guard(
            tool=_Tool("routine"), args={"action": "create"}, tool_context=ctx
        )
        assert result is not None
        assert result.get("headless_denied") is True
    finally:
        set_headless_mode(False)
        from horizon.guardrails.permission_guard import reset_headless_mode

        reset_headless_mode(tok)


async def test_default_approval_mode_still_asks():
    # approval_mode=default (or absent) preserves the existing behavior: an ask_user
    # decision triggers HITL confirmation.
    ctx = _Ctx()
    ctx.state["approval_mode"] = "default"
    result = await permission_guard(
        tool=_Tool("bash"),
        args={"command": "sudo apt-get update"},
        tool_context=ctx,
    )
    assert result is not None
    assert result.get("confirmation_required") is True


async def test_subagent_blocking_call_exempt_from_spawn_prompt():
    # A blocking call's child self-gates each risky op via resurfacing, so
    # the coarse spawn-time prompt is dropped.
    ctx = _Ctx()
    assert (
        await permission_guard(
            tool=_Tool("subagent"), args={"goal": "x"}, tool_context=ctx
        )
        is None
    )
    assert ctx._requested is None


async def test_subagent_background_call_exempt_from_spawn_prompt():
    # Spawning grants the child nothing (its headless guard hard-denies risky ops
    # and surfaces them in the reported result), so the spawn prompt is friction.
    ctx = _Ctx()
    assert (
        await permission_guard(
            tool=_Tool("subagent"),
            args={"background": True, "goal": "x"},
            tool_context=ctx,
        )
        is None
    )
    assert ctx._requested is None
