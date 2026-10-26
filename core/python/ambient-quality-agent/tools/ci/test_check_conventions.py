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

"""Tests for the lockfile-registry and Node-version checks in check_conventions."""

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
