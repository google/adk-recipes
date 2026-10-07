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
"""Unit tests for check_recipe_pom.py.

These tests pin the metadata rules for Java recipes:
  - <artifactId> matching folder basename (or <vertical>-<solution> for plugins)
  - Java release floor: maven.compiler.release in <properties> is canonical and <= 17
  - Setting only source/target fails, naming release in message
  - <description> matching manifest.description (if present)
  - <repositories>, <pluginRepositories>, and <mirrors> pointing to Maven Central
  - Missing pom.xml skipped with exit 0
  - Exit code contract (0 = pass, 1 = violations, 2 = CI fault)
"""

from __future__ import annotations

import sys
from pathlib import Path

import check_recipe_pom as m
import pytest

EXIT_OK = m.EXIT_OK
EXIT_VIOLATIONS = 1
EXIT_CI_FAULT = 2

MOCK_CHECKOUT = "/mock/repo"


def _write_file(target: Path, content: str | bytes) -> None:
    if isinstance(content, bytes):
        target.write_bytes(content)
    else:
        target.write_text(content, encoding="utf-8")


def _recipe(
    tmp_path: Path,
    pom_xml: str | bytes | None = None,
    manifest_yaml: str | bytes | None = None,
) -> Path:
    if pom_xml is not None:
        _write_file(tmp_path / "pom.xml", pom_xml)
    if manifest_yaml is not None:
        _write_file(tmp_path / "manifest.yaml", manifest_yaml)
    return tmp_path


def _run(tmp_path: Path, monkeypatch) -> int:
    monkeypatch.setattr(sys, "argv", ["check_recipe_pom.py", str(tmp_path)])
    return m.main()


# ---------------------------------------------------------------------------
# expected_project_name
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("core/java/my-recipe", "my-recipe"),
        ("./core/java/my-recipe", "my-recipe"),
        ("contrib/java/financial-advisor", "financial-advisor"),
        ("./plugins/retail/store-ops", "retail-store-ops"),
        ("plugins/retail/store-ops", "retail-store-ops"),
        ("my-recipe", "my-recipe"),
    ],
)
def test_expected_project_name_for_repo_relative_paths(path, expected):
    assert m.expected_project_name(Path(path)) == expected


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        (
            f"{MOCK_CHECKOUT}/core/java/my-recipe",
            "my-recipe",
        ),
        (
            f"{MOCK_CHECKOUT}/contrib/java/financial-advisor",
            "financial-advisor",
        ),
        (
            f"{MOCK_CHECKOUT}/plugins/retail/store-ops",
            "retail-store-ops",
        ),
    ],
)
def test_expected_project_name_for_absolute_paths_under_repo_root(
    path, expected
):
    assert (
        m.expected_project_name(Path(path), repo_root=Path(MOCK_CHECKOUT))
        == expected
    )


# ---------------------------------------------------------------------------
# Valid pom.xml
# ---------------------------------------------------------------------------

VALID_POM = """<?xml version="1.0" encoding="UTF-8"?>
<project xmlns="http://maven.apache.org/POM/4.0.0"
  xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
  xsi:schemaLocation="http://maven.apache.org/POM/4.0.0 http://maven.apache.org/xsd/maven-4.0.0.xsd">
  <modelVersion>4.0.0</modelVersion>
  <groupId>com.google.adk</groupId>
  <artifactId>{artifact_id}</artifactId>
  <version>1.0.0</version>
  <properties>
    <maven.compiler.release>17</maven.compiler.release>
    <project.build.sourceEncoding>UTF-8</project.build.sourceEncoding>
  </properties>
</project>
"""


def test_valid_pom_passes(tmp_path, monkeypatch, capsys):
    _recipe(
        tmp_path,
        pom_xml=VALID_POM.format(artifact_id=tmp_path.name),
    )
    assert _run(tmp_path, monkeypatch) == EXIT_OK
    assert "::error" not in capsys.readouterr().out


# ---------------------------------------------------------------------------
# artifactId rules
# ---------------------------------------------------------------------------


def test_artifact_id_mismatch_fails(tmp_path, monkeypatch, capsys):
    _recipe(
        tmp_path,
        pom_xml=VALID_POM.format(artifact_id="different-name"),
    )
    assert _run(tmp_path, monkeypatch) == EXIT_VIOLATIONS
    out = capsys.readouterr().out
    assert "::error" in out
    assert "different-name" in out
    assert tmp_path.name in out


def test_missing_artifact_id_fails(tmp_path, monkeypatch, capsys):
    pom = """<?xml version="1.0" encoding="UTF-8"?>
<project xmlns="http://maven.apache.org/POM/4.0.0">
  <modelVersion>4.0.0</modelVersion>
  <groupId>com.google.adk</groupId>
  <properties>
    <maven.compiler.release>17</maven.compiler.release>
  </properties>
</project>
"""
    _recipe(tmp_path, pom_xml=pom)
    assert _run(tmp_path, monkeypatch) == EXIT_VIOLATIONS
    assert "::error" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# Java version floor rules
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("release_ver", [17, 11, 8])
def test_valid_release_versions_pass(
    tmp_path, release_ver, monkeypatch, capsys
):
    pom = f"""<?xml version="1.0" encoding="UTF-8"?>
<project xmlns="http://maven.apache.org/POM/4.0.0">
  <modelVersion>4.0.0</modelVersion>
  <artifactId>{tmp_path.name}</artifactId>
  <properties>
    <maven.compiler.release>{release_ver}</maven.compiler.release>
  </properties>
</project>
"""
    _recipe(tmp_path, pom_xml=pom)
    assert _run(tmp_path, monkeypatch) == EXIT_OK
    assert "::error" not in capsys.readouterr().out


def test_release_21_fails_exceeding_ci_pin(tmp_path, monkeypatch, capsys):
    pom = f"""<?xml version="1.0" encoding="UTF-8"?>
<project xmlns="http://maven.apache.org/POM/4.0.0">
  <modelVersion>4.0.0</modelVersion>
  <artifactId>{tmp_path.name}</artifactId>
  <properties>
    <maven.compiler.release>21</maven.compiler.release>
  </properties>
</project>
"""
    _recipe(tmp_path, pom_xml=pom)
    assert _run(tmp_path, monkeypatch) == EXIT_VIOLATIONS
    out = capsys.readouterr().out
    assert "::error" in out
    assert "exceeds CI's pinned JDK version (17)" in out


def test_source_target_without_release_fails_and_names_release(
    tmp_path, monkeypatch, capsys
):
    pom = f"""<?xml version="1.0" encoding="UTF-8"?>
<project xmlns="http://maven.apache.org/POM/4.0.0">
  <modelVersion>4.0.0</modelVersion>
  <artifactId>{tmp_path.name}</artifactId>
  <properties>
    <maven.compiler.source>17</maven.compiler.source>
    <maven.compiler.target>17</maven.compiler.target>
  </properties>
</project>
"""
    _recipe(tmp_path, pom_xml=pom)
    assert _run(tmp_path, monkeypatch) == EXIT_VIOLATIONS
    out = capsys.readouterr().out
    assert "::error" in out
    assert "maven.compiler.release" in out


def test_missing_properties_block_fails(tmp_path, monkeypatch, capsys):
    pom = f"""<?xml version="1.0" encoding="UTF-8"?>
<project xmlns="http://maven.apache.org/POM/4.0.0">
  <modelVersion>4.0.0</modelVersion>
  <artifactId>{tmp_path.name}</artifactId>
</project>
"""
    _recipe(tmp_path, pom_xml=pom)
    assert _run(tmp_path, monkeypatch) == EXIT_VIOLATIONS
    assert "::error" in capsys.readouterr().out


def test_non_integer_release_fails(tmp_path, monkeypatch, capsys):
    pom = f"""<?xml version="1.0" encoding="UTF-8"?>
<project xmlns="http://maven.apache.org/POM/4.0.0">
  <modelVersion>4.0.0</modelVersion>
  <artifactId>{tmp_path.name}</artifactId>
  <properties>
    <maven.compiler.release>1.8.0_292</maven.compiler.release>
  </properties>
</project>
"""
    _recipe(tmp_path, pom_xml=pom)
    assert _run(tmp_path, monkeypatch) == EXIT_VIOLATIONS
    out = capsys.readouterr().out
    assert "::error" in out
    assert "not a valid integer" in out


# ---------------------------------------------------------------------------
# Description matching rules
# ---------------------------------------------------------------------------


def test_description_matches_manifest_passes(tmp_path, monkeypatch, capsys):
    pom = f"""<?xml version="1.0" encoding="UTF-8"?>
<project xmlns="http://maven.apache.org/POM/4.0.0">
  <modelVersion>4.0.0</modelVersion>
  <artifactId>{tmp_path.name}</artifactId>
  <description>Sample Java financial advisor recipe.</description>
  <properties>
    <maven.compiler.release>17</maven.compiler.release>
  </properties>
</project>
"""
    manifest = """
description: Sample Java financial advisor recipe.
language: java
"""
    _recipe(tmp_path, pom_xml=pom, manifest_yaml=manifest)
    assert _run(tmp_path, monkeypatch) == EXIT_OK
    assert "::error" not in capsys.readouterr().out


def test_description_mismatch_fails(tmp_path, monkeypatch, capsys):
    pom = f"""<?xml version="1.0" encoding="UTF-8"?>
<project xmlns="http://maven.apache.org/POM/4.0.0">
  <modelVersion>4.0.0</modelVersion>
  <artifactId>{tmp_path.name}</artifactId>
  <description>Old description</description>
  <properties>
    <maven.compiler.release>17</maven.compiler.release>
  </properties>
</project>
"""
    manifest = """
description: New description from manifest
language: java
"""
    _recipe(tmp_path, pom_xml=pom, manifest_yaml=manifest)
    assert _run(tmp_path, monkeypatch) == EXIT_VIOLATIONS
    out = capsys.readouterr().out
    assert "::error" in out
    assert "Old description" in out
    assert "New description from manifest" in out


def test_no_description_in_pom_passes(tmp_path, monkeypatch, capsys):
    pom = f"""<?xml version="1.0" encoding="UTF-8"?>
<project xmlns="http://maven.apache.org/POM/4.0.0">
  <modelVersion>4.0.0</modelVersion>
  <artifactId>{tmp_path.name}</artifactId>
  <properties>
    <maven.compiler.release>17</maven.compiler.release>
  </properties>
</project>
"""
    manifest = """
description: Some description
language: java
"""
    _recipe(tmp_path, pom_xml=pom, manifest_yaml=manifest)
    assert _run(tmp_path, monkeypatch) == EXIT_OK
    assert "::error" not in capsys.readouterr().out


# ---------------------------------------------------------------------------
# Repositories & mirrors rules
# ---------------------------------------------------------------------------


def test_maven_central_repositories_pass(tmp_path, monkeypatch, capsys):
    pom = f"""<?xml version="1.0" encoding="UTF-8"?>
<project xmlns="http://maven.apache.org/POM/4.0.0">
  <modelVersion>4.0.0</modelVersion>
  <artifactId>{tmp_path.name}</artifactId>
  <properties>
    <maven.compiler.release>17</maven.compiler.release>
  </properties>
  <repositories>
    <repository>
      <id>central</id>
      <url>https://repo.maven.apache.org/maven2</url>
    </repository>
    <repository>
      <id>central-mirror</id>
      <url>https://repo1.maven.org/maven2/</url>
    </repository>
  </repositories>
  <pluginRepositories>
    <pluginRepository>
      <id>central-plugins</id>
      <url>https://repo.maven.apache.org/maven2</url>
    </pluginRepository>
  </pluginRepositories>
</project>
"""
    _recipe(tmp_path, pom_xml=pom)
    assert _run(tmp_path, monkeypatch) == EXIT_OK
    assert "::error" not in capsys.readouterr().out


def test_custom_repository_fails(tmp_path, monkeypatch, capsys):
    pom = f"""<?xml version="1.0" encoding="UTF-8"?>
<project xmlns="http://maven.apache.org/POM/4.0.0">
  <modelVersion>4.0.0</modelVersion>
  <artifactId>{tmp_path.name}</artifactId>
  <properties>
    <maven.compiler.release>17</maven.compiler.release>
  </properties>
  <repositories>
    <repository>
      <id>custom-repo</id>
      <url>https://jitpack.io</url>
    </repository>
  </repositories>
</project>
"""
    _recipe(tmp_path, pom_xml=pom)
    assert _run(tmp_path, monkeypatch) == EXIT_VIOLATIONS
    out = capsys.readouterr().out
    assert "::error" in out
    assert "https://jitpack.io" in out


def test_custom_mirror_fails(tmp_path, monkeypatch, capsys):
    pom = f"""<?xml version="1.0" encoding="UTF-8"?>
<project xmlns="http://maven.apache.org/POM/4.0.0">
  <modelVersion>4.0.0</modelVersion>
  <artifactId>{tmp_path.name}</artifactId>
  <properties>
    <maven.compiler.release>17</maven.compiler.release>
  </properties>
  <mirrors>
    <mirror>
      <id>internal-mirror</id>
      <url>https://mycorp.internal/maven</url>
    </mirror>
  </mirrors>
</project>
"""
    _recipe(tmp_path, pom_xml=pom)
    assert _run(tmp_path, monkeypatch) == EXIT_VIOLATIONS
    out = capsys.readouterr().out
    assert "::error" in out
    assert "https://mycorp.internal/maven" in out


# ---------------------------------------------------------------------------
# Missing file & encoding / parse errors
# ---------------------------------------------------------------------------


def test_missing_pom_xml_is_skipped(tmp_path, monkeypatch, capsys):
    assert _run(tmp_path, monkeypatch) == EXIT_OK
    assert "[SKIP]" in capsys.readouterr().out


def test_non_utf8_pom_xml_fails(tmp_path, monkeypatch, capsys):
    _recipe(tmp_path, pom_xml=b"\xff\xfe\x00\x00invalid")
    assert _run(tmp_path, monkeypatch) == EXIT_VIOLATIONS
    out = capsys.readouterr().out
    assert "::error" in out
    assert "not valid UTF-8" in out


def test_malformed_xml_pom_xml_fails(tmp_path, monkeypatch, capsys):
    _recipe(tmp_path, pom_xml="<project><unclosed>")
    assert _run(tmp_path, monkeypatch) == EXIT_VIOLATIONS
    out = capsys.readouterr().out
    assert "::error" in out
    assert "not valid XML" in out


# ---------------------------------------------------------------------------
# CLI argument handling & CI faults
# ---------------------------------------------------------------------------


def test_no_args_exits_ci_fault(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["check_recipe_pom.py"])
    assert m.main() == EXIT_CI_FAULT


def test_too_many_args_exits_ci_fault(monkeypatch):
    monkeypatch.setattr(sys, "argv", ["check_recipe_pom.py", "arg1", "arg2"])
    assert m.main() == EXIT_CI_FAULT


def test_non_directory_arg_exits_ci_fault(tmp_path, monkeypatch):
    monkeypatch.setattr(
        sys, "argv", ["check_recipe_pom.py", str(tmp_path / "nonexistent")]
    )
    assert m.main() == EXIT_CI_FAULT
