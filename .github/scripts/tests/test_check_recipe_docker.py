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

"""Unit tests for check_recipe_docker.py."""

import io
import subprocess
import sys
from pathlib import Path

import check_recipe_docker as m
import pytest
from check_exemptions import Exemption

SKIPPED = "core/python/skipped"
CHECKED = "core/python/checked"


def test_parse_env_example(tmp_path: Path):
    env_file = tmp_path / ".env.example"
    env_file.write_text(
        "# Example comment\n"
        "MODEL_NAME=gemini-3.5-flash\n"
        "export PORT=8080 # inline comment\n"
        'PROJECT_ID="my-project"\n'
        "EMPTY_KEY=\n"
        "TODO_KEY=<TODO: replace-me>\n"
        "INVALID-LINE\n",
        encoding="utf-8",
    )
    parsed = m.parse_env_example(env_file)
    assert parsed.get("MODEL_NAME") == "gemini-3.5-flash"
    assert parsed.get("PORT") == "8080"
    assert parsed.get("PROJECT_ID") == "my-project"
    assert "TODO_KEY" not in parsed
    assert "EMPTY_KEY" in parsed and parsed["EMPTY_KEY"] == ""


def test_sanitize_tag():
    assert (
        m.sanitize_tag("core/python/ambient-expense-agent")
        == "core-python-ambient-expense-agent"
    )
    assert m.sanitize_tag("My Recipe @ 1.0!") == "my-recipe-1.0"
    assert m.sanitize_tag("---test---") == "test"
    assert m.sanitize_tag("") == "recipe"


def test_parse_host_port():
    output = "8080/tcp -> 0.0.0.0:32768\n8080/tcp -> [::]:32768\n"
    assert m.parse_host_port(output) == 32768

    output_v4_only = "127.0.0.1:45123\n"
    assert m.parse_host_port(output_v4_only) == 45123

    assert m.parse_host_port("") is None
    assert m.parse_host_port("invalid output") is None


def test_diagnose_build_failure():
    # Missing file
    err, fix = m.diagnose_build_failure(
        "COPY failed: stat /assets: file not found", ""
    )
    assert "COPY instruction was not found" in err
    assert "assets" in fix or "exist" in fix

    # uv.lock mismatch
    err, fix = m.diagnose_build_failure(
        "RUN uv sync --frozen failed: lockfile out of date", ""
    )
    assert "uv dependency synchronization failed" in err
    assert "uv lock" in fix

    # npm build
    err, fix = m.diagnose_build_failure(
        "npm ERR! command failed: npm run build", ""
    )
    assert "Frontend build failed" in err
    assert "package.json" in fix

    # Generic
    err, fix = m.diagnose_build_failure(
        "Command returned a non-zero code: 1", ""
    )
    assert "RUN instruction" in err


def test_diagnose_runtime_failure():
    # ValidationError
    err, fix = m.diagnose_runtime_failure(
        "pydantic_core._pydantic_core.ValidationError: 1 validation error for Gemini"
    )
    assert "Pydantic" in err
    assert ".env.example" in fix

    # ModuleNotFoundError
    err, fix = m.diagnose_runtime_failure(
        "ModuleNotFoundError: No module named 'foo'"
    )
    assert "import a required module" in err
    assert "pyproject.toml" in fix

    # DefaultCredentialsError
    err, fix = m.diagnose_runtime_failure(
        "google.auth.exceptions.DefaultCredentialsError: could not automatically determine credentials"
    )
    assert "credentials at module/import time" in err
    assert "INTEGRATION_TEST" in fix


def test_find_recipes_with_dockerfile(tmp_path: Path):
    core = tmp_path / "core" / "python" / "recipe-a"
    core.mkdir(parents=True)
    (core / "manifest.yaml").write_text("language: python\n", encoding="utf-8")
    (core / "Dockerfile").write_text("FROM scratch\n", encoding="utf-8")

    # Recipe without Dockerfile
    contrib = tmp_path / "contrib" / "python" / "recipe-b"
    contrib.mkdir(parents=True)
    (contrib / "manifest.yaml").write_text(
        "language: python\n", encoding="utf-8"
    )

    # Subdirectory Dockerfile (not at recipe root)
    nested = core / "subservice"
    nested.mkdir(parents=True)
    (nested / "Dockerfile").write_text("FROM scratch\n", encoding="utf-8")

    found = m.find_recipes_with_dockerfile(tmp_path)
    assert len(found) == 1
    assert found[0] == core


def test_validate_recipe_docker_no_dockerfile(tmp_path: Path):
    recipe = tmp_path / "recipe"
    recipe.mkdir()
    result = m.validate_recipe_docker(recipe)
    assert not result.has_dockerfile
    assert not result.passed
    assert "No Dockerfile found" in (result.error_message or "")


def test_validate_recipe_docker_build_failure(tmp_path: Path, monkeypatch):
    recipe = tmp_path / "recipe"
    recipe.mkdir()
    (recipe / "Dockerfile").write_text("FROM scratch\n", encoding="utf-8")

    def mock_run_cmd(cmd, **kwargs):
        if "build" in cmd:
            return subprocess.CompletedProcess(
                args=cmd,
                returncode=1,
                stdout="",
                stderr="ERROR: failed to solve: /missing not found",
            )
        return subprocess.CompletedProcess(
            args=cmd, returncode=0, stdout="", stderr=""
        )

    monkeypatch.setattr(m, "run_cmd", mock_run_cmd)

    result = m.validate_recipe_docker(recipe)
    assert result.has_dockerfile
    assert not result.build_passed
    assert not result.passed
    assert result.error_message is not None


def test_validate_recipe_docker_success(tmp_path: Path, monkeypatch):
    recipe = tmp_path / "recipe"
    recipe.mkdir()
    (recipe / "Dockerfile").write_text("FROM scratch\n", encoding="utf-8")

    def mock_run_cmd(cmd, **kwargs):
        if "build" in cmd:
            return subprocess.CompletedProcess(
                args=cmd, returncode=0, stdout="Successfully built", stderr=""
            )
        if "run" in cmd:
            return subprocess.CompletedProcess(
                args=cmd, returncode=0, stdout="container-123", stderr=""
            )
        if "port" in cmd:
            return subprocess.CompletedProcess(
                args=cmd,
                returncode=0,
                stdout="8080/tcp -> 127.0.0.1:32768\n",
                stderr="",
            )
        if "inspect" in cmd:
            return subprocess.CompletedProcess(
                args=cmd, returncode=0, stdout="true\n", stderr=""
            )
        return subprocess.CompletedProcess(
            args=cmd, returncode=0, stdout="", stderr=""
        )

    def mock_probe_http(url, timeout=5):
        if "/list-apps" in url:
            return 200, '["app"]'
        return 404, "Not Found"

    monkeypatch.setattr(m, "run_cmd", mock_run_cmd)
    monkeypatch.setattr(m, "probe_http", mock_probe_http)

    result = m.validate_recipe_docker(recipe, probe_timeout=5)
    assert result.has_dockerfile
    assert result.build_passed
    assert result.run_passed
    assert result.passed
    assert result.accessible_endpoint == "/list-apps (HTTP 200)"


def test_main_cli(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["check_recipe_docker.py"])
    rc = m.main()
    assert rc == 0
    assert "No recipe directories with Dockerfiles" in capsys.readouterr().out


def _docker_mocks(monkeypatch, probe):
    """Wire up a container that builds and stays running, with `probe` for HTTP."""

    def mock_run_cmd(cmd, **kwargs):
        if "build" in cmd:
            return subprocess.CompletedProcess(
                args=cmd, returncode=0, stdout="Successfully built", stderr=""
            )
        if "run" in cmd:
            return subprocess.CompletedProcess(
                args=cmd, returncode=0, stdout="container-123", stderr=""
            )
        if "port" in cmd:
            return subprocess.CompletedProcess(
                args=cmd,
                returncode=0,
                stdout="8080/tcp -> 127.0.0.1:32768\n",
                stderr="",
            )
        if "inspect" in cmd:
            return subprocess.CompletedProcess(
                args=cmd, returncode=0, stdout="true\n", stderr=""
            )
        if "logs" in cmd:
            return subprocess.CompletedProcess(
                args=cmd, returncode=0, stdout="", stderr=""
            )
        return subprocess.CompletedProcess(
            args=cmd, returncode=0, stdout="", stderr=""
        )

    monkeypatch.setattr(m, "run_cmd", mock_run_cmd)
    monkeypatch.setattr(m, "probe_http", probe)


def _recipe_with_dockerfile(tmp_path: Path) -> Path:
    recipe = tmp_path / "recipe"
    recipe.mkdir()
    (recipe / "Dockerfile").write_text("FROM scratch\n", encoding="utf-8")
    return recipe


def test_all_paths_404_is_not_accessible(tmp_path: Path, monkeypatch):
    """A server that 404s everywhere is up but serving nothing — that is a FAIL.

    This is the false PASS the gate exists to prevent: an image whose web
    server boots but whose ADK app never mounts answers 404 on every probe
    path. Counting that as "accessible" would report a broken container green.
    """
    _docker_mocks(monkeypatch, lambda url, timeout=5: (404, "Not Found"))

    result = m.validate_recipe_docker(
        _recipe_with_dockerfile(tmp_path), probe_timeout=1
    )
    assert result.build_passed
    assert not result.run_passed
    assert not result.passed
    assert result.accessible_endpoint is None
    assert "did not become accessible" in (result.error_message or "")


def test_401_is_accessible(tmp_path: Path, monkeypatch):
    """401 proves the route exists and the request was routed to it."""
    _docker_mocks(monkeypatch, lambda url, timeout=5: (401, "Unauthorized"))

    result = m.validate_recipe_docker(
        _recipe_with_dockerfile(tmp_path), probe_timeout=1
    )
    assert result.run_passed
    assert result.passed
    assert "HTTP 401" in (result.accessible_endpoint or "")


def test_200_wins_over_a_404_on_an_earlier_path(tmp_path: Path, monkeypatch):
    """A 404 on one path must not stop a later path from proving the app is up."""

    def probe(url, timeout=5):
        return (200, '["app"]') if "/docs" in url else (404, "Not Found")

    _docker_mocks(monkeypatch, probe)

    result = m.validate_recipe_docker(
        _recipe_with_dockerfile(tmp_path), probe_timeout=1
    )
    assert result.run_passed
    assert result.accessible_endpoint == "/docs (HTTP 200)"


def _recipe_in_repo(repo_root: Path, rel: str) -> Path:
    recipe = repo_root / rel
    recipe.mkdir(parents=True)
    (recipe / "manifest.yaml").write_text(
        "language: python\n", encoding="utf-8"
    )
    (recipe / "Dockerfile").write_text("FROM scratch\n", encoding="utf-8")
    return recipe


def _record_docker_commands(
    monkeypatch, *, build_fails: bool = False
) -> list[list[str]]:
    """Simulates container execution and records issued Docker commands.

    Args:
        monkeypatch: Pytest fixture used to intercept `run_cmd` and container
            probes.
        build_fails: When True, simulates a failed `docker build` invocation.

    Returns:
        Argument lists of all intercepted `run_cmd` calls, in execution order.
    """
    _docker_mocks(monkeypatch, lambda url, timeout=5: (200, '["app"]'))
    commands: list[list[str]] = []
    mocked_run_cmd = m.run_cmd

    def recording_run_cmd(cmd, **kwargs):
        commands.append(cmd)
        if build_fails and cmd[:2] == ["docker", "build"]:
            return subprocess.CompletedProcess(
                args=cmd, returncode=1, stdout="", stderr="build broke"
            )
        return mocked_run_cmd(cmd, **kwargs)

    monkeypatch.setattr(m, "run_cmd", recording_run_cmd)
    return commands


def _use_exemptions(monkeypatch, *exemptions: Exemption) -> None:
    by_check: dict[str, list[Exemption]] = {}
    for exemption in exemptions:
        by_check.setdefault(exemption.check, []).append(exemption)
    monkeypatch.setattr(m, "load_exemptions", lambda: by_check)


def _select_docker_commands(
    commands: list[list[str]], verb: str, rel: str
) -> list[list[str]]:
    return [
        c for c in commands if c[:2] == ["docker", verb] and rel in " ".join(c)
    ]


def _build_image_tag(rel: str) -> str:
    return f"adk-verify/{m.sanitize_tag(rel)}:test"


@pytest.mark.parametrize("mode", ["positional", "all", "stdin"])
def test_docker_build_exemption_applies_to_every_input_mode(
    tmp_path: Path, monkeypatch, capsys, mode: str
):
    monkeypatch.setattr(m, "REPO_ROOT", tmp_path)
    _recipe_in_repo(tmp_path, SKIPPED)
    _recipe_in_repo(tmp_path, CHECKED)
    _use_exemptions(
        monkeypatch, Exemption("docker-build", SKIPPED, "cannot build")
    )
    commands = _record_docker_commands(monkeypatch)

    argv = ["check_recipe_docker.py", "--probe-timeout", "1"]
    if mode == "positional":
        argv += [SKIPPED, CHECKED]
    elif mode == "all":
        argv.append("--all")
    else:
        monkeypatch.setattr("sys.stdin", io.StringIO(f"{SKIPPED}\n{CHECKED}\n"))
    monkeypatch.setattr(sys, "argv", argv)

    rc = m.main()

    out = capsys.readouterr().out
    assert rc == 0
    assert (
        f"[SKIP] {SKIPPED}: exempt from docker-build by policy.yml "
        "check_exemptions: cannot build" in out
    )
    assert f"[PASS] {CHECKED}" in out
    assert _select_docker_commands(commands, "build", CHECKED)
    assert not _select_docker_commands(commands, "build", SKIPPED)


def test_docker_serves_exemption_builds_but_does_not_run(
    tmp_path: Path, monkeypatch, capsys
):
    monkeypatch.setattr(m, "REPO_ROOT", tmp_path)
    _recipe_in_repo(tmp_path, SKIPPED)
    _recipe_in_repo(tmp_path, CHECKED)
    _use_exemptions(
        monkeypatch, Exemption("docker-serves", SKIPPED, "needs ADC")
    )
    commands = _record_docker_commands(monkeypatch)
    monkeypatch.setattr(
        sys,
        "argv",
        ["check_recipe_docker.py", "--probe-timeout", "1", SKIPPED, CHECKED],
    )

    rc = m.main()

    out = capsys.readouterr().out
    assert rc == 0
    assert (
        f"[SKIP] {SKIPPED}: exempt from docker-serves by policy.yml "
        "check_exemptions: needs ADC" in out
    )
    assert _select_docker_commands(commands, "build", SKIPPED)
    run_images = [c[-1] for c in commands if c[:2] == ["docker", "run"]]
    assert run_images == [_build_image_tag(CHECKED)]
    assert ["docker", "rmi", "-f", _build_image_tag(SKIPPED)] in commands
    assert f"[PASS] {SKIPPED} (build only; serve skipped: needs ADC)" in out
    assert f"[PASS] {CHECKED} (/list-apps (HTTP 200))" in out
    assert "every recipe not exempt from docker-serves verified" in out


def test_docker_serves_exemption_still_fails_a_broken_build(
    tmp_path: Path, monkeypatch, capsys
):
    monkeypatch.setattr(m, "REPO_ROOT", tmp_path)
    _recipe_in_repo(tmp_path, SKIPPED)
    _use_exemptions(
        monkeypatch, Exemption("docker-serves", SKIPPED, "needs ADC")
    )
    commands = _record_docker_commands(monkeypatch, build_fails=True)
    monkeypatch.setattr(sys, "argv", ["check_recipe_docker.py", SKIPPED])

    rc = m.main()

    out = capsys.readouterr().out
    assert rc == 1
    assert f"[FAIL] {SKIPPED}" in out
    assert f"[docker-build] Docker image failed to build for {SKIPPED}" in out
    assert not [c for c in commands if c[:2] == ["docker", "run"]]


def test_docker_build_exemption_takes_precedence_over_docker_serves(
    tmp_path: Path, monkeypatch, capsys
):
    monkeypatch.setattr(m, "REPO_ROOT", tmp_path)
    _recipe_in_repo(tmp_path, SKIPPED)
    _use_exemptions(
        monkeypatch,
        Exemption("docker-build", SKIPPED, "cannot build"),
        Exemption("docker-serves", SKIPPED, "needs ADC"),
    )
    commands = _record_docker_commands(monkeypatch)
    monkeypatch.setattr(sys, "argv", ["check_recipe_docker.py", SKIPPED])

    rc = m.main()

    out = capsys.readouterr().out
    assert rc == 0
    assert "exempt from docker-build" in out
    assert "exempt from docker-serves" not in out
    assert commands == []


def test_all_targets_exempt_from_docker_build_needs_no_docker(
    tmp_path: Path, monkeypatch, capsys
):
    monkeypatch.setattr(m, "REPO_ROOT", tmp_path)
    _recipe_in_repo(tmp_path, SKIPPED)
    _recipe_in_repo(tmp_path, CHECKED)
    _use_exemptions(
        monkeypatch,
        Exemption("docker-build", SKIPPED, "cannot build"),
        Exemption("docker-build", CHECKED, "cannot build either"),
    )
    commands = _record_docker_commands(monkeypatch)
    monkeypatch.setattr(
        sys, "argv", ["check_recipe_docker.py", SKIPPED, CHECKED]
    )

    rc = m.main()

    assert rc == 0
    assert commands == []
    assert (
        "[PASS] Every recipe Dockerfile in scope is exempt by policy.yml "
        "check_exemptions." in capsys.readouterr().out
    )
