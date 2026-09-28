"""Unit tests for process_issue_response.py."""

import json
from pathlib import Path

import pytest
from process_issue_response import (
    DEFAULT_ASSIGNEE,
    ROUTING_RULES,
    Option,
    _parse_json_dict,
    _strip_trailing_commas,
    extract_decision_json,
    find_json_objects,
    normalize_path,
    parse_close_issue,
    parse_option,
    process_response,
    resolve_assignee_from_path,
)

# ---------------------------------------------------------------------------
# Option parsing tests
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (1, Option.CLARIFY),
        (2, Option.SIMPLE_SOLUTION),
        (3, Option.DETAILED_SOLUTION),
        (4, Option.ACKNOWLEDGE_AND_ASSIGN),
        ("1", Option.CLARIFY),
        ("2", Option.SIMPLE_SOLUTION),
        ("3", Option.DETAILED_SOLUTION),
        ("4", Option.ACKNOWLEDGE_AND_ASSIGN),
        ("option 1", Option.CLARIFY),
        ("option_2_quick_solution", Option.SIMPLE_SOLUTION),
        ("option_3_detailed_solution", Option.DETAILED_SOLUTION),
        ("option_4_acknowledge_and_assign", Option.ACKNOWLEDGE_AND_ASSIGN),
        ("clarify", Option.CLARIFY),
        ("simple", Option.SIMPLE_SOLUTION),
        ("detailed", Option.DETAILED_SOLUTION),
        ("assign", Option.ACKNOWLEDGE_AND_ASSIGN),
        ("unknown", Option.ACKNOWLEDGE_AND_ASSIGN),
    ],
)
def test_parse_option(raw, expected):
    assert parse_option(raw) == expected


# ---------------------------------------------------------------------------
# Path normalization & routing tests
# ---------------------------------------------------------------------------


def test_normalize_path():
    assert normalize_path("/core/python/my-recipe/") == "core/python/my-recipe"
    assert normalize_path("contrib\\go\\sample") == "contrib/go/sample"
    assert (
        normalize_path("SKILLS/retail/store-ops") == "skills/retail/store-ops"
    )


@pytest.mark.parametrize(
    ("path", "expected_assignee"),
    [
        # Core directory assignments
        ("/core/python/my-recipe", "eliasecchig"),
        ("core/python/gemini-live", "eliasecchig"),
        ("core/go/agent-sample", "ToniCorinne"),
        ("/core/go/tool-calling", "ToniCorinne"),
        ("core/java/spring-ai", "eliasecchig"),
        ("/core/java/sample", "eliasecchig"),
        ("core/typescript/express-agent", "happyhuman"),
        ("/core/typescript/sample", "happyhuman"),
        ("core/kotlin/android-gemini", "happyhuman"),
        ("/core/kotlin/sample", "happyhuman"),
        # Contrib directory assignments
        ("contrib/python/custom-tool", "happyhuman"),
        ("/contrib/python/recipe", "happyhuman"),
        ("contrib/go/rag-search", "ToniCorinne"),
        ("/contrib/go/sample", "ToniCorinne"),
        ("contrib/java/micronaut", "happyhuman"),
        ("/contrib/java/sample", "happyhuman"),
        ("contrib/typescript/nextjs", "happyhuman"),
        ("/contrib/typescript/sample", "happyhuman"),
        ("contrib/kotlin/kmp-recipe", "happyhuman"),
        ("/contrib/kotlin/sample", "happyhuman"),
        # Skills directory assignments
        ("skills/retail/store-ops", "happyhuman"),
        ("/skills/finance/analyst", "happyhuman"),
        ("skills/customer-service", "happyhuman"),
        # Catch-all
        ("docs/recipe-handbook/README.md", "happyhuman"),
        (".github/workflows/ci.yml", "happyhuman"),
        ("pyproject.toml", "happyhuman"),
        ("", "happyhuman"),
        (None, "happyhuman"),
    ],
)
def test_resolve_assignee_from_path(path, expected_assignee):
    assert resolve_assignee_from_path(path) == expected_assignee


# ---------------------------------------------------------------------------
# Routing synchronization tests (CODEOWNERS vs script vs workflow prompt)
# ---------------------------------------------------------------------------


def parse_codeowners_routing(
    codeowners_path: Path,
) -> tuple[list[tuple[str, str]], str]:
    rules = []
    default = ""
    for raw_line in codeowners_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split()
        if len(parts) >= 2:
            pattern, assignee = parts[0], parts[1].lstrip("@")
            if pattern == "*":
                default = assignee
            else:
                prefix = pattern.strip("/").rstrip("/*").rstrip("*")
                while prefix.endswith("/"):
                    prefix = prefix[:-1]
                rules.append((prefix, assignee))
    return rules, default


def parse_workflow_prompt_routing(
    workflow_path: Path,
) -> tuple[list[tuple[str, str]], str]:
    content = workflow_path.read_text(encoding="utf-8")
    rules = []
    default = ""
    for raw_line in content.splitlines():
        line = raw_line.strip()
        if "Assign to:" in line:
            default = line.split("Assign to:", 1)[1].strip().lstrip("@")
        elif line.startswith("- /") and "->" in line:
            left, right = line.split("->", 1)
            pattern = left.lstrip("- ").strip()
            assignee = right.strip().lstrip("@")
            prefix = pattern.strip("/").rstrip("/*").rstrip("*")
            while prefix.endswith("/"):
                prefix = prefix[:-1]
            rules.append((prefix, assignee))
    return rules, default


def test_routing_rules_sync_with_codeowners_and_workflow():
    repo_root = Path(__file__).resolve().parents[3]
    codeowners_path = repo_root / ".github" / "CODEOWNERS"
    workflow_path = (
        repo_root / ".github" / "workflows" / "_ai-issue-response-core.yml"
    )

    codeowners_rules, codeowners_default = parse_codeowners_routing(
        codeowners_path
    )
    workflow_rules, workflow_default = parse_workflow_prompt_routing(
        workflow_path
    )

    assert DEFAULT_ASSIGNEE == codeowners_default
    assert DEFAULT_ASSIGNEE == workflow_default
    assert ROUTING_RULES == codeowners_rules
    assert ROUTING_RULES == workflow_rules


# ---------------------------------------------------------------------------
# Close issue parsing tests
# ---------------------------------------------------------------------------


def test_parse_close_issue_rules():
    # Option 1 (clarify): always False
    assert parse_close_issue({"close_issue": True}, Option.CLARIFY) is False

    # Option 4 (acknowledge and assign): always False
    assert (
        parse_close_issue({"close_issue": True}, Option.ACKNOWLEDGE_AND_ASSIGN)
        is False
    )

    # Option 2 (simple solution): follows close_issue
    assert (
        parse_close_issue({"close_issue": True}, Option.SIMPLE_SOLUTION) is True
    )
    assert (
        parse_close_issue({"close_issue": False}, Option.SIMPLE_SOLUTION)
        is False
    )
    assert (
        parse_close_issue({"close_issue": "true"}, Option.SIMPLE_SOLUTION)
        is True
    )
    assert parse_close_issue({"close": "yes"}, Option.SIMPLE_SOLUTION) is True
    assert parse_close_issue({}, Option.SIMPLE_SOLUTION) is False

    # Option 3 (detailed solution): follows close_issue
    assert (
        parse_close_issue({"close_issue": True}, Option.DETAILED_SOLUTION)
        is True
    )
    assert (
        parse_close_issue({"close_issue": False}, Option.DETAILED_SOLUTION)
        is False
    )
    assert (
        parse_close_issue({"close_issue": "false"}, Option.DETAILED_SOLUTION)
        is False
    )
    assert parse_close_issue({"close": True}, Option.DETAILED_SOLUTION) is True


# ---------------------------------------------------------------------------
# Decision JSON extraction tests
# ---------------------------------------------------------------------------


def test_find_json_objects_multiple_in_text():
    text = (
        'Here is item 1: {"a": 1, "nested": {"b": 2}} and '
        'item 2: {"c": "hello \\"world\\""} trailing.'
    )
    objs = find_json_objects(text)
    assert len(objs) == 2
    assert objs[0] == {"a": 1, "nested": {"b": 2}}
    assert objs[1] == {"c": 'hello "world"'}


def test_extract_decision_json_from_raw():
    raw = json.dumps(
        {
            "option": 2,
            "response": "Try setting MODEL_NAME to gemini-3.5-flash.",
            "path": "core/python/sample",
            "close_issue": True,
        }
    )
    extracted = extract_decision_json(raw)
    assert extracted["option"] == 2
    assert "gemini-3.5-flash" in extracted["response"]
    assert extracted["close_issue"] is True


def test_extract_decision_json_from_fence():
    raw = """Here is my decision:
```json
{
  "option": 1,
  "response": "Could you provide steps to reproduce?",
  "path": null,
  "close_issue": false
}
```
"""
    extracted = extract_decision_json(raw)
    assert extracted["option"] == 1
    assert "reproduce" in extracted["response"]


def test_extract_decision_json_from_agy_envelope():
    raw = json.dumps(
        {
            "status": "SUCCESS",
            "response": """```json
{
  "option": 4,
  "response": "We have assigned this to the team.",
  "path": "core/go/sample",
  "assignee": "ToniCorinne"
}
```""",
        }
    )
    extracted = extract_decision_json(raw)
    assert extracted["option"] == 4
    assert extracted["path"] == "core/go/sample"


def test_extract_decision_json_with_thought_preamble():
    """Reproduces the issue #2604 failure where thought preamble preceded JSON."""
    raw_thought_text = (
        "I will search for any relevant files or documentation in the workspace to see "
        "if there are any specific guidelines on `core` vs `contrib` directories.\n"
        "{\n"
        '  "option": 3,\n'
        '  "path": null,\n'
        '  "assignee": null,\n'
        '  "close_issue": true,\n'
        '  "response": "For new recipes, please place them in contrib/python/your-recipe."\n'
        "}\n"
    )
    envelope = json.dumps({"status": "SUCCESS", "response": raw_thought_text})

    extracted = extract_decision_json(envelope)
    assert extracted["option"] == 3
    assert extracted["close_issue"] is True
    assert (
        extracted["response"]
        == "For new recipes, please place them in contrib/python/your-recipe."
    )


def test_extract_decision_json_with_nested_json_in_response():
    raw = (
        "Here is the solution:\n"
        "{\n"
        '  "option": 2,\n'
        '  "close_issue": true,\n'
        '  "response": "You can configure your settings with {\\"timeout\\": 30}."\n'
        "}"
    )
    extracted = extract_decision_json(raw)
    assert extracted["option"] == 2
    assert '{"timeout": 30}' in extracted["response"]


def test_extract_decision_json_with_trailing_commas():
    raw = """```json
{
  "option": 1,
  "response": "Could you share reproduction steps and error logs?",
  "path": null,
  "assignee": null,
  "close_issue": false,
}
```"""
    extracted = extract_decision_json(raw)
    assert extracted["option"] == 1
    assert "reproduction" in extracted["response"]
    assert extracted["close_issue"] is False


def test_extract_decision_json_with_raw_control_chars_in_response():
    # Raw literal newline inside a JSON string
    raw = (
        "{\n"
        '  "option": 1,\n'
        '  "path": null,\n'
        '  "assignee": null,\n'
        '  "close_issue": false,\n'
        '  "response": "Line 1\nLine 2"\n'
        "}"
    )
    extracted = extract_decision_json(raw)
    assert extracted["option"] == 1
    assert "Line 1\nLine 2" in extracted["response"]


def test_strip_trailing_commas():
    assert _strip_trailing_commas("") == ""
    assert _strip_trailing_commas('{"a": 1,}') == '{"a": 1}'
    assert _strip_trailing_commas('{"a": 1, \n }') == '{"a": 1 \n }'
    assert _strip_trailing_commas("[1, 2, ]") == "[1, 2 ]"
    # Preserves trailing comma patterns inside string literals
    assert (
        _strip_trailing_commas('{"code": "int arr[] = {1, 2, };", "opt": 1,}')
        == '{"code": "int arr[] = {1, 2, };", "opt": 1}'
    )
    # Handles escaped quotes in strings
    assert (
        _strip_trailing_commas(
            r'{"text": "quote: \"hello, }\", rest", "opt": 2,}'
        )
        == r'{"text": "quote: \"hello, }\", rest", "opt": 2}'
    )


def test_parse_json_dict_helper():
    assert _parse_json_dict("") is None
    assert _parse_json_dict("not json") is None
    assert _parse_json_dict('["not", "a", "dict"]') is None
    assert _parse_json_dict('{"key": "val"}') == {"key": "val"}
    assert _parse_json_dict('{"key": "val",}') == {"key": "val"}
    assert _parse_json_dict('{"arr": [1, 2,], "key": "val",}') == {
        "arr": [1, 2],
        "key": "val",
    }
    # String literal containing ', }' must not be rewritten
    parsed = _parse_json_dict(
        '{"response": "Valid syntax: {1, 2, } in C", "option": 1,}'
    )
    assert parsed == {
        "response": "Valid syntax: {1, 2, } in C",
        "option": 1,
    }


def test_extract_decision_json_invalid_raises():
    with pytest.raises(ValueError, match="No valid JSON decision"):
        extract_decision_json("Just random text with no JSON object at all.")


# ---------------------------------------------------------------------------
# Full processing response tests
# ---------------------------------------------------------------------------


def test_process_response_options_1_to_3():
    # Option 1: Clarify
    d1 = {
        "option": 1,
        "response": "Please share more information.",
        "path": "core/python/sample",
        "close_issue": True,  # should be overridden to False for clarify
    }
    opt, resp, assignee, close_issue = process_response(d1)
    assert opt == Option.CLARIFY
    assert resp == "Please share more information."
    assert assignee is None
    assert close_issue is False

    # Option 2: Quick solution
    d2 = {
        "option": 2,
        "response": "Use `uv sync` to install dependencies.",
        "close_issue": True,
    }
    opt, resp, assignee, close_issue = process_response(d2)
    assert opt == Option.SIMPLE_SOLUTION
    assert "uv sync" in resp
    assert assignee is None
    assert close_issue is True

    # Option 3: Detailed solution
    d3 = {
        "option": 3,
        "response": "Step 1: ... Step 2: ...",
        "close_issue": True,
    }
    opt, resp, assignee, close_issue = process_response(d3)
    assert opt == Option.DETAILED_SOLUTION
    assert "Step 1" in resp
    assert assignee is None
    assert close_issue is True


def test_process_response_option_4_with_path():
    # Option 4: Acknowledge and Assign with core/python path
    d4_py = {
        "option": 4,
        "response": "Received. Routing to the Python team.",
        "path": "/core/python/my-recipe",
        "close_issue": True,  # should be overridden to False for Option 4
    }
    opt, _resp, assignee, close_issue = process_response(d4_py)
    assert opt == Option.ACKNOWLEDGE_AND_ASSIGN
    assert assignee == "eliasecchig"
    assert close_issue is False

    # Option 4: Acknowledge and Assign with core/go path
    d4_go = {
        "option": 4,
        "response": "Received. Routing to Go maintainer.",
        "path": "core/go/search",
    }
    opt, _resp, assignee, close_issue = process_response(d4_go)
    assert opt == Option.ACKNOWLEDGE_AND_ASSIGN
    assert assignee == "ToniCorinne"
    assert close_issue is False

    # Option 4: Acknowledge and Assign with skills path
    d4_skills = {
        "option": 4,
        "response": "Received. Routing to skills maintainer.",
        "path": "skills/retail/store-ops",
    }
    opt, _resp, assignee, close_issue = process_response(d4_skills)
    assert opt == Option.ACKNOWLEDGE_AND_ASSIGN
    assert assignee == "happyhuman"
    assert close_issue is False


def test_process_response_option_4_fallback():
    # Option 4 with no path but valid assignee (case-insensitive)
    d4_raw = {
        "option": 4,
        "response": "Routing ticket.",
        "assignee": "tonicorinne",
    }
    opt, _resp, assignee, close_issue = process_response(d4_raw)
    assert opt == Option.ACKNOWLEDGE_AND_ASSIGN
    assert assignee == "ToniCorinne"
    assert close_issue is False

    # Option 4 with no path and unknown assignee -> catch-all default
    d4_unknown = {
        "option": 4,
        "response": "Routing ticket.",
        "assignee": "some_random_user",
    }
    opt, _resp, assignee, close_issue = process_response(d4_unknown)
    assert opt == Option.ACKNOWLEDGE_AND_ASSIGN
    assert assignee == "happyhuman"
    assert close_issue is False


# ---------------------------------------------------------------------------
# CLI entrypoint tests
# ---------------------------------------------------------------------------


def test_cli_execution_option_4(tmp_path):
    import subprocess
    import sys

    result_file = tmp_path / "agy_result.json"
    result_file.write_text(
        json.dumps(
            {
                "status": "SUCCESS",
                "response": json.dumps(
                    {
                        "option": 4,
                        "path": "core/python/sample-recipe",
                        "response": "Thank you! Routing this to the Python maintainer.",
                    }
                ),
            }
        ),
        encoding="utf-8",
    )
    comment_out = tmp_path / "comment.md"
    assignee_out = tmp_path / "assignee.txt"
    close_out = tmp_path / "close.txt"
    github_output = tmp_path / "github_output.txt"

    script_path = (
        Path(__file__).resolve().parents[1] / "process_issue_response.py"
    )
    cmd = [
        sys.executable,
        str(script_path),
        "--result",
        str(result_file),
        "--issue-number",
        "42",
        "--comment-out",
        str(comment_out),
        "--assignee-out",
        str(assignee_out),
        "--close-out",
        str(close_out),
        "--github-output",
        str(github_output),
    ]

    res = subprocess.run(cmd, capture_output=True, text=True, check=False)
    assert res.returncode == 0, res.stderr
    assert "eliasecchig" in res.stdout

    assert comment_out.read_text(encoding="utf-8").strip() == (
        "Thank you! Routing this to the Python maintainer."
    )
    assert assignee_out.read_text(encoding="utf-8").strip() == "eliasecchig"
    assert close_out.read_text(encoding="utf-8").strip() == "false"

    output_lines = github_output.read_text(encoding="utf-8").splitlines()
    assert "option=4" in output_lines
    assert "assignee=eliasecchig" in output_lines
    assert "has_assignee=true" in output_lines
    assert "close_issue=false" in output_lines


def test_cli_execution_option_3_with_close(tmp_path):
    import subprocess
    import sys

    result_file = tmp_path / "agy_result.json"
    thought_and_json = (
        "I will search for any relevant guidelines on core vs contrib.\n"
        "{\n"
        '  "option": 3,\n'
        '  "close_issue": true,\n'
        '  "response": "Please place your new recipe in `contrib/python/your-recipe`."\n'
        "}"
    )
    result_file.write_text(
        json.dumps(
            {
                "status": "SUCCESS",
                "response": thought_and_json,
            }
        ),
        encoding="utf-8",
    )
    comment_out = tmp_path / "comment.md"
    assignee_out = tmp_path / "assignee.txt"
    close_out = tmp_path / "close.txt"
    github_output = tmp_path / "github_output.txt"

    script_path = (
        Path(__file__).resolve().parents[1] / "process_issue_response.py"
    )
    cmd = [
        sys.executable,
        str(script_path),
        "--result",
        str(result_file),
        "--issue-number",
        "2604",
        "--comment-out",
        str(comment_out),
        "--assignee-out",
        str(assignee_out),
        "--close-out",
        str(close_out),
        "--github-output",
        str(github_output),
    ]

    res = subprocess.run(cmd, capture_output=True, text=True, check=False)
    assert res.returncode == 0, res.stderr

    assert comment_out.read_text(encoding="utf-8").strip() == (
        "Please place your new recipe in `contrib/python/your-recipe`."
    )
    assert assignee_out.read_text(encoding="utf-8").strip() == ""
    assert close_out.read_text(encoding="utf-8").strip() == "true"

    output_lines = github_output.read_text(encoding="utf-8").splitlines()
    assert "option=3" in output_lines
    assert "has_assignee=false" in output_lines
    assert "close_issue=true" in output_lines
