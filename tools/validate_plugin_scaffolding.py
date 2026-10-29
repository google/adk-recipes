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

r"""Validate Agent Plugins v1.0.0 directory anatomy and scaffolding.

Enforces the Agent Plugins v1.0.0 layout under plugins/<vertical>/:
plugins/<vertical>/
├── plugin.json                 # Package manifest (Agent Plugins v1.0.0)
├── mcp.json                    # Optional MCP server config
└── skills/<skill-name>/
    ├── SKILL.md                # Conversational Q-MODE installer
    ├── EVAL.yaml               # Evaluation rubrics (>=80% pass threshold)
    ├── README.md               # Quickstart, flags & architecture
    ├── manifest.yaml           # Repo governance metadata (optional)
    ├── .env.example            # Standardized environment variables
    ├── pyproject.toml          # [project].name MUST be <vertical>-<skill-name>
    ├── uv.lock                 # Public PyPI lockfile
    ├── Makefile                # make setup PROJECT_ID=... REGION=...
    ├── scripts/
    │   ├── setup.py            # Ingests data, provisions GCP infra
    │   └── cleanup.py          # Tears down GCP resources cleanly
    ├── references/             # Deep-dive docs loaded by assistants
    └── assets/                 # Demo datasets and prompt templates

Usage:
    uv run python tools/validate_plugin_scaffolding.py [scope]
    scope can be 'plugins', a specific plugin path (e.g. plugins/retail), or omitted for all plugins.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import tomllib
from pathlib import Path

import yaml

# Add tools directory to path
TOOLS_DIR = Path(__file__).resolve().parent
REPO_ROOT = TOOLS_DIR.parent
if str(TOOLS_DIR) not in sys.path:
    sys.path.insert(0, str(TOOLS_DIR))

from ci_message import Diagnostic, Doc, guard, report  # noqa: E402
from validate_manifest import repo_relative  # noqa: E402


def _parse_yaml_frontmatter(content: str) -> tuple[dict | None, str | None]:
    """Extract and parse YAML front-matter from a markdown string."""
    if not content.startswith("---"):
        return (
            None,
            "File does not start with '---' YAML front-matter delimiter.",
        )
    parts = content.split("---", 2)
    if len(parts) < 3:
        return None, "YAML front-matter is not closed with a matching '---'."
    fm_text = parts[1]
    try:
        data = yaml.safe_load(fm_text)
    except yaml.YAMLError as exc:
        return None, f"YAML front-matter is malformed: {exc}"
    if not isinstance(data, dict):
        return None, "YAML front-matter must be a mapping (key-value pairs)."
    return data, None


def validate_skill(
    skill_dir: Path,
    vertical: str,
    repo_root: Path = REPO_ROOT,
) -> list[Diagnostic]:
    """Validate a single skill inside plugins/<vertical>/skills/<skill-name>/."""
    diagnostics: list[Diagnostic] = []
    skill_name = skill_dir.name
    rel_skill = repo_relative(skill_dir, repo_root)

    # 1. SKILL.md
    skill_md = skill_dir / "SKILL.md"
    if not skill_md.is_file():
        diagnostics.append(
            Diagnostic(
                check="required-files",
                what=f"Required file 'SKILL.md' is missing in '{rel_skill}'.",
                why="Every skill must include a SKILL.md defining its conversational installer and prompts.",
                how=f"Create {rel_skill}/SKILL.md with valid YAML front-matter (name and description).",
                doc=Doc.REQUIRED_FILES,
                file=f"{rel_skill}/SKILL.md",
            )
        )
    else:
        content = skill_md.read_text(encoding="utf-8", errors="replace")
        fm, err = _parse_yaml_frontmatter(content)
        if err:
            diagnostics.append(
                Diagnostic(
                    check="skill-frontmatter",
                    what=f"'{rel_skill}/SKILL.md' has invalid front-matter: {err}",
                    why="Agent skills require YAML front-matter with 'name' and 'description'.",
                    how="Add '---\nname: <skill-name>\ndescription: ...\n---' at the top of SKILL.md.",
                    doc=Doc.REQUIRED_FILES,
                    file=f"{rel_skill}/SKILL.md",
                )
            )
        elif fm:
            if not fm.get("name"):
                diagnostics.append(
                    Diagnostic(
                        check="skill-frontmatter",
                        what=f"'{rel_skill}/SKILL.md' front-matter is missing required 'name' field.",
                        why="The skill front-matter must specify 'name'.",
                        how=f"Set 'name: {skill_name}' in {rel_skill}/SKILL.md front-matter.",
                        doc=Doc.REQUIRED_FILES,
                        file=f"{rel_skill}/SKILL.md",
                    )
                )
            if not fm.get("description"):
                diagnostics.append(
                    Diagnostic(
                        check="skill-frontmatter",
                        what=f"'{rel_skill}/SKILL.md' front-matter is missing required 'description' field.",
                        why="The skill front-matter must specify 'description'.",
                        how=f"Set 'description: ...' in {rel_skill}/SKILL.md front-matter.",
                        doc=Doc.REQUIRED_FILES,
                        file=f"{rel_skill}/SKILL.md",
                    )
                )

    # 2. EVAL.yaml (or eval.yaml)
    eval_yaml = skill_dir / "EVAL.yaml"
    if not eval_yaml.is_file():
        eval_yaml = skill_dir / "eval.yaml"
    if not eval_yaml.is_file():
        diagnostics.append(
            Diagnostic(
                check="required-files",
                what=f"Required file 'EVAL.yaml' is missing in '{rel_skill}'.",
                why="Every plugin skill must provide an EVAL.yaml declaring evaluation rubrics and criteria.",
                how=f"Add {rel_skill}/EVAL.yaml with test cases, rubrics, and evaluation assertions.",
                doc=Doc.REQUIRED_FILES,
                file=f"{rel_skill}/EVAL.yaml",
            )
        )
    else:
        try:
            eval_data = yaml.safe_load(
                eval_yaml.read_text(encoding="utf-8", errors="replace")
            )
            if not isinstance(eval_data, dict):
                diagnostics.append(
                    Diagnostic(
                        check="eval-schema",
                        what=f"'{repo_relative(eval_yaml, repo_root)}' is not a valid YAML mapping.",
                        why="EVAL.yaml must contain structured test cases, rubrics, or assertions.",
                        how="Structure EVAL.yaml with a top-level mapping, e.g. 'cases: [...]'.",
                        doc=Doc.REQUIRED_FILES,
                        file=repo_relative(eval_yaml, repo_root),
                    )
                )
            else:
                threshold = eval_data.get("threshold")
                if threshold is not None:
                    # Parse threshold float or percent string
                    num_threshold: float | None = None
                    if isinstance(threshold, (int, float)):
                        num_threshold = float(threshold)
                    elif isinstance(threshold, str) and threshold.endswith("%"):
                        try:
                            num_threshold = float(threshold[:-1]) / 100.0
                        except ValueError:
                            pass
                    if num_threshold is not None and num_threshold < 0.80:
                        diagnostics.append(
                            Diagnostic(
                                check="eval-threshold",
                                what=f"'{repo_relative(eval_yaml, repo_root)}' declares a pass threshold of {threshold} (< 80%).",
                                why="Plugin evaluation rubrics require a pass threshold of at least 80% (>= 0.80).",
                                how="Raise the evaluation threshold to at least 0.80 (80%) in EVAL.yaml.",
                                doc=Doc.REQUIRED_FILES,
                                file=repo_relative(eval_yaml, repo_root),
                            )
                        )
        except yaml.YAMLError as exc:
            diagnostics.append(
                Diagnostic(
                    check="eval-yaml-parse",
                    what=f"Failed to parse '{repo_relative(eval_yaml, repo_root)}': {exc}",
                    why="EVAL.yaml must be valid YAML.",
                    how="Fix YAML syntax errors in EVAL.yaml.",
                    doc=Doc.REQUIRED_FILES,
                    file=repo_relative(eval_yaml, repo_root),
                )
            )

    # 3. README.md
    readme_md = skill_dir / "README.md"
    if not readme_md.is_file() or readme_md.stat().st_size < 30:
        diagnostics.append(
            Diagnostic(
                check="required-files",
                what=f"Required file 'README.md' is missing or too short in '{rel_skill}'.",
                why="Every plugin skill must document quickstart, flags, and architecture.",
                how=f"Create a comprehensive README.md in {rel_skill}/README.md.",
                doc=Doc.README_MISSING,
                file=f"{rel_skill}/README.md",
            )
        )

    # 4. Optional manifest.yaml validation if present
    manifest_yaml = skill_dir / "manifest.yaml"
    if manifest_yaml.is_file():
        try:
            m_data = yaml.safe_load(
                manifest_yaml.read_text(encoding="utf-8", errors="replace")
            )
            if not isinstance(m_data, dict):
                diagnostics.append(
                    Diagnostic(
                        check="manifest-format",
                        what=f"'{rel_skill}/manifest.yaml' is not a valid YAML dictionary.",
                        why="manifest.yaml must be a valid key-value mapping.",
                        how="Ensure manifest.yaml contains valid YAML mapping.",
                        doc=Doc.MANIFEST,
                        file=f"{rel_skill}/manifest.yaml",
                    )
                )
        except yaml.YAMLError as exc:
            diagnostics.append(
                Diagnostic(
                    check="manifest-format",
                    what=f"Failed to parse '{rel_skill}/manifest.yaml': {exc}",
                    why="manifest.yaml must be valid YAML.",
                    how="Fix YAML syntax in manifest.yaml.",
                    doc=Doc.MANIFEST,
                    file=f"{rel_skill}/manifest.yaml",
                )
            )

    # 5. .env.example
    env_example = skill_dir / ".env.example"
    if not env_example.is_file() or env_example.stat().st_size == 0:
        diagnostics.append(
            Diagnostic(
                check="required-files",
                what=f"Required file '.env.example' is missing or empty in '{rel_skill}'.",
                why="Standardized environment variable declarations are required for reproducibility.",
                how=f"Add environment variable template to {rel_skill}/.env.example.",
                doc=Doc.REQUIRED_FILES,
                file=f"{rel_skill}/.env.example",
            )
        )

    # 6. pyproject.toml
    pyproject_toml = skill_dir / "pyproject.toml"
    expected_pkg_name = f"{vertical}-{skill_name}"
    if not pyproject_toml.is_file():
        diagnostics.append(
            Diagnostic(
                check="required-files",
                what=f"Required file 'pyproject.toml' is missing in '{rel_skill}'.",
                why="Every Python plugin skill must define pyproject.toml.",
                how=f"Create {rel_skill}/pyproject.toml with [project].name = '{expected_pkg_name}'.",
                doc=Doc.REQUIRED_FILES,
                file=f"{rel_skill}/pyproject.toml",
            )
        )
    else:
        try:
            data = tomllib.loads(
                pyproject_toml.read_text(encoding="utf-8", errors="replace")
            )
            project = data.get("project") or {}
            pkg_name = project.get("name")
            if pkg_name != expected_pkg_name:
                diagnostics.append(
                    Diagnostic(
                        check="project-name",
                        what=f"pyproject.toml in '{rel_skill}' has name '{pkg_name}', expected '{expected_pkg_name}'.",
                        why="Plugin skill package names MUST follow the format <vertical>-<skill-name>.",
                        how=f"Set 'name = \"{expected_pkg_name}\"' in {rel_skill}/pyproject.toml [project].",
                        doc=Doc.PROJECT_NAME,
                        file=f"{rel_skill}/pyproject.toml",
                    )
                )
        except tomllib.TOMLDecodeError as exc:
            diagnostics.append(
                Diagnostic(
                    check="pyproject-parse",
                    what=f"Failed to parse '{rel_skill}/pyproject.toml': {exc}",
                    why="pyproject.toml must be valid TOML.",
                    how="Fix syntax errors in pyproject.toml.",
                    doc=Doc.REQUIRED_FILES,
                    file=f"{rel_skill}/pyproject.toml",
                )
            )

    # 7. uv.lock
    uv_lock = skill_dir / "uv.lock"
    if not uv_lock.is_file() or uv_lock.stat().st_size == 0:
        diagnostics.append(
            Diagnostic(
                check="required-files",
                what=f"Required lockfile 'uv.lock' is missing or empty in '{rel_skill}'.",
                why="Every Python plugin skill must ship a public PyPI lockfile.",
                how=f"Run 'uv lock' inside {rel_skill}/ to generate uv.lock.",
                doc=Doc.REQUIRED_FILES,
                file=f"{rel_skill}/uv.lock",
            )
        )

    # 8. Makefile
    makefile = skill_dir / "Makefile"
    if not makefile.is_file():
        diagnostics.append(
            Diagnostic(
                check="required-files",
                what=f"Required file 'Makefile' is missing in '{rel_skill}'.",
                why="Plugin skills must provide a Makefile supporting setup (e.g. 'make setup PROJECT_ID=... REGION=...').",
                how=f"Create a Makefile in {rel_skill}/ with a 'setup' target.",
                doc=Doc.REQUIRED_FILES,
                file=f"{rel_skill}/Makefile",
            )
        )
    else:
        makefile_content = makefile.read_text(
            encoding="utf-8", errors="replace"
        )
        if not re.search(r"^setup\s*:", makefile_content, re.MULTILINE):
            diagnostics.append(
                Diagnostic(
                    check="makefile-target",
                    what=f"'{rel_skill}/Makefile' is missing the 'setup' target.",
                    why="Makefile must provide a 'setup' target for provisioning infrastructure (make setup PROJECT_ID=... REGION=...).",
                    how=f"Add a 'setup:' target to {rel_skill}/Makefile.",
                    doc=Doc.REQUIRED_FILES,
                    file=f"{rel_skill}/Makefile",
                )
            )

    # 9. scripts/ directory + setup.py + cleanup.py
    scripts_dir = skill_dir / "scripts"
    if not scripts_dir.is_dir():
        diagnostics.append(
            Diagnostic(
                check="required-dirs",
                what=f"Required directory 'scripts/' is missing in '{rel_skill}'.",
                why="Plugin skills require a scripts/ directory containing executable entry points.",
                how=f"Create {rel_skill}/scripts/ with setup.py and cleanup.py.",
                doc=Doc.REQUIRED_FILES,
                file=f"{rel_skill}/scripts",
            )
        )
    else:
        setup_py = scripts_dir / "setup.py"
        if not setup_py.is_file():
            diagnostics.append(
                Diagnostic(
                    check="required-files",
                    what=f"Required script 'setup.py' is missing in '{rel_skill}/scripts'.",
                    why="Plugin skills must provide scripts/setup.py to ingest data and provision GCP infra.",
                    how=f"Add {rel_skill}/scripts/setup.py.",
                    doc=Doc.REQUIRED_FILES,
                    file=f"{rel_skill}/scripts/setup.py",
                )
            )
        cleanup_py = scripts_dir / "cleanup.py"
        if not cleanup_py.is_file():
            diagnostics.append(
                Diagnostic(
                    check="required-files",
                    what=f"Required script 'cleanup.py' is missing in '{rel_skill}/scripts'.",
                    why="Plugin skills must provide scripts/cleanup.py to cleanly tear down GCP resources.",
                    how=f"Add {rel_skill}/scripts/cleanup.py.",
                    doc=Doc.REQUIRED_FILES,
                    file=f"{rel_skill}/scripts/cleanup.py",
                )
            )

    # 10. references/ directory
    references_dir = skill_dir / "references"
    if not references_dir.is_dir():
        diagnostics.append(
            Diagnostic(
                check="required-dirs",
                what=f"Required directory 'references/' is missing in '{rel_skill}'.",
                why="Plugin skills require a references/ directory for deep-dive docs loaded by assistants.",
                how=f"Create directory {rel_skill}/references/.",
                doc=Doc.REQUIRED_FILES,
                file=f"{rel_skill}/references",
            )
        )

    # 11. assets/ directory
    assets_dir = skill_dir / "assets"
    if not assets_dir.is_dir():
        diagnostics.append(
            Diagnostic(
                check="required-dirs",
                what=f"Required directory 'assets/' is missing in '{rel_skill}'.",
                why="Plugin skills require an assets/ directory for demo datasets and prompt templates.",
                how=f"Create directory {rel_skill}/assets/.",
                doc=Doc.REQUIRED_FILES,
                file=f"{rel_skill}/assets",
            )
        )

    return diagnostics


def validate_plugin(
    plugin_dir: Path,
    repo_root: Path = REPO_ROOT,
) -> list[Diagnostic]:
    """Validate a plugin directory at plugins/<vertical>/."""
    diagnostics: list[Diagnostic] = []
    vertical = plugin_dir.name
    rel_plugin = repo_relative(plugin_dir, repo_root)

    # 1. plugin.json
    plugin_json = plugin_dir / "plugin.json"
    if not plugin_json.is_file():
        diagnostics.append(
            Diagnostic(
                check="required-files",
                what=f"Required manifest 'plugin.json' is missing in '{rel_plugin}'.",
                why="Agent Plugins v1.0.0 requires a root plugin.json manifest.",
                how=f"Add {rel_plugin}/plugin.json conforming to Agent Plugins v1.0.0 spec.",
                doc=Doc.MANIFEST,
                file=f"{rel_plugin}/plugin.json",
            )
        )
    else:
        try:
            data = json.loads(plugin_json.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                diagnostics.append(
                    Diagnostic(
                        check="plugin-schema",
                        what=f"'{rel_plugin}/plugin.json' is not a JSON object.",
                        why="plugin.json must be a JSON object conforming to Agent Plugins v1.0.0.",
                        how="Format plugin.json as a JSON object.",
                        doc=Doc.MANIFEST,
                        file=f"{rel_plugin}/plugin.json",
                    )
                )
            else:
                for req_key in ("$schema", "name", "ownership"):
                    if req_key not in data:
                        diagnostics.append(
                            Diagnostic(
                                check="plugin-schema",
                                what=f"'{rel_plugin}/plugin.json' is missing required field '{req_key}'.",
                                why=f"Agent Plugins v1.0.0 requires the '{req_key}' field in plugin.json.",
                                how=f"Add '{req_key}' to {rel_plugin}/plugin.json.",
                                doc=Doc.MANIFEST,
                                file=f"{rel_plugin}/plugin.json",
                            )
                        )
                ownership = data.get("ownership")
                if isinstance(ownership, dict):
                    if not ownership.get("team"):
                        diagnostics.append(
                            Diagnostic(
                                check="plugin-ownership",
                                what=f"'{rel_plugin}/plugin.json' ownership is missing 'team'.",
                                why="Plugin ownership requires a team identifier.",
                                how="Set 'ownership.team' in plugin.json.",
                                doc=Doc.OWNERSHIP_PLACEHOLDER,
                                file=f"{rel_plugin}/plugin.json",
                            )
                        )
                    if not ownership.get("poc"):
                        diagnostics.append(
                            Diagnostic(
                                check="plugin-ownership",
                                what=f"'{rel_plugin}/plugin.json' ownership is missing 'poc'.",
                                why="Plugin ownership requires a POC identifier.",
                                how="Set 'ownership.poc' in plugin.json.",
                                doc=Doc.OWNERSHIP_PLACEHOLDER,
                                file=f"{rel_plugin}/plugin.json",
                            )
                        )
        except json.JSONDecodeError as exc:
            diagnostics.append(
                Diagnostic(
                    check="plugin-json-parse",
                    what=f"Failed to parse '{rel_plugin}/plugin.json': {exc}",
                    why="plugin.json must be valid JSON.",
                    how="Fix JSON syntax in plugin.json.",
                    doc=Doc.MANIFEST,
                    file=f"{rel_plugin}/plugin.json",
                )
            )

    # 2. mcp.json (optional, but if present must be valid JSON)
    mcp_json = plugin_dir / "mcp.json"
    if mcp_json.is_file():
        try:
            json.loads(mcp_json.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            diagnostics.append(
                Diagnostic(
                    check="mcp-json-parse",
                    what=f"Optional config '{rel_plugin}/mcp.json' is invalid JSON: {exc}",
                    why="If mcp.json is present, it must be valid JSON.",
                    how="Fix JSON syntax in mcp.json.",
                    doc=Doc.REQUIRED_FILES,
                    file=f"{rel_plugin}/mcp.json",
                )
            )

    # 3. skills/ directory
    skills_dir = plugin_dir / "skills"
    if not skills_dir.is_dir():
        diagnostics.append(
            Diagnostic(
                check="required-dirs",
                what=f"Required directory 'skills/' is missing in '{rel_plugin}'.",
                why="Agent Plugins v1.0.0 container must contain a 'skills/' directory.",
                how=f"Create {rel_plugin}/skills/ containing at least one skill package.",
                doc=Doc.REQUIRED_FILES,
                file=f"{rel_plugin}/skills",
            )
        )
        return diagnostics

    skill_dirs = [
        d
        for d in sorted(skills_dir.iterdir())
        if d.is_dir() and not d.name.startswith(".")
    ]
    if not skill_dirs:
        diagnostics.append(
            Diagnostic(
                check="required-dirs",
                what=f"Plugin '{rel_plugin}/skills' contains no skill directories.",
                why="A plugin container must ship at least one skill under skills/ (e.g. skills/<skill-name>/).",
                how=f"Add a skill package to {rel_plugin}/skills/<skill-name>/.",
                doc=Doc.REQUIRED_FILES,
                file=f"{rel_plugin}/skills",
            )
        )
        return diagnostics

    for skill_dir in skill_dirs:
        diagnostics.extend(validate_skill(skill_dir, vertical, repo_root))

    return diagnostics


def collect_plugins(
    scope_path: Path | None = None, repo_root: Path = REPO_ROOT
) -> list[Path]:
    """Find all plugin directories to validate."""
    plugins_root = repo_root / "plugins"

    if scope_path and scope_path != plugins_root and scope_path.is_dir():
        if scope_path.parent.name == "skills":
            return [scope_path.parent.parent]
        return [scope_path]

    if not plugins_root.is_dir():
        return []

    # Collect all top-level directories in plugins/ that have plugin.json or subdirs
    plugins = []
    for item in sorted(plugins_root.iterdir()):
        if item.is_dir() and not item.name.startswith("."):
            plugins.append(item)
    return plugins


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Validate Agent Plugins v1.0.0 scaffolding and directory anatomy."
    )
    parser.add_argument(
        "scope",
        nargs="?",
        default="plugins",
        help="Path or scope to validate (default: 'plugins')",
    )
    args = parser.parse_args()

    scope_str = args.scope.strip()
    if scope_str in ("all", "plugins"):
        scope_path = REPO_ROOT / "plugins"
    else:
        scope_path = Path(scope_str).resolve()
        if not scope_path.exists():
            # Might be relative to repo root
            scope_path = (REPO_ROOT / scope_str).resolve()

    plugin_dirs = collect_plugins(scope_path, REPO_ROOT)
    if not plugin_dirs:
        print("[INFO] No plugin directories found to validate.")
        return 0

    all_diagnostics: list[Diagnostic] = []
    print(f"Validating {len(plugin_dirs)} plugin(s) under plugins/ ...")
    for p_dir in plugin_dirs:
        rel = repo_relative(p_dir, REPO_ROOT)
        print(f"  Checking {rel} ...")
        diags = validate_plugin(p_dir, REPO_ROOT)
        all_diagnostics.extend(diags)

    return report(
        all_diagnostics,
        header="Plugins with scaffolding or anatomy problems",
        passed_message=(
            f"All {len(plugin_dirs)} plugin(s) conform to Agent Plugins v1.0.0 anatomy."
        ),
        next_step="See docs/recipe-handbook/plugins.md for layout and specification details.",
    )


if __name__ == "__main__":
    sys.exit(guard("validate_plugin_scaffolding.py", main))
