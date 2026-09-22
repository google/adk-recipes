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

import subprocess
import sys
from pathlib import Path

import check_recipe_docker as m


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
