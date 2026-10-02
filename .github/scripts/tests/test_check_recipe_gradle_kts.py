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
"""Unit tests for check_recipe_gradle_kts.py.

These tests pin the metadata rules for Kotlin recipes:
  - JVM toolchain declaration (jvmToolchain or JavaLanguageVersion) <= CI pin (17)
  - Repositories block requiring mavenCentral() and forbidding private maven repos
  - Optional settings.gradle.kts declaring matching rootProject.name
  - Exit code contract (0 = pass, 1 = violations, 2 = CI fault)
  - Live specimen core/kotlin/llm-auditor passing clean
"""

from __future__ import annotations

import sys
from pathlib import Path

import check_recipe_gradle_kts as m
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
    build_gradle_kts: str | bytes | None = None,
    settings_gradle_kts: str | bytes | None = None,
) -> Path:
    if build_gradle_kts is not None:
        _write_file(tmp_path / "build.gradle.kts", build_gradle_kts)
    if settings_gradle_kts is not None:
        _write_file(tmp_path / "settings.gradle.kts", settings_gradle_kts)
    return tmp_path


def _run(tmp_path: Path, monkeypatch) -> int:
    monkeypatch.setattr(
        sys, "argv", ["check_recipe_gradle_kts.py", str(tmp_path)]
    )
    return m.main()


# ---------------------------------------------------------------------------
# expected_project_name
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("core/kotlin/llm-auditor", "llm-auditor"),
        ("./core/kotlin/llm-auditor", "llm-auditor"),
        ("contrib/kotlin/financial-advisor", "financial-advisor"),
        ("./plugins/retail/store-ops", "retail-store-ops"),
        ("plugins/retail/store-ops", "retail-store-ops"),
        ("llm-auditor", "llm-auditor"),
    ],
)
def test_expected_project_name_for_repo_relative_paths(path, expected):
    assert m.expected_project_name(Path(path)) == expected


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        (
            f"{MOCK_CHECKOUT}/core/kotlin/llm-auditor",
            "llm-auditor",
        ),
        (
            f"{MOCK_CHECKOUT}/contrib/kotlin/financial-advisor",
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
# JVM toolchain
# ---------------------------------------------------------------------------


VALID_BUILD_GRADLE_KTS_TOOLCHAIN = """
plugins {
    kotlin("jvm") version "2.1.20"
}

repositories {
    mavenCentral()
}

kotlin {
    jvmToolchain(17)
}
"""

VALID_BUILD_GRADLE_KTS_LANGUAGE_VERSION = """
plugins {
    kotlin("jvm") version "2.1.20"
}

repositories {
    mavenCentral()
}

kotlin {
    jvmToolchain {
        languageVersion.set(JavaLanguageVersion.of(17))
    }
}
"""


def test_jvm_toolchain_form_1_passes(tmp_path, monkeypatch):
    _recipe(tmp_path, build_gradle_kts=VALID_BUILD_GRADLE_KTS_TOOLCHAIN)
    assert _run(tmp_path, monkeypatch) == EXIT_OK


def test_jvm_toolchain_form_1_with_spaces_passes(tmp_path, monkeypatch):
    content = """
    kotlin {
        jvmToolchain( 17 )
    }
    """
    _recipe(tmp_path, build_gradle_kts=content)
    assert _run(tmp_path, monkeypatch) == EXIT_OK


def test_jvm_toolchain_form_2_passes(tmp_path, monkeypatch):
    _recipe(tmp_path, build_gradle_kts=VALID_BUILD_GRADLE_KTS_LANGUAGE_VERSION)
    assert _run(tmp_path, monkeypatch) == EXIT_OK


def test_jvm_toolchain_lower_than_ci_pin_passes(tmp_path, monkeypatch):
    content = """
    kotlin {
        jvmToolchain(11)
    }
    """
    _recipe(tmp_path, build_gradle_kts=content)
    assert _run(tmp_path, monkeypatch) == EXIT_OK


def test_jvm_toolchain_higher_than_ci_pin_fails(tmp_path, monkeypatch, capsys):
    content = """
    kotlin {
        jvmToolchain(21)
    }
    """
    _recipe(tmp_path, build_gradle_kts=content)
    assert _run(tmp_path, monkeypatch) == EXIT_VIOLATIONS
    out = capsys.readouterr().out
    assert "exceeds CI's pinned JDK version (17)" in out
    assert "::error" in out


def test_jvm_language_version_higher_than_ci_pin_fails(
    tmp_path, monkeypatch, capsys
):
    content = """
    kotlin {
        jvmToolchain {
            languageVersion.set(JavaLanguageVersion.of(21))
        }
    }
    """
    _recipe(tmp_path, build_gradle_kts=content)
    assert _run(tmp_path, monkeypatch) == EXIT_VIOLATIONS
    out = capsys.readouterr().out
    assert "exceeds CI's pinned JDK version (17)" in out
    assert "::error" in out


def test_missing_jvm_toolchain_fails(tmp_path, monkeypatch, capsys):
    content = """
    plugins {
        kotlin("jvm") version "2.1.20"
    }

    repositories {
        mavenCentral()
    }
    """
    _recipe(tmp_path, build_gradle_kts=content)
    assert _run(tmp_path, monkeypatch) == EXIT_VIOLATIONS
    out = capsys.readouterr().out
    assert "JVM toolchain is not declared" in out
    assert "::error" in out


def test_commented_out_jvm_toolchain_fails(tmp_path, monkeypatch, capsys):
    content = """
    // jvmToolchain(17)
    /*
    kotlin {
        jvmToolchain(17)
    }
    */
    """
    _recipe(tmp_path, build_gradle_kts=content)
    assert _run(tmp_path, monkeypatch) == EXIT_VIOLATIONS
    out = capsys.readouterr().out
    assert "JVM toolchain is not declared" in out


# ---------------------------------------------------------------------------
# Repositories block
# ---------------------------------------------------------------------------


def test_repositories_block_absent_passes(tmp_path, monkeypatch):
    content = """
    kotlin {
        jvmToolchain(17)
    }
    """
    _recipe(tmp_path, build_gradle_kts=content)
    assert _run(tmp_path, monkeypatch) == EXIT_OK


def test_repositories_with_maven_central_passes(tmp_path, monkeypatch):
    content = """
    repositories {
        mavenCentral()
    }
    kotlin {
        jvmToolchain(17)
    }
    """
    _recipe(tmp_path, build_gradle_kts=content)
    assert _run(tmp_path, monkeypatch) == EXIT_OK


def test_repositories_with_google_and_maven_central_passes(
    tmp_path, monkeypatch
):
    content = """
    repositories {
        google()
        mavenCentral()
    }
    kotlin {
        jvmToolchain(17)
    }
    """
    _recipe(tmp_path, build_gradle_kts=content)
    assert _run(tmp_path, monkeypatch) == EXIT_OK


def test_repositories_missing_maven_central_fails(
    tmp_path, monkeypatch, capsys
):
    content = """
    repositories {
        google()
    }
    kotlin {
        jvmToolchain(17)
    }
    """
    _recipe(tmp_path, build_gradle_kts=content)
    assert _run(tmp_path, monkeypatch) == EXIT_VIOLATIONS
    out = capsys.readouterr().out
    assert "does not declare `mavenCentral()`" in out
    assert "::error" in out


@pytest.mark.parametrize(
    "maven_decl",
    [
        'maven(url = "https://private.repo.internal/maven")',
        'maven("https://private.repo.internal/maven")',
        'maven(url = uri("https://private.repo.internal/maven"))',
        'maven(uri("https://private.repo.internal/maven"))',
        'maven { url = uri("https://private.repo.internal/maven") }',
        'maven {\n        url = uri("https://private.repo.internal/maven")\n    }',
    ],
)
def test_repositories_with_private_maven_fails(
    tmp_path, monkeypatch, capsys, maven_decl
):
    content = f"""
    repositories {{
        mavenCentral()
        {maven_decl}
    }}
    kotlin {{
        jvmToolchain(17)
    }}
    """
    _recipe(tmp_path, build_gradle_kts=content)
    assert _run(tmp_path, monkeypatch) == EXIT_VIOLATIONS
    out = capsys.readouterr().out
    assert "references a custom maven repository" in out
    assert "::error" in out


def test_commented_private_maven_passes(tmp_path, monkeypatch):
    content = """
    repositories {
        mavenCentral()
        // maven(url = "https://private.repo.internal/maven")
        /*
        maven { url = uri("https://private.repo.internal/maven") }
        */
    }
    kotlin {
        jvmToolchain(17)
    }
    """
    _recipe(tmp_path, build_gradle_kts=content)
    assert _run(tmp_path, monkeypatch) == EXIT_OK


def test_nested_publishing_repositories_allowed(tmp_path, monkeypatch):
    content = """
    repositories {
        mavenCentral()
    }

    publishing {
        repositories {
            maven {
                url = uri("https://internal.pkg.dev/company-registry")
            }
        }
    }

    kotlin {
        jvmToolchain(17)
    }
    """
    _recipe(tmp_path, build_gradle_kts=content)
    assert _run(tmp_path, monkeypatch) == EXIT_OK


def test_string_literal_with_braces_in_repositories_passes(
    tmp_path, monkeypatch
):
    content = """
    repositories {
        mavenCentral()
        val template = "nested { brace } in string"
    }
    kotlin {
        jvmToolchain(17)
    }
    """
    _recipe(tmp_path, build_gradle_kts=content)
    assert _run(tmp_path, monkeypatch) == EXIT_OK


# ---------------------------------------------------------------------------
# settings.gradle.kts / rootProject.name
# ---------------------------------------------------------------------------


def test_missing_settings_gradle_kts_is_skipped(tmp_path, monkeypatch):
    _recipe(tmp_path, build_gradle_kts=VALID_BUILD_GRADLE_KTS_TOOLCHAIN)
    assert _run(tmp_path, monkeypatch) == EXIT_OK


@pytest.mark.parametrize(
    "settings_content",
    [
        'rootProject.name = "{name}"',
        "rootProject.name = '{name}'",
        'rootProject.name.set("{name}")',
        "rootProject.name.set('{name}')",
        'rootProject.name  =  "{name}"',
    ],
)
def test_matching_root_project_name_passes(
    tmp_path, monkeypatch, settings_content
):
    name = tmp_path.name
    _recipe(
        tmp_path,
        build_gradle_kts=VALID_BUILD_GRADLE_KTS_TOOLCHAIN,
        settings_gradle_kts=settings_content.format(name=name),
    )
    assert _run(tmp_path, monkeypatch) == EXIT_OK


def test_mismatched_root_project_name_fails(tmp_path, monkeypatch, capsys):
    _recipe(
        tmp_path,
        build_gradle_kts=VALID_BUILD_GRADLE_KTS_TOOLCHAIN,
        settings_gradle_kts='rootProject.name = "completely-wrong-name"',
    )
    assert _run(tmp_path, monkeypatch) == EXIT_VIOLATIONS
    out = capsys.readouterr().out
    assert "rootProject.name = 'completely-wrong-name'" in out
    assert f"this recipe must declare '{tmp_path.name}'" in out
    assert "::error" in out


def test_missing_root_project_name_in_settings_fails(
    tmp_path, monkeypatch, capsys
):
    _recipe(
        tmp_path,
        build_gradle_kts=VALID_BUILD_GRADLE_KTS_TOOLCHAIN,
        settings_gradle_kts="// empty settings file\n",
    )
    assert _run(tmp_path, monkeypatch) == EXIT_VIOLATIONS
    out = capsys.readouterr().out
    assert "`rootProject.name` is not declared" in out
    assert "::error" in out


# ---------------------------------------------------------------------------
# File handling & encoding
# ---------------------------------------------------------------------------


def test_missing_build_gradle_kts_exits_ok(tmp_path, monkeypatch, capsys):
    assert _run(tmp_path, monkeypatch) == EXIT_OK
    out = capsys.readouterr().out
    assert "[SKIP]" in out


def test_invalid_utf8_in_build_gradle_kts_fails(tmp_path, monkeypatch, capsys):
    _recipe(tmp_path, build_gradle_kts=b"\xff\xfe\x00\x00")
    assert _run(tmp_path, monkeypatch) == EXIT_VIOLATIONS
    out = capsys.readouterr().out
    assert "is not valid UTF-8" in out
    assert "::error" in out


def test_invalid_utf8_in_settings_gradle_kts_fails(
    tmp_path, monkeypatch, capsys
):
    _recipe(
        tmp_path,
        build_gradle_kts=VALID_BUILD_GRADLE_KTS_TOOLCHAIN,
        settings_gradle_kts=b"\xff\xfe\x00\x00",
    )
    assert _run(tmp_path, monkeypatch) == EXIT_VIOLATIONS
    out = capsys.readouterr().out
    assert "is not valid UTF-8" in out
    assert "::error" in out


# ---------------------------------------------------------------------------
# CLI error handling
# ---------------------------------------------------------------------------


def test_main_with_no_args_exits_ci_fault(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["check_recipe_gradle_kts.py"])
    assert m.main() == EXIT_CI_FAULT
    out = capsys.readouterr().out
    assert "[ci-fault]" in out
    assert "invoked with 0 argument(s)" in out


def test_main_with_too_many_args_exits_ci_fault(monkeypatch, capsys):
    monkeypatch.setattr(
        sys, "argv", ["check_recipe_gradle_kts.py", "arg1", "arg2"]
    )
    assert m.main() == EXIT_CI_FAULT
    out = capsys.readouterr().out
    assert "[ci-fault]" in out
    assert "invoked with 2 argument(s)" in out


def test_main_with_non_directory_exits_ci_fault(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(
        sys,
        "argv",
        ["check_recipe_gradle_kts.py", str(tmp_path / "nonexistent")],
    )
    assert m.main() == EXIT_CI_FAULT
    out = capsys.readouterr().out
    assert "[ci-fault]" in out
    assert "is not a directory" in out


# ---------------------------------------------------------------------------
# Real specimen: core/kotlin/llm-auditor
# ---------------------------------------------------------------------------


def test_real_specimen_core_kotlin_llm_auditor_passes(monkeypatch):
    repo_root = Path(__file__).resolve().parents[3]
    specimen = repo_root / "core" / "kotlin" / "llm-auditor"
    if not specimen.is_dir():
        pytest.skip(f"specimen {specimen} does not exist in this clone")
    monkeypatch.setattr(
        sys, "argv", ["check_recipe_gradle_kts.py", str(specimen)]
    )
    assert m.main() == EXIT_OK
