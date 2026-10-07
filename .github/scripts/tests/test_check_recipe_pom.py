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
  - <artifactId> must match folder basename (or <vertical>-<solution> for plugins)
  - <maven.compiler.release> in <properties> is canonical and <= CI pin (17)
  - <description> matches manifest.description if present
  - Repositories and mirrors point to Maven Central
  - Exit code contract (0 = pass, 1 = violations, 2 = CI fault)
  - Specimen contrib/java/time-series-forecasting fails as expected (lacks compiler release)
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
        ("core/java/time-series-forecasting", "time-series-forecasting"),
        ("./core/java/time-series-forecasting", "time-series-forecasting"),
        ("contrib/java/financial-advisor", "financial-advisor"),
        ("./plugins/retail/store-ops", "retail-store-ops"),
        ("plugins/retail/store-ops", "retail-store-ops"),
        ("time-series-forecasting", "time-series-forecasting"),
    ],
)
def test_expected_project_name_for_repo_relative_paths(path, expected):
    assert m.expected_project_name(Path(path)) == expected


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        (
            f"{MOCK_CHECKOUT}/core/java/time-series-forecasting",
            "time-series-forecasting",
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
def test_expected_project_name_for_absolute_paths_inside_repo(path, expected):
    assert (
        m.expected_project_name(Path(path), repo_root=Path(MOCK_CHECKOUT))
        == expected
    )


# ---------------------------------------------------------------------------
# Valid pom.xml
# ---------------------------------------------------------------------------

VALID_POM_TEMPLATE = """<?xml version="1.0" encoding="UTF-8"?>
<project xmlns="http://maven.apache.org/POM/4.0.0"
  xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
  xsi:schemaLocation="http://maven.apache.org/POM/4.0.0 http://maven.apache.org/xsd/maven-4.0.0.xsd">
  <modelVersion>4.0.0</modelVersion>

  <groupId>adk-agents</groupId>
  <artifactId>{name}</artifactId>
  <version>1.0</version>
  <description>{description}</description>

  <properties>
    <maven.compiler.release>{release}</maven.compiler.release>
    <project.build.sourceEncoding>UTF-8</project.build.sourceEncoding>
  </properties>

  <repositories>
    <repository>
      <id>central</id>
      <url>https://repo.maven.apache.org/maven2</url>
    </repository>
  </repositories>
</project>
"""


def test_valid_pom_passes(tmp_path, monkeypatch, capsys):
    name = tmp_path.name
    desc = "A sample recipe description."
    pom = VALID_POM_TEMPLATE.format(name=name, description=desc, release=17)
    manifest = f"name: {name}\ndescription: {desc}\nlanguage: java\n"
    _recipe(tmp_path, pom_xml=pom, manifest_yaml=manifest)
    assert _run(tmp_path, monkeypatch) == EXIT_OK
    out = capsys.readouterr().out
    assert "[PASS]" in out
    assert "::error" not in out


def test_valid_pom_without_xml_namespace_passes(tmp_path, monkeypatch):
    name = tmp_path.name
    pom = f"""<?xml version="1.0" encoding="UTF-8"?>
<project>
  <modelVersion>4.0.0</modelVersion>
  <groupId>adk-agents</groupId>
  <artifactId>{name}</artifactId>
  <version>1.0</version>
  <properties>
    <maven.compiler.release>17</maven.compiler.release>
  </properties>
</project>
"""
    _recipe(tmp_path, pom_xml=pom)
    assert _run(tmp_path, monkeypatch) == EXIT_OK


def test_valid_pom_with_lower_release_versions_passes(tmp_path, monkeypatch):
    for ver in [11, 8]:
        pom = f"""<?xml version="1.0" encoding="UTF-8"?>
<project xmlns="http://maven.apache.org/POM/4.0.0">
  <modelVersion>4.0.0</modelVersion>
  <groupId>adk-agents</groupId>
  <artifactId>{tmp_path.name}</artifactId>
  <version>1.0</version>
  <properties>
    <maven.compiler.release>{ver}</maven.compiler.release>
  </properties>
</project>
"""
        _recipe(tmp_path, pom_xml=pom)
        assert _run(tmp_path, monkeypatch) == EXIT_OK


def test_valid_pom_without_description_passes(tmp_path, monkeypatch):
    pom = f"""<?xml version="1.0" encoding="UTF-8"?>
<project xmlns="http://maven.apache.org/POM/4.0.0">
  <modelVersion>4.0.0</modelVersion>
  <groupId>adk-agents</groupId>
  <artifactId>{tmp_path.name}</artifactId>
  <version>1.0</version>
  <properties>
    <maven.compiler.release>17</maven.compiler.release>
  </properties>
</project>
"""
    _recipe(tmp_path, pom_xml=pom)
    assert _run(tmp_path, monkeypatch) == EXIT_OK


# ---------------------------------------------------------------------------
# Artifact ID rule
# ---------------------------------------------------------------------------


def test_missing_artifact_id_fails(tmp_path, monkeypatch, capsys):
    pom = """<?xml version="1.0" encoding="UTF-8"?>
<project xmlns="http://maven.apache.org/POM/4.0.0">
  <modelVersion>4.0.0</modelVersion>
  <groupId>adk-agents</groupId>
  <version>1.0</version>
  <properties>
    <maven.compiler.release>17</maven.compiler.release>
  </properties>
</project>
"""
    _recipe(tmp_path, pom_xml=pom)
    assert _run(tmp_path, monkeypatch) == EXIT_VIOLATIONS
    out = capsys.readouterr().out
    assert "[pom-artifact-id]" in out
    assert "<artifactId> is missing" in out
    assert "::error" in out


def test_mismatched_artifact_id_fails(tmp_path, monkeypatch, capsys):
    pom = """<?xml version="1.0" encoding="UTF-8"?>
<project xmlns="http://maven.apache.org/POM/4.0.0">
  <modelVersion>4.0.0</modelVersion>
  <groupId>adk-agents</groupId>
  <artifactId>completely-wrong-artifact-id</artifactId>
  <version>1.0</version>
  <properties>
    <maven.compiler.release>17</maven.compiler.release>
  </properties>
</project>
"""
    _recipe(tmp_path, pom_xml=pom)
    assert _run(tmp_path, monkeypatch) == EXIT_VIOLATIONS
    out = capsys.readouterr().out
    assert "[pom-artifact-id]" in out
    assert "<artifactId> = 'completely-wrong-artifact-id'" in out
    assert f"this recipe must declare '{tmp_path.name}'" in out
    assert "::error" in out


# ---------------------------------------------------------------------------
# Java Version Floor rule
# ---------------------------------------------------------------------------


def test_release_higher_than_ci_pin_fails(tmp_path, monkeypatch, capsys):
    pom = f"""<?xml version="1.0" encoding="UTF-8"?>
<project xmlns="http://maven.apache.org/POM/4.0.0">
  <modelVersion>4.0.0</modelVersion>
  <groupId>adk-agents</groupId>
  <artifactId>{tmp_path.name}</artifactId>
  <version>1.0</version>
  <properties>
    <maven.compiler.release>21</maven.compiler.release>
  </properties>
</project>
"""
    _recipe(tmp_path, pom_xml=pom)
    assert _run(tmp_path, monkeypatch) == EXIT_VIOLATIONS
    out = capsys.readouterr().out
    assert "[pom-java-release]" in out
    assert "exceeds CI's pinned JDK version (17)" in out
    assert "::error" in out


def test_source_and_target_without_release_fails(tmp_path, monkeypatch, capsys):
    pom = f"""<?xml version="1.0" encoding="UTF-8"?>
<project xmlns="http://maven.apache.org/POM/4.0.0">
  <modelVersion>4.0.0</modelVersion>
  <groupId>adk-agents</groupId>
  <artifactId>{tmp_path.name}</artifactId>
  <version>1.0</version>
  <properties>
    <maven.compiler.source>17</maven.compiler.source>
    <maven.compiler.target>17</maven.compiler.target>
  </properties>
</project>
"""
    _recipe(tmp_path, pom_xml=pom)
    assert _run(tmp_path, monkeypatch) == EXIT_VIOLATIONS
    out = capsys.readouterr().out
    assert "[pom-java-release]" in out
    assert (
        "<maven.compiler.source> / <maven.compiler.target> declared without <maven.compiler.release>"
        in out
    )
    assert "maven.compiler.release" in out
    assert "::error" in out


def test_missing_release_in_properties_fails(tmp_path, monkeypatch, capsys):
    pom = f"""<?xml version="1.0" encoding="UTF-8"?>
<project xmlns="http://maven.apache.org/POM/4.0.0">
  <modelVersion>4.0.0</modelVersion>
  <groupId>adk-agents</groupId>
  <artifactId>{tmp_path.name}</artifactId>
  <version>1.0</version>
  <properties>
    <project.build.sourceEncoding>UTF-8</project.build.sourceEncoding>
  </properties>
</project>
"""
    _recipe(tmp_path, pom_xml=pom)
    assert _run(tmp_path, monkeypatch) == EXIT_VIOLATIONS
    out = capsys.readouterr().out
    assert "[pom-java-release]" in out
    assert "<maven.compiler.release> is not declared" in out
    assert "::error" in out


def test_missing_properties_element_fails(tmp_path, monkeypatch, capsys):
    pom = f"""<?xml version="1.0" encoding="UTF-8"?>
<project xmlns="http://maven.apache.org/POM/4.0.0">
  <modelVersion>4.0.0</modelVersion>
  <groupId>adk-agents</groupId>
  <artifactId>{tmp_path.name}</artifactId>
  <version>1.0</version>
</project>
"""
    _recipe(tmp_path, pom_xml=pom)
    assert _run(tmp_path, monkeypatch) == EXIT_VIOLATIONS
    out = capsys.readouterr().out
    assert "[pom-java-release]" in out
    assert "<maven.compiler.release> in <properties> is missing" in out
    assert "::error" in out


def test_non_integer_release_fails(tmp_path, monkeypatch, capsys):
    pom = f"""<?xml version="1.0" encoding="UTF-8"?>
<project xmlns="http://maven.apache.org/POM/4.0.0">
  <modelVersion>4.0.0</modelVersion>
  <groupId>adk-agents</groupId>
  <artifactId>{tmp_path.name}</artifactId>
  <version>1.0</version>
  <properties>
    <maven.compiler.release>17.0-ea</maven.compiler.release>
  </properties>
</project>
"""
    _recipe(tmp_path, pom_xml=pom)
    assert _run(tmp_path, monkeypatch) == EXIT_VIOLATIONS
    out = capsys.readouterr().out
    assert "[pom-java-release]" in out
    assert "not a valid integer" in out
    assert "::error" in out


# ---------------------------------------------------------------------------
# Description rule
# ---------------------------------------------------------------------------


def test_description_matches_manifest_passes(tmp_path, monkeypatch):
    name = tmp_path.name
    desc = "  A matching recipe description.  "
    pom = f"""<?xml version="1.0" encoding="UTF-8"?>
<project xmlns="http://maven.apache.org/POM/4.0.0">
  <modelVersion>4.0.0</modelVersion>
  <groupId>adk-agents</groupId>
  <artifactId>{name}</artifactId>
  <version>1.0</version>
  <description>{desc}</description>
  <properties>
    <maven.compiler.release>17</maven.compiler.release>
  </properties>
</project>
"""
    manifest = f"name: {name}\ndescription: 'A matching recipe description.'\nlanguage: java\n"
    _recipe(tmp_path, pom_xml=pom, manifest_yaml=manifest)
    assert _run(tmp_path, monkeypatch) == EXIT_OK


def test_description_mismatch_fails(tmp_path, monkeypatch, capsys):
    name = tmp_path.name
    pom = f"""<?xml version="1.0" encoding="UTF-8"?>
<project xmlns="http://maven.apache.org/POM/4.0.0">
  <modelVersion>4.0.0</modelVersion>
  <groupId>adk-agents</groupId>
  <artifactId>{name}</artifactId>
  <version>1.0</version>
  <description>POM Description</description>
  <properties>
    <maven.compiler.release>17</maven.compiler.release>
  </properties>
</project>
"""
    manifest = f"name: {name}\ndescription: Different Manifest Description\nlanguage: java\n"
    _recipe(tmp_path, pom_xml=pom, manifest_yaml=manifest)
    assert _run(tmp_path, monkeypatch) == EXIT_VIOLATIONS
    out = capsys.readouterr().out
    assert "[pom-description]" in out
    assert "does not match manifest.description" in out
    assert "POM Description" in out
    assert "Different Manifest Description" in out
    assert "::error" in out


def test_description_without_manifest_fails(tmp_path, monkeypatch, capsys):
    pom = f"""<?xml version="1.0" encoding="UTF-8"?>
<project xmlns="http://maven.apache.org/POM/4.0.0">
  <modelVersion>4.0.0</modelVersion>
  <groupId>adk-agents</groupId>
  <artifactId>{tmp_path.name}</artifactId>
  <version>1.0</version>
  <description>Some description</description>
  <properties>
    <maven.compiler.release>17</maven.compiler.release>
  </properties>
</project>
"""
    _recipe(tmp_path, pom_xml=pom)
    assert _run(tmp_path, monkeypatch) == EXIT_VIOLATIONS
    out = capsys.readouterr().out
    assert "[pom-description]" in out
    assert "there is no" in out
    assert "manifest.yaml" in out
    assert "::error" in out


def test_description_with_invalid_yaml_manifest_fails(
    tmp_path, monkeypatch, capsys
):
    pom = f"""<?xml version="1.0" encoding="UTF-8"?>
<project xmlns="http://maven.apache.org/POM/4.0.0">
  <modelVersion>4.0.0</modelVersion>
  <groupId>adk-agents</groupId>
  <artifactId>{tmp_path.name}</artifactId>
  <version>1.0</version>
  <description>Some description</description>
  <properties>
    <maven.compiler.release>17</maven.compiler.release>
  </properties>
</project>
"""
    _recipe(tmp_path, pom_xml=pom, manifest_yaml="[unclosed list")
    assert _run(tmp_path, monkeypatch) == EXIT_VIOLATIONS
    out = capsys.readouterr().out
    assert "[pom-description]" in out
    assert "could not be read as YAML" in out
    assert "::error" in out


def test_description_with_non_dict_manifest_fails(
    tmp_path, monkeypatch, capsys
):
    pom = f"""<?xml version="1.0" encoding="UTF-8"?>
<project xmlns="http://maven.apache.org/POM/4.0.0">
  <modelVersion>4.0.0</modelVersion>
  <groupId>adk-agents</groupId>
  <artifactId>{tmp_path.name}</artifactId>
  <version>1.0</version>
  <description>Some description</description>
  <properties>
    <maven.compiler.release>17</maven.compiler.release>
  </properties>
</project>
"""
    _recipe(tmp_path, pom_xml=pom, manifest_yaml="- list item\n- another\n")
    assert _run(tmp_path, monkeypatch) == EXIT_VIOLATIONS
    out = capsys.readouterr().out
    assert "[pom-description]" in out
    assert "top level is not a mapping" in out
    assert "::error" in out


# ---------------------------------------------------------------------------
# Repositories and Mirrors rules
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "https://repo.maven.apache.org/maven2",
        "https://repo.maven.apache.org/maven2/",
        "https://repo.maven.apache.org:443/maven2",
        "http://repo.maven.apache.org/maven2",
        "https://repo1.maven.org/maven2",
        "https://repo1.maven.org/maven2/",
        "http://repo1.maven.org/maven2",
        "https://repo.maven.org/maven2",
    ],
)
def test_valid_maven_central_repositories_pass(tmp_path, monkeypatch, url):
    pom = f"""<?xml version="1.0" encoding="UTF-8"?>
<project xmlns="http://maven.apache.org/POM/4.0.0">
  <modelVersion>4.0.0</modelVersion>
  <groupId>adk-agents</groupId>
  <artifactId>{tmp_path.name}</artifactId>
  <version>1.0</version>
  <properties>
    <maven.compiler.release>17</maven.compiler.release>
  </properties>
  <repositories>
    <repository>
      <id>central</id>
      <url>{url}</url>
    </repository>
  </repositories>
</project>
"""
    _recipe(tmp_path, pom_xml=pom)
    assert _run(tmp_path, monkeypatch) == EXIT_OK


@pytest.mark.parametrize(
    "url",
    [
        "https://packages.confluent.io/maven/",
        "https://private.repo.internal/maven",
        "https://jitpack.io",
        "http://localhost:8081/nexus",
    ],
)
def test_custom_repository_fails(tmp_path, monkeypatch, capsys, url):
    pom = f"""<?xml version="1.0" encoding="UTF-8"?>
<project xmlns="http://maven.apache.org/POM/4.0.0">
  <modelVersion>4.0.0</modelVersion>
  <groupId>adk-agents</groupId>
  <artifactId>{tmp_path.name}</artifactId>
  <version>1.0</version>
  <properties>
    <maven.compiler.release>17</maven.compiler.release>
  </properties>
  <repositories>
    <repository>
      <id>custom-repo</id>
      <url>{url}</url>
    </repository>
  </repositories>
</project>
"""
    _recipe(tmp_path, pom_xml=pom)
    assert _run(tmp_path, monkeypatch) == EXIT_VIOLATIONS
    out = capsys.readouterr().out
    assert "[pom-repositories]" in out
    assert "does not point to Maven Central" in out
    assert url in out
    assert "::error" in out


def test_custom_mirror_fails(tmp_path, monkeypatch, capsys):
    pom = f"""<?xml version="1.0" encoding="UTF-8"?>
<project xmlns="http://maven.apache.org/POM/4.0.0">
  <modelVersion>4.0.0</modelVersion>
  <groupId>adk-agents</groupId>
  <artifactId>{tmp_path.name}</artifactId>
  <version>1.0</version>
  <properties>
    <maven.compiler.release>17</maven.compiler.release>
  </properties>
  <mirrors>
    <mirror>
      <id>internal-mirror</id>
      <url>https://mirror.internal/maven</url>
    </mirror>
  </mirrors>
</project>
"""
    _recipe(tmp_path, pom_xml=pom)
    assert _run(tmp_path, monkeypatch) == EXIT_VIOLATIONS
    out = capsys.readouterr().out
    assert "[pom-repositories]" in out
    assert "does not point to Maven Central" in out
    assert "https://mirror.internal/maven" in out
    assert "::error" in out


def test_custom_plugin_repository_fails(tmp_path, monkeypatch, capsys):
    pom = f"""<?xml version="1.0" encoding="UTF-8"?>
<project xmlns="http://maven.apache.org/POM/4.0.0">
  <modelVersion>4.0.0</modelVersion>
  <groupId>adk-agents</groupId>
  <artifactId>{tmp_path.name}</artifactId>
  <version>1.0</version>
  <properties>
    <maven.compiler.release>17</maven.compiler.release>
  </properties>
  <pluginRepositories>
    <pluginRepository>
      <id>custom-plugins</id>
      <url>https://plugins.internal/maven</url>
    </pluginRepository>
  </pluginRepositories>
</project>
"""
    _recipe(tmp_path, pom_xml=pom)
    assert _run(tmp_path, monkeypatch) == EXIT_VIOLATIONS
    out = capsys.readouterr().out
    assert "[pom-repositories]" in out
    assert "does not point to Maven Central" in out
    assert "::error" in out


# ---------------------------------------------------------------------------
# File handling & parsing
# ---------------------------------------------------------------------------


def test_missing_pom_xml_is_skipped(tmp_path, monkeypatch, capsys):
    assert _run(tmp_path, monkeypatch) == EXIT_OK
    out = capsys.readouterr().out
    assert "[SKIP]" in out


def test_invalid_xml_fails(tmp_path, monkeypatch, capsys):
    _recipe(tmp_path, pom_xml="<project><unclosed></project>")
    assert _run(tmp_path, monkeypatch) == EXIT_VIOLATIONS
    out = capsys.readouterr().out
    assert "[pom-parse]" in out
    assert "not valid XML" in out
    assert "::error" in out


def test_xml_entity_expansion_fails(tmp_path, monkeypatch, capsys):
    xml_bomb = """<?xml version="1.0"?>
<!DOCTYPE lolz [
 <!ENTITY lol "lol">
 <!ELEMENT lolz (#PCDATA)>
 <!ENTITY lol1 "&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;">
]>
<project><artifactId>&lol1;</artifactId></project>"""
    _recipe(tmp_path, pom_xml=xml_bomb)
    assert _run(tmp_path, monkeypatch) == EXIT_VIOLATIONS
    out = capsys.readouterr().out
    assert "[pom-parse]" in out
    assert "EntitiesForbidden" in out or "not valid XML" in out
    assert "::error" in out


def test_invalid_utf8_fails(tmp_path, monkeypatch, capsys):
    _recipe(tmp_path, pom_xml=b"\xff\xfe\x00\x00")
    assert _run(tmp_path, monkeypatch) == EXIT_VIOLATIONS
    out = capsys.readouterr().out
    assert "[pom-parse]" in out
    assert "not valid UTF-8" in out
    assert "::error" in out


# ---------------------------------------------------------------------------
# CLI error handling & CI fault
# ---------------------------------------------------------------------------


def test_main_with_no_args_exits_ci_fault(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["check_recipe_pom.py"])
    assert m.main() == EXIT_CI_FAULT
    out = capsys.readouterr().out
    assert "[ci-fault]" in out
    assert "invoked with 0 argument(s)" in out


def test_main_with_too_many_args_exits_ci_fault(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["check_recipe_pom.py", "arg1", "arg2"])
    assert m.main() == EXIT_CI_FAULT
    out = capsys.readouterr().out
    assert "[ci-fault]" in out
    assert "invoked with 2 argument(s)" in out


def test_main_with_non_directory_exits_ci_fault(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(
        sys,
        "argv",
        ["check_recipe_pom.py", str(tmp_path / "nonexistent")],
    )
    assert m.main() == EXIT_CI_FAULT
    out = capsys.readouterr().out
    assert "[ci-fault]" in out
    assert "is not a directory" in out


def test_unexpected_crash_is_ci_fault(tmp_path, monkeypatch, capsys):
    def boom(*_args, **_kwargs):
        raise RuntimeError("simulated bug")

    monkeypatch.setattr(m, "check_artifact_id", boom)
    pom = f"""<?xml version="1.0" encoding="UTF-8"?>
<project xmlns="http://maven.apache.org/POM/4.0.0">
  <artifactId>{tmp_path.name}</artifactId>
  <properties>
    <maven.compiler.release>17</maven.compiler.release>
  </properties>
</project>
"""
    _recipe(tmp_path, pom_xml=pom)
    assert _run(tmp_path, monkeypatch) == EXIT_CI_FAULT
    out = capsys.readouterr().out
    assert "RuntimeError: simulated bug" in out
    assert "::error file=" not in out


# ---------------------------------------------------------------------------
# Real specimen: contrib/java/time-series-forecasting
# ---------------------------------------------------------------------------


def test_specimen_time_series_forecasting_fails(monkeypatch, capsys):
    repo_root = Path(__file__).resolve().parents[3]
    specimen = repo_root / "contrib" / "java" / "time-series-forecasting"
    if not specimen.is_dir():
        pytest.skip(f"specimen {specimen} does not exist in this clone")
    monkeypatch.setattr(sys, "argv", ["check_recipe_pom.py", str(specimen)])
    # Fails because time-series-forecasting sets source/target instead of maven.compiler.release
    assert m.main() == EXIT_VIOLATIONS
    out = capsys.readouterr().out
    assert "[pom-java-release]" in out
