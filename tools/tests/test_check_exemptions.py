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

"""Unit tests for policy exemption loading, path matching, and CLI filtering."""

import io
import shutil
import subprocess
import sys
from pathlib import Path

import check_exemptions as m
import pytest

AQUA = "core/python/ambient-quality-agent"
TOOLS_DIR = Path(m.__file__).parent


def _write_policy(tmp_path: Path, body: str) -> Path:
    policy = tmp_path / "policy.yml"
    policy.write_text(body, encoding="utf-8")
    return policy


def _build_exemptions(*paths: str) -> dict[str, list[m.Exemption]]:
    return {
        "docker-serves": [m.Exemption("docker-serves", p, "why") for p in paths]
    }


# ---------------------------------------------------------------------------
# load_exemptions
# ---------------------------------------------------------------------------


def test_valid_policy_loads_normalized_entries(tmp_path: Path):
    policy = _write_policy(
        tmp_path,
        "check_exemptions:\n"
        "  docker-serves:\n"
        "    - path: ' /core/python/a/ '\n"
        "      reason: >-\n"
        "        Needs   live\n"
        "        credentials.\n"
        "  docker-build:\n"
        "    - path: contrib/python/b\n"
        "      reason: Broken upstream.\n",
    )
    assert m.load_exemptions(policy) == {
        "docker-serves": [
            m.Exemption(
                "docker-serves", "core/python/a", "Needs live credentials."
            )
        ],
        "docker-build": [
            m.Exemption("docker-build", "contrib/python/b", "Broken upstream.")
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
        ("check_exemptions: [docker-build]\n", "must be a mapping"),
        (
            "check_exemptions:\n  lint:\n    - {path: core/a, reason: x}\n",
            "unknown check id 'lint'; known ids: docker-build, docker-serves",
        ),
        (
            "check_exemptions:\n  docker-build: core/a\n",
            "docker-build: must be a list",
        ),
        (
            "check_exemptions:\n  docker-build:\n    - core/a\n",
            "docker-build[0]: must be a mapping",
        ),
        (
            "check_exemptions:\n  docker-build:\n"
            "    - {path: core/a, reason: x, until: 2027}\n",
            "unknown keys until",
        ),
        (
            "check_exemptions:\n  docker-build:\n    - {reason: x}\n",
            "`path` is missing",
        ),
        (
            "check_exemptions:\n  docker-build:\n"
            "    - {path: ' / ', reason: x}\n",
            "`path` is empty",
        ),
        (
            "check_exemptions:\n  docker-build:\n    - {path: 3, reason: x}\n",
            "`path` must be a string",
        ),
        (
            "check_exemptions:\n  docker-build:\n"
            "    - {path: tools/x, reason: x}\n",
            "must be under one of core, contrib, plugins",
        ),
        (
            "check_exemptions:\n  docker-build:\n"
            "    - {path: core/../tools, reason: x}\n",
            "must not contain empty, '.' or '..' components",
        ),
        (
            "check_exemptions:\n  docker-build:\n"
            "    - {path: core/./a, reason: x}\n",
            "must not contain empty, '.' or '..' components",
        ),
        (
            "check_exemptions:\n  docker-build:\n    - {path: core/a}\n",
            "`reason` is missing",
        ),
        (
            "check_exemptions:\n  docker-build:\n"
            "    - {path: core/a, reason: '  '}\n",
            "`reason` is empty",
        ),
        (
            "check_exemptions:\n  docker-build:\n"
            "    - {path: core/a, reason: [x]}\n",
            "`reason` must be a string",
        ),
        (
            "check_exemptions:\n  docker-build:\n"
            "    - {path: core/a, reason: x}\n"
            "    - {path: core/a/, reason: y}\n",
            "docker-build[1]: duplicate path 'core/a'",
        ),
    ],
)
def test_invalid_policy_is_rejected(tmp_path: Path, section: str, expected):
    policy = _write_policy(tmp_path, section)
    with pytest.raises(m.ExemptionPolicyError) as excinfo:
        m.load_exemptions(policy)
    assert expected in str(excinfo.value)


def test_check_without_entries_is_empty(tmp_path: Path):
    policy = _write_policy(tmp_path, "check_exemptions:\n  docker-build:\n")
    assert m.load_exemptions(policy) == {"docker-build": []}


def test_every_problem_is_reported_in_one_error(tmp_path: Path):
    policy = _write_policy(
        tmp_path,
        "check_exemptions:\n"
        "  lint: []\n"
        "  docker-build:\n"
        "    - {path: tools/x, reason: x}\n"
        "  docker-serves:\n"
        "    - {path: core/a}\n",
    )
    with pytest.raises(m.ExemptionPolicyError) as excinfo:
        m.load_exemptions(policy)
    message = str(excinfo.value)
    assert "unknown check id 'lint'" in message
    assert "docker-build[0]: `path` 'tools/x' must be under" in message
    assert "docker-serves[0]: `reason` is missing" in message


# ---------------------------------------------------------------------------
# find_exemption
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
    found = m.find_exemption(exemptions, "docker-serves", path)
    assert (found is not None) is matched


def test_find_exemption_is_scoped_to_its_check():
    assert (
        m.find_exemption(_build_exemptions("core/a"), "docker-build", "core/a")
        is None
    )


def test_find_exemption_rejects_an_unknown_check():
    with pytest.raises(ValueError, match="KNOWN_CHECKS"):
        m.find_exemption(_build_exemptions("core/a"), "lint", "core/a")


def test_render_skip_line():
    exemption = m.Exemption("docker-serves", "core/a", "Needs ADC.")
    assert m.render_skip_line(exemption, "core/a/b") == (
        "[SKIP] core/a/b: exempt from docker-serves by policy.yml "
        "check_exemptions: Needs ADC."
    )


# ---------------------------------------------------------------------------
# filter CLI
# ---------------------------------------------------------------------------


def test_filter_prints_non_exempt_paths_and_skips_to_stderr(
    tmp_path: Path, monkeypatch, capsys
):
    policy = _write_policy(
        tmp_path,
        "check_exemptions:\n"
        "  docker-serves:\n"
        "    - {path: core/python/a, reason: Needs ADC.}\n",
    )
    monkeypatch.setattr(m, "POLICY_PATH", policy)
    monkeypatch.setattr(
        "sys.stdin",
        io.StringIO("contrib/python/z\n\ncore/python/a\ncore/python/b\n"),
    )

    rc = m.main(["filter", "--check", "docker-serves"])

    captured = capsys.readouterr()
    assert rc == 0
    assert captured.out == "contrib/python/z\ncore/python/b\n"
    assert captured.err == (
        "[SKIP] core/python/a: exempt from docker-serves by policy.yml "
        "check_exemptions: Needs ADC.\n"
    )


def test_invalid_policy_is_a_ci_fault_kept_off_stdout(tmp_path: Path):
    tools = tmp_path / "tools"
    tools.mkdir()
    for name in ("check_exemptions.py", "ci_message.py"):
        shutil.copy(TOOLS_DIR / name, tools / name)
    (tmp_path / ".github").mkdir()
    _write_policy(tmp_path / ".github", "check_exemptions:\n  lint: []\n")

    proc = subprocess.run(
        [
            sys.executable,
            str(tools / "check_exemptions.py"),
            "filter",
            "--check",
            "docker-build",
        ],
        input="core/python/a\n",
        capture_output=True,
        text=True,
        check=False,
    )

    assert proc.returncode == 2
    assert proc.stdout == ""
    assert "unknown check id 'lint'" in proc.stderr


# ---------------------------------------------------------------------------
# The real policy file
# ---------------------------------------------------------------------------


def test_real_policy_exempts_the_ambient_quality_agent_from_serving():
    exemptions = m.load_exemptions()
    assert m.find_exemption(exemptions, "docker-serves", AQUA) is not None
    assert m.find_exemption(exemptions, "docker-build", AQUA) is None
