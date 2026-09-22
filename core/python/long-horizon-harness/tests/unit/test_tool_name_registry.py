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

"""Tool-name registry safety net.

Positive tests, not a ban-list regex scan (rejected: too many false
positives on real code that merely shares a word with a tool name). Three
invariants: every fail-closed tool-name set is a subset of the live
registry, the registry equals the live tool set, and no dead legacy token
survives outside a small, explicit exception list of permanent survivors.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from horizon.tools import names

pytestmark = pytest.mark.asyncio

_HORIZON_ROOT = Path(__file__).resolve().parents[2] / "horizon"
_DEFAULT_POLICIES_JSONL = (
    _HORIZON_ROOT / "guardrails" / "default_policies.jsonl"
)


def _fail_closed_sets() -> dict[str, set[str]]:
    from horizon.a2a import executor as a2a_executor
    from horizon.context import artifact_url_redaction as aur
    from horizon.context import summarizer as summ
    from horizon.context import tool_output_pruning as top
    from horizon.conversation import system_prompt as sp
    from horizon.guardrails import permission_rules as pr
    from horizon.guardrails.permission_guard import (
        READ_ONLY_TOOLS,
        SELF_CONFIRMING_TOOLS,
        SUBAGENT_TOOLS,
    )
    from horizon.memory import flush_fork as ff
    from horizon.memory import review_fork as rf
    from horizon.memory import skill_telemetry as st
    from horizon.subagents import delegate_builder as db
    from horizon.subagents import descriptions as desc
    from horizon.subagents import profiles as prof

    sets: dict[str, set[str]] = {
        "READ_ONLY_TOOLS": set(READ_ONLY_TOOLS),
        "SELF_CONFIRMING_TOOLS": set(SELF_CONFIRMING_TOOLS),
        "SUBAGENT_TOOLS": set(SUBAGENT_TOOLS),
        "_SANDBOX_WRITE_TOOLS": set(pr._SANDBOX_WRITE_TOOLS),
        "_BENIGN_SIDE_EFFECT_TOOLS": set(pr._BENIGN_SIDE_EFFECT_TOOLS),
        "_TOOLS_REQUIRING_NARROWING": set(pr._TOOLS_REQUIRING_NARROWING),
        "_DEFAULT_RULE_DICTS": {
            d["toolName"]
            for d in pr._DEFAULT_RULE_DICTS
            if d["toolName"] != "*"
        },
        "_REVIEW_TOOL_NAMES": set(rf._REVIEW_TOOL_NAMES),
        "_SKILL_MUTATE_TOOL_NAMES": set(rf._SKILL_MUTATE_TOOL_NAMES),
        "_FLUSH_TOOL_NAMES": set(ff._FLUSH_TOOL_NAMES),
        "_VIEW_TOOLS": set(st._VIEW_TOOLS),
        "_MUTATE_TOOLS": set(st._MUTATE_TOOLS),
        "_BLOCKED_TOOL_NAMES": set(db._BLOCKED_TOOL_NAMES),
        "_SUBAGENT_TOOL_NAMES": set(desc._SUBAGENT_TOOL_NAMES),
        "_MEMORY_TOOL_NAMES": set(sp._MEMORY_TOOL_NAMES),
        # _RECALL_PAST_SESSIONS_TOOL_NAMES and _ARTIFACT_TOOL_NAMES were
        # removed in Task 6 (folded unconditionally into STYLE_GUIDANCE);
        # _WEB_RESEARCH_TOOL_NAMES was removed the same way.
        # _SESSION_SEARCH_TOOL_NAMES is gone entirely: session_search folded
        # into memory(action='search'), and its guidance now lives inside
        # MEMORY_GUIDANCE, gated on _MEMORY_TOOL_NAMES above — no separate
        # gate to track. _SKILL_TOOL_NAMES is no longer a dead gate — it
        # points at a real registered tool (see test_system_prompt_gating.py).
        "_SKILL_TOOL_NAMES": set(sp._SKILL_TOOL_NAMES),
        # Cumulative file tracking across compactions (6c).
        "_READ_FILE_TOOLS": set(summ._READ_FILE_TOOLS),
        "_WRITE_FILE_TOOLS": set(summ._WRITE_FILE_TOOLS),
        # Security control: on a missed rename this silently starts letting
        # the model read credentialed 7-day V4 signed URLs (verification
        # review blocker 3).
        "artifact_url_redaction.ARTIFACT": {aur.ARTIFACT},
        # "adk_request_confirmation" is an ADK framework literal, not a
        # horizon tool name, so it's excluded the same way the "*" wildcard
        # and "skill" substring are excluded elsewhere in this function.
        "_FRAMEWORK_TOOL_NAMES": set(a2a_executor._FRAMEWORK_TOOL_NAMES)
        - {"adk_request_confirmation"},
        # "skill" is a substring match (names.LOAD_SKILL), not a tool name
        # itself, so it can't round-trip through names.ALL — exclude it here
        # the same way default_policies.jsonl excludes the "*" wildcard.
        "PROTECTED_TOOL_SUBSTRINGS": set(top.PROTECTED_TOOL_SUBSTRINGS)
        - {"skill"},
    }
    for profile_name, profile in prof.PROFILES.items():
        allowed = profile.allowed_tool_names
        if allowed:
            sets[f"PROFILES[{profile_name}].allowed_tool_names"] = set(allowed)
    # default_policies.jsonl is data, not Python, so its canonical_tool_name
    # values can't reference names.py's constants directly — policies.py's
    # loader aliases them at read time (_alias_canonical_tool_name), by
    # design, so the file itself never needs to change on a rename. Compare
    # the aliased form, matching what production actually resolves.
    sets["default_policies.jsonl"] = {
        names.apply_tool_aliases(json.loads(line)["canonical_tool_name"])
        for line in _DEFAULT_POLICIES_JSONL.read_text().splitlines()
        if line.strip()
    }
    return sets


async def test_no_fail_closed_set_references_a_missing_tool():
    # Task 6 fixed both prompt gates that used to name a dead tool
    # ("session_search", "skill"), so every set is now held to the same
    # standard — no more _KNOWN_DEAD_GATES carve-out.
    stray = {
        label: sorted(members - names.ALL)
        for label, members in _fail_closed_sets().items()
        if members - names.ALL
    }
    assert not stray, stray


async def test_registry_matches_registered_tools():
    from horizon.agent import root_agent

    live = {t.name for t in await root_agent.canonical_tools()}
    assert live == set(names.ALL), {
        "declared_not_registered": sorted(names.ALL - live),
        "registered_not_declared": sorted(live - names.ALL),
    }


async def test_no_dead_tool_name_in_model_facing_prose():
    """Deleting a tool while leaving prose that tells the model to call it is
    this repo's most common defect. Checks the two surfaces a model reads
    that a repo-wide source scan can miss: the LIVE, fully assembled
    ``static_instruction`` string (catches a mention that only renders under
    some condition) and every builtin skill's instructions.
    """
    from horizon.agent import root_agent

    sources: dict[str, str] = {
        "root_agent.static_instruction": root_agent.static_instruction
    }
    for path in _HORIZON_ROOT.glob("builtin_skills/**/SKILL.md"):
        rel = str(path.relative_to(_HORIZON_ROOT.parent))
        sources[rel] = path.read_text(encoding="utf-8")

    offenders: dict[str, list[str]] = {}
    for label, text in sources.items():
        token_snippets = _DEAD_TOOL_PRESENCE_EXCEPTIONS.get(label, {})
        scrubbed = text
        for snippet_list in token_snippets.values():
            for snippet in snippet_list:
                assert snippet in scrubbed, (label, snippet)  # must exist
                scrubbed = scrubbed.replace(snippet, "", 1)
        hits = [tok for tok in _DEAD_TOOL_NAMES if tok in scrubbed]
        if hits:
            offenders[label] = hits
    assert not offenders, offenders


# Unambiguous dead names only. "terminal"/"patch"/"write_file"/"reload" stay
# out: each has a legitimate non-tool meaning (job-type literal,
# unittest.mock, plain English, web page reload) common enough to swamp the
# scan. "delegate" stays out too: the internal delegate() callable behind
# `subagent` is genuinely still named that. "background=True" stays out
# because it's ambiguous, not just noisy: `subagent(background=True)` uses
# the identical spelling for a real, current parameter.
_LEGACY_TOKENS = (
    "read_file",
    "view_file",
    "add_memory",
    "recall_past_sessions",
    "write_todos",
    "repo_overview",
    "set_workspace_window",
    "report_to_maintainers",
    "load_skill_resource",
    "run_skill_script",
    "session_search",
    "old_string",
    "new_string",
    "replace_all",
    "as_media",
)

# Subset of _LEGACY_TOKENS that named a TOOL (not a since-renamed parameter
# like old_string/replace_all/as_media) — the universe test_no_dead_tool_
# name_in_model_facing_prose checks against the LIVE assembled prompt, not
# source text, so a gated/conditional string that never renders (the
# SKILLS_GUIDANCE-never-shipped class of bug) can't hide from it.
#
# "reminder" is added here, not to _LEGACY_TOKENS above: it has the same
# "legitimate non-tool meaning common enough to swamp the scan" problem as
# reload/patch/terminal/write_file (see the comment above _LEGACY_TOKENS) —
# `<system-reminder>` (the volatile prompt tail, horizon/conversation/
# reminders.py) uses the word constantly and legitimately across 16+ files.
# The narrower two-surface scan below, with snippet-level (not file-level)
# exceptions, can tell the two apart; a repo-wide bare-substring scan cannot.
_DEAD_TOOL_NAMES = (
    frozenset(_LEGACY_TOKENS)
    - frozenset({"old_string", "new_string", "replace_all", "as_media"})
) | frozenset({"reminder"})

# Per-token exceptions are the EXACT legitimate snippet, not the bare token:
# stripping only that snippet before scanning means a future, different
# mention of the same dead name (the actual instance-15 shape) still gets
# caught, instead of the whole file going blind to the token. Verified by
# temporarily reintroducing "Use reminder for plain time-based pings." during
# this test's own development — a bare-token exception let it through; this
# snippet-scoped one does not.
_DEAD_TOOL_PRESENCE_EXCEPTIONS: dict[str, dict[str, list[str]]] = {
    "root_agent.static_instruction": {
        "reminder": [
            "<system-reminder>",
            "there is no reminder tool",
        ],
    },
    "horizon/builtin_skills/routines/SKILL.md": {
        "reminder": ["no one-off reminder tool"],
    },
    # An external MCP server's own same-named tool, unrelated to ours (same
    # exception as the repo-wide scan below); both example lines it appears
    # on, so a genuinely new mention elsewhere in the file still gets caught.
    "horizon/builtin_skills/bootstrap-google-tools/SKILL.md": {
        "read_file": [
            'mcp-cli info filesystem read_file")',
            "mcp-cli call filesystem read_file '",
        ],
    },
}

_LEGACY_SCAN_GLOBS = (
    "horizon/**/*.py",
    "horizon/builtin_skills/**/*.md",
    "tests/eval/evalsets/*.json",
    "web/**/*.ts",
    "web/**/*.tsx",
    "docs/**/*.md",
    # Root-level docs live outside docs/**.
    "README.md",
    "AGENTS.md",
)

# Permanent, legitimate survivors — each is one of: (a) an ADK
# BaseEnvironment method (Environment.read_file), never renamed; (b) a
# deliberately-retained internal helper, not the registered tool; (c) a
# permanent TOOL_NAME_ALIASES key in names.py, needed for old persisted
# permission rules; (d) the unrenamed add_memory_tool.py module filename; (e)
# an external MCP server's own same-named tool, unrelated to ours; (f) an
# eval_id/rubric_id label, not graded content; (g) historically-accurate
# prose about a deleted tool.
_LEGACY_TOKEN_EXCEPTIONS: dict[str, frozenset[str]] = {
    "horizon/tools/names.py": frozenset(_LEGACY_TOKENS),
    "horizon/tools/file_ops.py": frozenset({"read_file", "replace_all"}),
    "horizon/tools/__init__.py": frozenset({"read_file"}),
    "horizon/tools/read.py": frozenset({"read_file", "as_media"}),
    "horizon/tools/artifacts.py": frozenset({"read_file"}),
    "horizon/tools/skill_loader.py": frozenset({"read_file"}),
    "horizon/tools/past_sessions.py": frozenset({"recall_past_sessions"}),
    "horizon/guardrails/_overlay.py": frozenset({"read_file"}),
    "horizon/guardrails/permission_rules.py": frozenset({"read_file"}),
    "horizon/environment/sandbox.py": frozenset({"read_file"}),
    "horizon/api/uploads.py": frozenset({"read_file"}),
    "horizon/sandbox/runtime/server.py": frozenset({"read_file"}),
    # recall_past_sessions_entries, category (b), same as past_sessions.py.
    "horizon/memory/add_memory_tool.py": frozenset(
        {"add_memory", "recall_past_sessions", "session_search"}
    ),
    "horizon/memory/_writer.py": frozenset({"add_memory"}),
    "horizon/memory/__init__.py": frozenset({"add_memory"}),
    "horizon/memory/review_fork.py": frozenset({"add_memory"}),
    "horizon/memory/flush_fork.py": frozenset({"add_memory"}),
    "horizon/builtin_skills/bootstrap-google-tools/SKILL.md": frozenset(
        {"read_file"}
    ),
    "docs/extending.md": frozenset({"read_file", "session_search"}),
    "docs/architecture.md": frozenset({"add_memory", "session_search"}),
    "docs/memory.md": frozenset({"add_memory"}),
    "docs/permission-model.md": frozenset({"session_search"}),
    "tests/eval/evalsets/dynamic_delegate_routing.evalset.json": frozenset(
        {"read_file"}
    ),
    "tests/eval/evalsets/memory_recall.evalset.json": frozenset({"add_memory"}),
    "tests/eval/evalsets/skill_curation.evalset.json": frozenset(
        {"add_memory"}
    ),
    "tests/eval/evalsets/tool_selection_core.evalset.json": frozenset(
        {"read_file", "add_memory"}
    ),
    "tests/eval/evalsets/workspace_window.evalset.json": frozenset(
        {"set_workspace_window"}
    ),
    # (d) unrenamed module filename; (g) historically-accurate deletion prose.
    "AGENTS.md": frozenset({"add_memory", "set_workspace_window"}),
    # session_search: historically-accurate prose about the merge into
    # memory(action='search'), category (g).
    "horizon/agent.py": frozenset({"session_search"}),
    "horizon/conversation/system_prompt.py": frozenset({"session_search"}),
    "horizon/guardrails/permission_guard.py": frozenset({"session_search"}),
    "horizon/subagents/delegate_builder.py": frozenset({"session_search"}),
    # find_replacement's own kwargs (category b); the model-facing error
    # text says oldText, but this private helper's signature didn't change.
    "horizon/tools/_replacers.py": frozenset({"old_string", "replace_all"}),
}


async def test_no_legacy_tool_tokens_survive_after_the_rename():
    repo_root = _HORIZON_ROOT.parent
    offenders: dict[str, list[str]] = {}
    for glob in _LEGACY_SCAN_GLOBS:
        for path in repo_root.glob(glob):
            if not path.is_file():
                continue
            # web/**/*.ts(x) also globs vendored node_modules, drowning any
            # real hit even with it excluded.
            if "node_modules" in path.parts:
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            rel = str(path.relative_to(repo_root))
            allowed = _LEGACY_TOKEN_EXCEPTIONS.get(rel, frozenset())
            hits = [
                tok
                for tok in _LEGACY_TOKENS
                if tok in text and tok not in allowed
            ]
            if hits:
                offenders[rel] = hits
    assert not offenders, offenders


# These four tokens are unusable as bare substrings (too many unrelated
# meanings); scoping to the CALL shape, the token immediately followed by
# "(", collapses that to the 4 real internal callables below.
_CALL_SHAPE_PATTERN = re.compile(
    r"(?<![\w.])(reload|patch|terminal|write_file)\("
)
_CALL_SHAPE_EXCEPTIONS: dict[str, frozenset[str]] = {
    "horizon/commands/__init__.py": frozenset({"reload"}),  # /reload's helper
    "horizon/environment/sandbox.py": frozenset({"write_file"}),  # ADK method
    "horizon/sandbox/runtime/server.py": frozenset({"write_file"}),  # its route
}


async def test_no_legacy_tool_calls_survive_after_the_rename():
    """Dead-tool-name PROSE survives easily ("the terminal tool"); a
    dead-tool-name CALL ("terminal(...)") is the shape a model would
    actually try to invoke, and none should remain outside the exceptions.
    """
    repo_root = _HORIZON_ROOT.parent
    offenders: dict[str, list[str]] = {}
    for glob in (
        "horizon/**/*.py",
        "horizon/builtin_skills/**/*.md",
        "tests/eval/evalsets/*.json",
    ):
        for path in repo_root.glob(glob):
            if not path.is_file() or "node_modules" in path.parts:
                continue
            try:
                text = path.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            rel = str(path.relative_to(repo_root))
            allowed = _CALL_SHAPE_EXCEPTIONS.get(rel, frozenset())
            hits = [
                m.group(1)
                for m in _CALL_SHAPE_PATTERN.finditer(text)
                if m.group(1) not in allowed
            ]
            if hits:
                offenders[rel] = hits
    assert not offenders, offenders
