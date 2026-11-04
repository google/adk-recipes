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

"""Tests for the lockfile-registry, Node-version and Python-version checks in check_conventions."""

from __future__ import annotations

import importlib.util
import pathlib
import sys
from typing import Any

import pytest

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[2]


def _load_check_conventions() -> Any:
    """Loads tools/ci/check_conventions.py, which is a script, not a package.

    Returns:
        The loaded module.
    """
    path = pathlib.Path(__file__).with_name("check_conventions.py")
    spec = importlib.util.spec_from_file_location("check_conventions", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


cc = _load_check_conventions()

_MIRROR_ENTRY = """\
    "node_modules/@a2a-js/sdk": {
      "version": "1.3.0",
      "resolved": "https://us-npm.pkg.dev/artifact-foundry-prod/ah-3p-staging-npm/@a2a-js/sdk/-/sdk-1.3.0.tgz",
"""
_PUBLIC_ENTRY = """\
    "node_modules/@a2a-js/sdk": {
      "version": "1.3.0",
      "resolved": "https://registry.npmjs.org/@a2a-js/sdk/-/sdk-1.3.0.tgz",
"""
_UV_MIRROR_ENTRY = (
    'sdist = { url = "https://us-python.pkg.dev/artifact-foundry-prod/'
    'ah-3p-staging-python/attrs/attrs-25.3.0.tar.gz", hash = "sha256:0" }\n'
)


def test_list_mirror_urls_finds_npm_mirror_with_line_number() -> None:
    assert cc.list_mirror_urls(_MIRROR_ENTRY) == [
        (
            3,
            "https://us-npm.pkg.dev/artifact-foundry-prod/ah-3p-staging-npm/"
            "@a2a-js/sdk/-/sdk-1.3.0.tgz",
        )
    ]


def test_list_mirror_urls_finds_uv_mirror() -> None:
    [(lineno, url)] = cc.list_mirror_urls(_UV_MIRROR_ENTRY)
    assert lineno == 1
    assert url.startswith("https://us-python.pkg.dev/")
    assert url.endswith(".tar.gz")


def test_list_mirror_urls_ignores_public_registries() -> None:
    assert cc.list_mirror_urls(_PUBLIC_ENTRY) == []
    assert (
        cc.list_mirror_urls(
            'url = "https://files.pythonhosted.org/packages/a/b/attrs.whl"'
        )
        == []
    )


def test_check_lockfile_registries_fails_on_mirror(
    tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    clean = tmp_path / "uv.lock"
    clean.write_text(_PUBLIC_ENTRY)
    dirty = tmp_path / "package-lock.json"
    dirty.write_text(_MIRROR_ENTRY)

    assert cc.check_lockfile_registries([clean, dirty]) is False
    err = capsys.readouterr().err
    assert f"{dirty}:3: https://us-npm.pkg.dev/" in err
    assert str(clean) not in err


def test_check_lockfile_registries_passes_on_public(
    tmp_path: pathlib.Path,
) -> None:
    lockfile = tmp_path / "package-lock.json"
    lockfile.write_text(_PUBLIC_ENTRY)
    assert cc.check_lockfile_registries([lockfile]) is True


def test_repository_lockfiles_use_public_registries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(_REPO_ROOT)
    lockfiles = cc.list_tracked_lockfiles()
    assert {p.as_posix() for p in lockfiles} >= {
        "uv.lock",
        "ui/web/package-lock.json",
    }
    assert cc.check_lockfile_registries(lockfiles) is True


@pytest.mark.parametrize(
    ("dockerfile", "expected"),
    [
        ("FROM node:22-slim AS web-builder\n", "22"),
        ("FROM python:3.12-slim\nFROM node:26.1.0-bookworm AS b\n", "26"),
        ("FROM docker.io/library/node:24\n", "24"),
        ("FROM python:3.12-slim\n", None),
        ("FROM node:lts-slim\n", None),
    ],
)
def test_extract_dockerfile_node_major(
    dockerfile: str, expected: str | None
) -> None:
    assert cc.extract_dockerfile_node_major(dockerfile) == expected


@pytest.mark.parametrize(
    ("nvmrc", "expected"),
    [
        ("22\n", "22"),
        ("v22.11.0\n", "22"),
        ("24.1", "24"),
        ("lts/*\n", None),
    ],
)
def test_extract_nvmrc_node_major(nvmrc: str, expected: str | None) -> None:
    assert cc.extract_nvmrc_node_major(nvmrc) == expected


def test_check_ui_node_version_fails_on_mismatch(
    tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    dockerfile = tmp_path / "Dockerfile"
    dockerfile.write_text("FROM node:26-slim AS web-builder\n")
    nvmrc = tmp_path / ".nvmrc"
    nvmrc.write_text("22\n")

    assert cc.check_ui_node_version(dockerfile, nvmrc) is False
    assert "node:26" in capsys.readouterr().err


def test_check_ui_node_version_fails_on_unreadable_version(
    tmp_path: pathlib.Path,
) -> None:
    dockerfile = tmp_path / "Dockerfile"
    dockerfile.write_text("FROM node:22-slim\n")
    nvmrc = tmp_path / ".nvmrc"
    nvmrc.write_text("lts/*\n")

    assert cc.check_ui_node_version(dockerfile, nvmrc) is False


def test_repository_ui_node_versions_match() -> None:
    assert (
        cc.check_ui_node_version(
            _REPO_ROOT / cc.UI_DOCKERFILE, _REPO_ROOT / cc.NVMRC
        )
        is True
    )


@pytest.mark.parametrize(
    ("dockerfile", "expected"),
    [
        ("FROM python:3.13-slim\n", ["3.13-slim"]),
        (
            "FROM node:26-slim AS web-builder\nFROM python:3.13-slim AS app\n",
            ["3.13-slim"],
        ),
        ("FROM docker.io/library/python:3.12\n", ["3.12"]),
        ("FROM --platform=linux/amd64 python:3.13-slim\n", ["3.13-slim"]),
        ("from python:3.13-slim\n", ["3.13-slim"]),
        (
            "FROM python:3.12-slim\nFROM python:3.13-slim\n",
            ["3.12-slim", "3.13-slim"],
        ),
        ("FROM node:22-slim\n", []),
    ],
)
def test_extract_dockerfile_python_tags(
    dockerfile: str, expected: list[str]
) -> None:
    assert cc.extract_dockerfile_python_tags(dockerfile) == expected


@pytest.mark.parametrize(
    ("tag", "expected"),
    [
        ("3.13-slim", (3, 13)),
        ("3.13", (3, 13)),
        ("3.15-slim-bookworm", (3, 15)),
        ("3-slim", None),
        ("3.13.7-slim", (3, 13)),
        ("slim", None),
    ],
)
def test_parse_python_tag_version(
    tag: str, expected: tuple[int, int] | None
) -> None:
    assert cc.parse_python_tag_version(tag) == expected


@pytest.mark.parametrize(
    ("spec", "expected"),
    [
        (">=3.11,<3.14", [(">=", (3, 11)), ("<", (3, 14))]),
        (">= 3.11, < 3.14", [(">=", (3, 11)), ("<", (3, 14))]),
        ("<3.14", [("<", (3, 14))]),
        ("<=3.13", None),
        ("==3.13", None),
        ("~=3.11", None),
        (">=3.11.2", None),
        ("==3.13.*", None),
        ("", None),
    ],
)
def test_parse_requires_python(
    spec: str, expected: list[tuple[str, tuple[int, int]]] | None
) -> None:
    assert cc.parse_requires_python(spec) == expected


@pytest.mark.parametrize(
    ("version", "expected"),
    [
        ((3, 11), True),
        ((3, 13), True),
        ((3, 10), False),
        ((3, 14), False),
        ((3, 15), False),
    ],
)
def test_matches_requires_python(
    version: tuple[int, int], expected: bool
) -> None:
    clauses = [(">=", (3, 11)), ("<", (3, 14))]
    assert cc.matches_requires_python(version, clauses) is expected


def _write_pyproject(
    tmp_path: pathlib.Path, requires_python: str
) -> pathlib.Path:
    """Writes a minimal pyproject.toml that declares requires-python.

    Args:
        tmp_path: The directory to write it in.
        requires_python: The requires-python value.

    Returns:
        The path of the written file.
    """
    pyproject = tmp_path / "pyproject.toml"
    pyproject.write_text(f'[project]\nrequires-python = "{requires_python}"\n')
    return pyproject


def test_check_python_base_images_passes_on_allowed_version(
    tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    dockerfile = tmp_path / "Dockerfile"
    dockerfile.write_text("FROM python:3.13-slim\n")
    pyproject = _write_pyproject(tmp_path, ">=3.11,<3.14")

    assert cc.check_python_base_images((dockerfile,), pyproject) is True
    assert "python:3.13-slim" in capsys.readouterr().out


def test_check_python_base_images_rejects_version_above_range(
    tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    allowed = tmp_path / "Dockerfile"
    allowed.write_text("FROM python:3.13-slim\n")
    too_new = tmp_path / "ui.Dockerfile"
    too_new.write_text("FROM node:26-slim AS web\nFROM python:3.15-slim\n")
    pyproject = _write_pyproject(tmp_path, ">=3.11,<3.14")

    assert cc.check_python_base_images((allowed, too_new), pyproject) is False
    err = capsys.readouterr().err
    assert "python:3.15-slim" in err
    assert "python:3.13-slim" not in err


def test_check_python_base_images_fails_without_python_stage(
    tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    dockerfile = tmp_path / "Dockerfile"
    dockerfile.write_text("FROM node:26-slim\n")
    pyproject = _write_pyproject(tmp_path, ">=3.11,<3.14")

    assert cc.check_python_base_images((dockerfile,), pyproject) is False
    assert "no FROM python:<X.Y> stage" in capsys.readouterr().err


def test_check_python_base_images_fails_on_unversioned_tag(
    tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    dockerfile = tmp_path / "Dockerfile"
    dockerfile.write_text("FROM python:slim\n")
    pyproject = _write_pyproject(tmp_path, ">=3.11,<3.14")

    assert cc.check_python_base_images((dockerfile,), pyproject) is False
    assert "python:slim" in capsys.readouterr().err


def test_check_python_base_images_fails_on_unparseable_specifier(
    tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    dockerfile = tmp_path / "Dockerfile"
    dockerfile.write_text("FROM python:3.13-slim\n")
    pyproject = _write_pyproject(tmp_path, "~=3.11")

    assert cc.check_python_base_images((dockerfile,), pyproject) is False
    assert 'Could not parse requires-python "~=3.11"' in capsys.readouterr().err


def test_repository_python_base_images_satisfy_requires_python() -> None:
    assert (
        cc.check_python_base_images(
            tuple(_REPO_ROOT / p for p in cc.PYTHON_DOCKERFILES),
            _REPO_ROOT / cc.PYPROJECT,
        )
        is True
    )
