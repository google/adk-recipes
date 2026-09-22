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

from horizon.guardrails._regex_safety import safe_regex
from horizon.guardrails.permission_rules import parse_rule
from horizon.guardrails.policies import _evaluate


def test_plain_patterns_are_safe():
    assert safe_regex(r"^\s*bq query\b")
    assert safe_regex(r"git (status|log|diff)")
    assert safe_regex(r'"action":\s*"(list|poll)"')


def test_nested_quantifiers_rejected():
    assert not safe_regex(r"(a+)+")
    assert not safe_regex(r"(a*)*")
    assert not safe_regex(r"(.*)+")
    assert not safe_regex(r"([a-z]+)+$")


def test_overlong_pattern_rejected():
    assert not safe_regex("a" * 1001)


def test_empty_and_none_safe_handling():
    assert safe_regex("")
    assert not safe_regex(None)  # type: ignore[arg-type]


def test_permission_rule_with_unsafe_command_regex_is_dropped():
    rule = parse_rule(
        {"toolName": "bash", "commandRegex": r"(a+)+", "decision": "allow"}
    )
    assert rule is None


def test_permission_rule_with_unsafe_args_pattern_is_dropped():
    rule = parse_rule(
        {"toolName": "process", "argsPattern": r"(.*)+", "decision": "allow"}
    )
    assert rule is None


def test_policies_skips_unsafe_destructive_regex(tmp_path):
    from horizon.guardrails.policies import load_policies

    # Write an overlay with mix of safe and unsafe patterns
    overlay_path = tmp_path / ".lha" / "policies.jsonl"
    overlay_path.parent.mkdir(parents=True)
    overlay_path.write_text(
        '{"canonical_tool_name": "bash", "destructive_commands_regex": {"command": ["(a+)+", "safe_pattern"]}}\n'
    )

    # Load policies - the unsafe pattern should be filtered out
    rules = load_policies(tmp_path)

    # Find overlay rule - it should have only the safe pattern
    overlay_rule = None
    for r in reversed(rules):  # Overlay rules come last
        if (
            r.get("canonical_tool_name") == "bash"
            and "destructive_commands_regex" in r
        ):
            patterns = r.get("destructive_commands_regex", {}).get(
                "command", []
            )
            if "safe_pattern" in patterns and "(a+)+" not in patterns:
                overlay_rule = r
                break

    assert overlay_rule is not None, (
        "Overlay rule should exist with unsafe pattern filtered"
    )
    assert _evaluate(overlay_rule, "bash", {"command": "aaaa"}) is None
    assert (
        _evaluate(overlay_rule, "bash", {"command": "safe_pattern"}) is not None
    )
