#!/usr/bin/env python3
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

from pathlib import Path

import validate_plugin_scaffolding as vps


def _create_minimal_valid_plugin(
    base_dir: Path, vertical: str = "retail", skill: str = "product-search"
) -> Path:
    plugin_dir = base_dir / "plugins" / vertical
    plugin_dir.mkdir(parents=True, exist_ok=True)

    # plugin.json
    (plugin_dir / "plugin.json").write_text(
        f'{{"$schema": "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json", "name": "{vertical}", "version": "1.0.0", "ownership": {{"team": "FDE", "poc": "poc-user"}}}}\n'
    )

    skill_dir = plugin_dir / "skills" / skill
    skill_dir.mkdir(parents=True, exist_ok=True)

    # SKILL.md
    (skill_dir / "SKILL.md").write_text(
        f"---\nname: {skill}\ndescription: Test skill description.\n---\n# {skill}\n"
    )

    # EVAL.yaml
    (skill_dir / "EVAL.yaml").write_text(
        "threshold: 0.85\ncases:\n  - id: test_case\n    prompt: 'hello'\n    rubric:\n      - 'The response MUST reply.'\n"
    )

    # README.md
    (skill_dir / "README.md").write_text(
        "# Test Skill\nQuickstart and architecture documentation.\n"
    )

    # .env.example
    (skill_dir / ".env.example").write_text("PROJECT_ID=my-project\n")

    # pyproject.toml
    (skill_dir / "pyproject.toml").write_text(
        f'[project]\nname = "{vertical}-{skill}"\nversion = "0.1.0"\n'
    )

    # uv.lock
    (skill_dir / "uv.lock").write_text("version = 1\nrevision = 1\n")

    # Makefile
    (skill_dir / "Makefile").write_text("setup:\n\t@echo 'Setting up'\n")

    # scripts/
    scripts_dir = skill_dir / "scripts"
    scripts_dir.mkdir(exist_ok=True)
    (scripts_dir / "setup.py").write_text("# setup script\n")
    (scripts_dir / "cleanup.py").write_text("# cleanup script\n")

    # references/ and assets/
    (skill_dir / "references").mkdir(exist_ok=True)
    (skill_dir / "assets").mkdir(exist_ok=True)

    return plugin_dir


# ==============================================================================
# Positive Tests
# ==============================================================================


def test_valid_plugin_passes(tmp_path: Path):
    plugin_dir = _create_minimal_valid_plugin(tmp_path)
    diags = vps.validate_plugin(plugin_dir, tmp_path)
    assert not diags, f"Expected no diagnostics, got: {[d.what for d in diags]}"


def test_valid_plugin_with_optional_mcp_passes(tmp_path: Path):
    plugin_dir = _create_minimal_valid_plugin(tmp_path)
    (plugin_dir / "mcp.json").write_text('{"mcpServers": {}}\n')
    diags = vps.validate_plugin(plugin_dir, tmp_path)
    assert not diags, f"Expected no diagnostics, got: {[d.what for d in diags]}"


def test_real_plugins_pass():
    real_plugins = vps.collect_plugins(vps.REPO_ROOT / "plugins", vps.REPO_ROOT)
    assert len(real_plugins) > 0
    all_diags = []
    for p in real_plugins:
        all_diags.extend(vps.validate_plugin(p, vps.REPO_ROOT))
    assert not all_diags, f"Real plugins in repo had errors: {[d.what for d in all_diags]}"


# ==============================================================================
# Negative Tests: Root / Container Constraints
# ==============================================================================


def test_missing_plugin_json_fails(tmp_path: Path):
    plugin_dir = _create_minimal_valid_plugin(tmp_path)
    (plugin_dir / "plugin.json").unlink()
    diags = vps.validate_plugin(plugin_dir, tmp_path)
    assert any("plugin.json" in d.what for d in diags)


def test_malformed_plugin_json_fails(tmp_path: Path):
    plugin_dir = _create_minimal_valid_plugin(tmp_path)
    (plugin_dir / "plugin.json").write_text("{bad json")
    diags = vps.validate_plugin(plugin_dir, tmp_path)
    assert any("Failed to parse" in d.what and "plugin.json" in d.what for d in diags)


def test_missing_schema_or_ownership_fails(tmp_path: Path):
    plugin_dir = _create_minimal_valid_plugin(tmp_path)
    (plugin_dir / "plugin.json").write_text('{"name": "retail"}\n')
    diags = vps.validate_plugin(plugin_dir, tmp_path)
    assert any("missing required field '$schema'" in d.what for d in diags)
    assert any("missing required field 'ownership'" in d.what for d in diags)


def test_missing_ownership_poc_fails(tmp_path: Path):
    plugin_dir = _create_minimal_valid_plugin(tmp_path)
    (plugin_dir / "plugin.json").write_text(
        '{"$schema": "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json", "name": "retail", "ownership": {"team": "FDE"}}\n'
    )
    diags = vps.validate_plugin(plugin_dir, tmp_path)
    assert any("ownership is missing 'poc'" in d.what for d in diags)


def test_malformed_optional_mcp_json_fails(tmp_path: Path):
    plugin_dir = _create_minimal_valid_plugin(tmp_path)
    (plugin_dir / "mcp.json").write_text("{bad mcp")
    diags = vps.validate_plugin(plugin_dir, tmp_path)
    assert any("mcp.json" in d.what and "invalid JSON" in d.what for d in diags)


def test_missing_skills_directory_fails(tmp_path: Path):
    plugin_dir = _create_minimal_valid_plugin(tmp_path)
    # Remove entire skills directory
    for item in (plugin_dir / "skills").rglob("*"):
        if item.is_file():
            item.unlink()
    for item in sorted((plugin_dir / "skills").rglob("*"), reverse=True):
        if item.is_dir():
            item.rmdir()
    (plugin_dir / "skills").rmdir()

    diags = vps.validate_plugin(plugin_dir, tmp_path)
    assert any("Required directory 'skills/' is missing" in d.what for d in diags)


def test_empty_skills_directory_fails(tmp_path: Path):
    plugin_dir = _create_minimal_valid_plugin(tmp_path)
    skill_dir = plugin_dir / "skills" / "product-search"
    for item in skill_dir.rglob("*"):
        if item.is_file():
            item.unlink()
    for item in sorted(skill_dir.rglob("*"), reverse=True):
        if item.is_dir():
            item.rmdir()
    skill_dir.rmdir()

    diags = vps.validate_plugin(plugin_dir, tmp_path)
    assert any("contains no skill directories" in d.what for d in diags)


# ==============================================================================
# Negative Tests: Skill Components
# ==============================================================================


def test_missing_skill_md_fails(tmp_path: Path):
    plugin_dir = _create_minimal_valid_plugin(tmp_path)
    (plugin_dir / "skills" / "product-search" / "SKILL.md").unlink()
    diags = vps.validate_plugin(plugin_dir, tmp_path)
    assert any("SKILL.md" in d.what for d in diags)


def test_skill_md_missing_frontmatter_fails(tmp_path: Path):
    plugin_dir = _create_minimal_valid_plugin(tmp_path)
    (plugin_dir / "skills" / "product-search" / "SKILL.md").write_text("# No frontmatter\n")
    diags = vps.validate_plugin(plugin_dir, tmp_path)
    assert any("invalid front-matter" in d.what for d in diags)


def test_skill_md_missing_description_in_frontmatter_fails(tmp_path: Path):
    plugin_dir = _create_minimal_valid_plugin(tmp_path)
    (plugin_dir / "skills" / "product-search" / "SKILL.md").write_text(
        "---\nname: product-search\n---\n# Content\n"
    )
    diags = vps.validate_plugin(plugin_dir, tmp_path)
    assert any("missing required 'description'" in d.what for d in diags)


def test_missing_eval_yaml_fails(tmp_path: Path):
    plugin_dir = _create_minimal_valid_plugin(tmp_path)
    (plugin_dir / "skills" / "product-search" / "EVAL.yaml").unlink()
    diags = vps.validate_plugin(plugin_dir, tmp_path)
    assert any("EVAL.yaml" in d.what for d in diags)


def test_low_eval_threshold_fails(tmp_path: Path):
    plugin_dir = _create_minimal_valid_plugin(tmp_path)
    skill_dir = plugin_dir / "skills" / "product-search"
    (skill_dir / "EVAL.yaml").write_text("threshold: 0.70\ncases: []\n")
    diags = vps.validate_plugin(plugin_dir, tmp_path)
    assert any("threshold" in d.what and "80%" in d.what for d in diags)


def test_missing_readme_md_fails(tmp_path: Path):
    plugin_dir = _create_minimal_valid_plugin(tmp_path)
    (plugin_dir / "skills" / "product-search" / "README.md").unlink()
    diags = vps.validate_plugin(plugin_dir, tmp_path)
    assert any("README.md" in d.what for d in diags)


def test_empty_env_example_fails(tmp_path: Path):
    plugin_dir = _create_minimal_valid_plugin(tmp_path)
    (plugin_dir / "skills" / "product-search" / ".env.example").write_text("")
    diags = vps.validate_plugin(plugin_dir, tmp_path)
    assert any(".env.example" in d.what and "empty" in d.what for d in diags)


def test_missing_pyproject_toml_fails(tmp_path: Path):
    plugin_dir = _create_minimal_valid_plugin(tmp_path)
    (plugin_dir / "skills" / "product-search" / "pyproject.toml").unlink()
    diags = vps.validate_plugin(plugin_dir, tmp_path)
    assert any("pyproject.toml" in d.what for d in diags)


def test_invalid_project_name_fails(tmp_path: Path):
    plugin_dir = _create_minimal_valid_plugin(tmp_path, "retail", "product-search")
    skill_dir = plugin_dir / "skills" / "product-search"
    (skill_dir / "pyproject.toml").write_text('[project]\nname = "wrong-name"\n')
    diags = vps.validate_plugin(plugin_dir, tmp_path)
    assert any("retail-product-search" in d.what for d in diags)


def test_missing_uv_lock_fails(tmp_path: Path):
    plugin_dir = _create_minimal_valid_plugin(tmp_path)
    (plugin_dir / "skills" / "product-search" / "uv.lock").unlink()
    diags = vps.validate_plugin(plugin_dir, tmp_path)
    assert any("uv.lock" in d.what for d in diags)


def test_missing_makefile_fails(tmp_path: Path):
    plugin_dir = _create_minimal_valid_plugin(tmp_path)
    (plugin_dir / "skills" / "product-search" / "Makefile").unlink()
    diags = vps.validate_plugin(plugin_dir, tmp_path)
    assert any("Makefile" in d.what for d in diags)


def test_makefile_missing_setup_target_fails(tmp_path: Path):
    plugin_dir = _create_minimal_valid_plugin(tmp_path)
    skill_dir = plugin_dir / "skills" / "product-search"
    (skill_dir / "Makefile").write_text("test:\n\tpytest\n")
    diags = vps.validate_plugin(plugin_dir, tmp_path)
    assert any("setup" in d.what for d in diags)


def test_missing_scripts_directory_fails(tmp_path: Path):
    plugin_dir = _create_minimal_valid_plugin(tmp_path)
    skill_dir = plugin_dir / "skills" / "product-search"
    (skill_dir / "scripts" / "setup.py").unlink()
    (skill_dir / "scripts" / "cleanup.py").unlink()
    (skill_dir / "scripts").rmdir()
    diags = vps.validate_plugin(plugin_dir, tmp_path)
    assert any("Required directory 'scripts/' is missing" in d.what for d in diags)


def test_missing_setup_script_fails(tmp_path: Path):
    plugin_dir = _create_minimal_valid_plugin(tmp_path)
    skill_dir = plugin_dir / "skills" / "product-search"
    (skill_dir / "scripts" / "setup.py").unlink()
    diags = vps.validate_plugin(plugin_dir, tmp_path)
    assert any("setup.py" in d.what for d in diags)


def test_missing_cleanup_script_fails(tmp_path: Path):
    plugin_dir = _create_minimal_valid_plugin(tmp_path)
    skill_dir = plugin_dir / "skills" / "product-search"
    (skill_dir / "scripts" / "cleanup.py").unlink()
    diags = vps.validate_plugin(plugin_dir, tmp_path)
    assert any("cleanup.py" in d.what for d in diags)


def test_missing_references_directory_fails(tmp_path: Path):
    plugin_dir = _create_minimal_valid_plugin(tmp_path)
    skill_dir = plugin_dir / "skills" / "product-search"
    (skill_dir / "references").rmdir()
    diags = vps.validate_plugin(plugin_dir, tmp_path)
    assert any("Required directory 'references/' is missing" in d.what for d in diags)


def test_missing_assets_directory_fails(tmp_path: Path):
    plugin_dir = _create_minimal_valid_plugin(tmp_path)
    skill_dir = plugin_dir / "skills" / "product-search"
    (skill_dir / "assets").rmdir()
    diags = vps.validate_plugin(plugin_dir, tmp_path)
    assert any("Required directory 'assets/' is missing" in d.what for d in diags)
