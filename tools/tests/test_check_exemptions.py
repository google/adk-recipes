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

"""Unit and integration tests for tools/check_exemptions.py."""

import io
import re
import shutil
import subprocess
import sys
from pathlib import Path

import check_exemptions as m
import pytest

AQUA = "core/python/ambient-quality-agent"
TOOLS_DIR = Path(m.__file__).parent
WORKFLOWS_DIR = m.REPO_ROOT / ".github" / "workflows"
FILTER_CALL = re.compile(
    r"check_exemptions\.py\s+filter\s+--check\s+([^\s\"')]+)"
)


def _write_policy(tmp_path: Path, body: str) -> Path:
    policy = tmp_path / "policy.yml"
    policy.write_text(body, encoding="utf-8")
    return policy


def _build_exemptions(*paths: str) -> dict[str, list[m.Exemption]]:
    return {"wf-a": [m.Exemption("wf-a", p, "why") for p in paths]}


def _find_filtered_check_ids(texts: list[str]) -> set[str]:
    return {
        check_id
        for text in texts
        for line in text.splitlines()
        if not line.lstrip().startswith("#")
        for check_id in FILTER_CALL.findall(line)
    }


# ---------------------------------------------------------------------------
# Policy loading and validation
# ---------------------------------------------------------------------------


def test_valid_policy_loads_normalized_entries(tmp_path: Path):
    policy = _write_policy(
        tmp_path,
        "check_exemptions:\n"
        "  wf-a:\n"
        "    - path: ' /core/python/a/ '\n"
        "      reason: >-\n"
        "        Needs   live\n"
        "        credentials.\n"
        "  wf-b/lint:\n"
        "    - path: contrib/python/b\n"
        "      reason: Broken upstream.\n",
    )
    assert m.load_exemptions(policy) == {
        "wf-a": [
            m.Exemption("wf-a", "core/python/a", "Needs live credentials.")
        ],
        "wf-b/lint": [
            m.Exemption("wf-b/lint", "contrib/python/b", "Broken upstream.")
        ],
    }


@pytest.mark.parametrize(
    "body", ["frozen_paths:\n  - python/agents\n", "check_exemptions:\n", ""]
)
def test_missing_or_empty_section_is_empty(tmp_path: Path, body: str):
    assert m.load_exemptions(_write_policy(tmp_path, body)) == {}


@pytest.mark.parametrize(
    "section,expected",
    [
        ("check_exemptions: [wf-a]\n", "must be a mapping"),
        (
            "check_exemptions:\n  wf-a: core/a\n",
            "wf-a: must be a list",
        ),
        (
            "check_exemptions:\n  wf-a:\n    - core/a\n",
            "wf-a[0]: must be a mapping",
        ),
        (
            "check_exemptions:\n  wf-a:\n"
            "    - {path: core/a, reason: x, until: 2027}\n",
            "unknown keys until",
        ),
        (
            "check_exemptions:\n  wf-a:\n    - {reason: x}\n",
            "`path` is missing",
        ),
        (
            "check_exemptions:\n  wf-a:\n    - {path: ' / ', reason: x}\n",
            "`path` is empty",
        ),
        (
            "check_exemptions:\n  wf-a:\n    - {path: 3, reason: x}\n",
            "`path` must be a string",
        ),
        (
            "check_exemptions:\n  wf-a:\n    - {path: tools/x, reason: x}\n",
            "must be under one of core, contrib, plugins",
        ),
        (
            "check_exemptions:\n  wf-a:\n"
            "    - {path: core/../tools, reason: x}\n",
            "must not contain empty, '.' or '..' components",
        ),
        (
            "check_exemptions:\n  wf-a:\n    - {path: core/./a, reason: x}\n",
            "must not contain empty, '.' or '..' components",
        ),
        (
            "check_exemptions:\n  wf-a:\n    - {path: core/a}\n",
            "`reason` is missing",
        ),
        (
            "check_exemptions:\n  wf-a:\n    - {path: core/a, reason: '  '}\n",
            "`reason` is empty",
        ),
        (
            "check_exemptions:\n  wf-a:\n    - {path: core/a, reason: [x]}\n",
            "`reason` must be a string",
        ),
        (
            "check_exemptions:\n  wf-a:\n"
            "    - {path: core/a, reason: x}\n"
            "    - {path: core/a/, reason: y}\n",
            "wf-a[1]: duplicate path 'core/a'",
        ),
    ],
)
def test_invalid_policy_is_rejected(tmp_path: Path, section: str, expected):
    policy = _write_policy(tmp_path, section)
    with pytest.raises(m.ExemptionPolicyError) as excinfo:
        m.load_exemptions(policy)
    assert expected in str(excinfo.value)


def test_check_without_entries_is_empty(tmp_path: Path):
    policy = _write_policy(tmp_path, "check_exemptions:\n  wf-a:\n")
    assert m.load_exemptions(policy) == {"wf-a": []}


@pytest.mark.parametrize(
    "check_id", ["recipe-docker-build", "wf/check-2", "0wf", "a"]
)
def test_well_formed_check_ids_are_accepted(tmp_path: Path, check_id: str):
    policy = _write_policy(tmp_path, f"check_exemptions:\n  {check_id}: []\n")
    assert m.load_exemptions(policy) == {check_id: []}


@pytest.mark.parametrize(
    "check_id",
    [
        "Upper",
        "-lead",
        "a/-b",
        "a/b/c",
        "a/",
        "/a",
        "a_b",
        "a b",
        "a.yml",
        "''",
        "7",
        "null",
    ],
)
def test_malformed_check_ids_are_rejected(tmp_path: Path, check_id: str):
    policy = _write_policy(tmp_path, f"check_exemptions:\n  {check_id}: []\n")
    with pytest.raises(m.ExemptionPolicyError, match="invalid check id"):
        m.load_exemptions(policy)


def test_every_problem_is_reported_in_one_error(tmp_path: Path):
    policy = _write_policy(
        tmp_path,
        "check_exemptions:\n"
        "  Bad_Id: []\n"
        "  wf-a:\n"
        "    - {path: tools/x, reason: x}\n"
        "  wf-b:\n"
        "    - {path: core/a}\n",
    )
    with pytest.raises(m.ExemptionPolicyError) as excinfo:
        m.load_exemptions(policy)
    message = str(excinfo.value)
    assert "invalid check id 'Bad_Id'" in message
    assert "wf-a[0]: `path` 'tools/x' must be under" in message
    assert "wf-b[0]: `reason` is missing" in message


# ---------------------------------------------------------------------------
# Path matching and exemption resolution
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path,matched",
    [
        ("core/python/a", True),
        ("core/python/a/app/agent.py", True),
        ("core/python/a/", True),
        ("core/python/a-b", False),
        ("core/python", False),
        ("contrib/python/a", False),
    ],
)
def test_find_exemption_matches_whole_components(path: str, matched: bool):
    exemptions = _build_exemptions("core/python/a")
    found = m.find_exemption(exemptions, "wf-a", path)
    assert (found is not None) is matched


def test_find_exemption_is_scoped_to_its_check():
    assert (
        m.find_exemption(_build_exemptions("core/a"), "wf-b", "core/a") is None
    )


def test_render_skip_line():
    exemption = m.Exemption("wf-b", "core/a", "Needs ADC.")
    assert m.render_skip_line(exemption, "core/a/b") == (
        "[SKIP] core/a/b: exempt from wf-b by policy.yml "
        "check_exemptions: Needs ADC."
    )


# ---------------------------------------------------------------------------
# Filter CLI command
# ---------------------------------------------------------------------------


def test_filter_prints_non_exempt_paths_and_skips_to_stderr(
    tmp_path: Path, monkeypatch, capsys
):
    policy = _write_policy(
        tmp_path,
        "check_exemptions:\n"
        "  wf-b:\n"
        "    - {path: core/python/a, reason: Needs ADC.}\n",
    )
    monkeypatch.setattr(m, "POLICY_PATH", policy)
    monkeypatch.setattr(
        "sys.stdin",
        io.StringIO("contrib/python/z\n\ncore/python/a\ncore/python/b\n"),
    )

    rc = m.main(["filter", "--check", "wf-b"])

    captured = capsys.readouterr()
    assert rc == 0
    assert captured.out == "contrib/python/z\ncore/python/b\n"
    assert captured.err == (
        "[SKIP] core/python/a: exempt from wf-b by policy.yml "
        "check_exemptions: Needs ADC.\n"
    )


def _run_filter_script(
    tmp_path: Path, policy_body: str, stdin: str
) -> subprocess.CompletedProcess:
    tools = tmp_path / "tools"
    tools.mkdir()
    for name in ("check_exemptions.py", "ci_message.py"):
        shutil.copy(TOOLS_DIR / name, tools / name)
    (tmp_path / ".github").mkdir()
    _write_policy(tmp_path / ".github", policy_body)
    return subprocess.run(
        [
            sys.executable,
            str(tools / "check_exemptions.py"),
            "filter",
            "--check",
            "wf-a",
        ],
        input=stdin,
        capture_output=True,
        text=True,
        check=False,
    )


def test_script_prints_only_non_exempt_paths_to_stdout(tmp_path: Path):
    proc = _run_filter_script(
        tmp_path,
        "check_exemptions:\n  wf-a:\n    - {path: core/python/a, reason: x}\n",
        "core/python/a\ncontrib/python/z\n",
    )

    assert proc.returncode == 0
    assert proc.stdout == "contrib/python/z\n"
    assert proc.stderr.startswith("[SKIP] core/python/a: exempt from wf-a")


def test_invalid_policy_is_a_ci_fault_kept_off_stdout(tmp_path: Path):
    proc = _run_filter_script(
        tmp_path, "check_exemptions:\n  Bad: []\n", "core/python/a\n"
    )

    assert proc.returncode == 2
    assert proc.stdout == ""
    assert "invalid check id 'Bad'" in proc.stderr


# ---------------------------------------------------------------------------
# Repository policy integration
# ---------------------------------------------------------------------------


def test_real_policy_exempts_the_ambient_quality_agent_from_docker_build():
    exemptions = m.load_exemptions()
    assert m.find_exemption(exemptions, "recipe-docker-build", AQUA)


def test_find_filtered_check_ids_reads_workflow_calls():
    text = (
        'X=$(echo "$R" | uv run python tools/check_exemptions.py filter '
        "--check recipe-docker-build)\n"
        "run: python tools/check_exemptions.py filter --check wf/lint\n"
        "run: python tools/check_exemptions.py --help\n"
        "  # python tools/check_exemptions.py filter --check commented\n"
    )
    assert _find_filtered_check_ids([text]) == {
        "recipe-docker-build",
        "wf/lint",
    }


def test_every_real_policy_check_id_is_filtered_by_a_workflow():
    texts = [
        p.read_text(encoding="utf-8")
        for p in sorted(WORKFLOWS_DIR.glob("*.yml"))
    ]
    used = _find_filtered_check_ids(texts)
    unused = sorted(set(m.load_exemptions()) - used)
    assert not unused, (
        f"policy.yml check_exemptions keys with no `check_exemptions.py "
        f"filter --check <id>` call in .github/workflows: {unused}"
    )
