"""House-rule checks, plus the three traps I fell into running them by hand.

The traps matter more than the happy paths: a false "this will fail CI" is the
most expensive comment this skill can produce.
"""

import json
import os
import textwrap
from pathlib import Path

import check_house_rules as chr
import pytest


def recipe(tmp_path, name="my-recipe", **files):
    root = tmp_path / "contrib" / "python" / name
    root.mkdir(parents=True)
    for rel, text in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(textwrap.dedent(text), encoding="utf-8")
    return str(tmp_path), f"contrib/python/{name}"


def rules(out):
    return {f["rule"] for f in out}


# ------------------------------------------------------- the three traps


def test_editable_self_reference_is_not_a_violation(tmp_path):
    """TRAP: `source = { editable = "." }` is the recipe's OWN package.

    A naive grep for `editable` flags it. I nearly filed this on PR #2373.
    """
    root, rel = recipe(
        tmp_path,
        **{
            "pyproject.toml": '[project]\nname = "my-recipe"\n',
            "uv.lock": '[[package]]\nname = "my-recipe"\nsource = { editable = "." }\n',
        },
    )
    out = []
    chr.check_uv_lock(out, root, rel, "my-recipe", "my-recipe")
    assert out == []


def test_third_party_editable_source_is_a_violation(tmp_path):
    root, rel = recipe(
        tmp_path,
        **{
            "uv.lock": '[[package]]\nname = "other-lib"\nsource = { editable = "../x" }\n'
        },
    )
    out = []
    chr.check_uv_lock(out, root, rel, "my-recipe", "my-recipe")
    assert rules(out) == {"H9"}


def test_licence_and_tags_are_permitted_manifest_keys(tmp_path):
    """TRAP: I expected these to fail the schema. They do not."""
    schema = tmp_path / "schema.json"
    schema.write_text(
        json.dumps(
            {
                "properties": {
                    k: {}
                    for k in (
                        "type",
                        "status",
                        "language",
                        "description",
                        "ownership",
                        "license",
                        "tags",
                        "deployable",
                        "large",
                        "architecture",
                    )
                },
                "required": ["type"],
            }
        )
    )
    root, rel = recipe(
        tmp_path,
        **{
            "manifest.yaml": 'type: standalone\ndescription: "A real description here"\n'
            "license: Apache-2.0\ntags:\n  - finance\n"
        },
    )
    out = []
    chr.check_manifest(out, root, rel, str(schema))
    assert "H19" not in rules(out)


def test_unknown_manifest_key_is_a_violation(tmp_path):
    schema = tmp_path / "schema.json"
    schema.write_text(json.dumps({"properties": {"type": {}}, "required": []}))
    root, rel = recipe(
        tmp_path, **{"manifest.yaml": "type: standalone\nowner: someone\n"}
    )
    out = []
    chr.check_manifest(out, root, rel, str(schema))
    assert "H19" in rules(out)


def test_load_dotenv_reports_where_it_is_called_not_just_absence(tmp_path):
    """TRAP: I posted "only in tests/ and eval/" from a truncated grep. Wrong.

    It was called from four package modules. The finding must name them, or the
    author replies "it's right there in agent.py" and discounts the comment.
    """
    root, rel = recipe(
        tmp_path,
        **{
            "mypkg/__init__.py": "# no bootstrap here\n",
            "mypkg/agent.py": "from dotenv import load_dotenv\nload_dotenv()\n",
            "mypkg/tools.py": "from dotenv import load_dotenv\nload_dotenv()\n",
        },
    )
    out = []
    chr.check_dotenv_bootstrap(out, root, rel)
    assert rules(out) == {"H15"}
    what = out[0]["what"]
    assert "2 module(s)" in what
    assert "agent.py" in what and "tools.py" in what


def test_load_dotenv_in_package_init_is_compliant(tmp_path):
    root, rel = recipe(
        tmp_path,
        **{
            "mypkg/__init__.py": "from dotenv import load_dotenv\nload_dotenv()\n",
            "mypkg/agent.py": "import os\n",
        },
    )
    out = []
    chr.check_dotenv_bootstrap(out, root, rel)
    assert out == []


def test_recipe_that_never_reads_dotenv_is_not_flagged(tmp_path):
    root, rel = recipe(
        tmp_path, **{"mypkg/__init__.py": "", "mypkg/a.py": "x = 1\n"}
    )
    out = []
    chr.check_dotenv_bootstrap(out, root, rel)
    assert out == []


# -------------------------------------------------------------- pyproject


def test_ruff_table_in_recipe_is_ci_fail(tmp_path):
    root, rel = recipe(
        tmp_path,
        **{
            "pyproject.toml": '[project]\nname = "my-recipe"\nrequires-python = ">=3.11"\n[tool.ruff]\nline-length = 100\n'
        },
    )
    out = []
    chr.check_pyproject(out, root, rel, "my-recipe")
    assert "H1" in rules(out)
    assert next(f for f in out if f["rule"] == "H1")["ci"] == "fail"


def test_name_must_equal_folder_basename(tmp_path):
    root, rel = recipe(
        tmp_path, **{"pyproject.toml": '[project]\nname = "other"\n'}
    )
    out = []
    chr.check_pyproject(out, root, rel, "my-recipe")
    assert "H3" in rules(out)


def test_requires_python_310_permits_older(tmp_path):
    root, rel = recipe(
        tmp_path,
        **{
            "pyproject.toml": '[project]\nname = "my-recipe"\nrequires-python = ">=3.10"\n'
        },
    )
    out = []
    chr.check_pyproject(out, root, rel, "my-recipe")
    assert "H4" in rules(out)


def test_requires_python_312_excludes_311(tmp_path):
    root, rel = recipe(
        tmp_path,
        **{
            "pyproject.toml": '[project]\nname = "my-recipe"\nrequires-python = ">=3.12"\n'
        },
    )
    out = []
    chr.check_pyproject(out, root, rel, "my-recipe")
    assert "H4" in rules(out)


def test_requires_python_311_range_is_accepted(tmp_path):
    root, rel = recipe(
        tmp_path,
        **{
            "pyproject.toml": '[project]\nname = "my-recipe"\nrequires-python = ">=3.11,<3.14"\n'
        },
    )
    out = []
    chr.check_pyproject(out, root, rel, "my-recipe")
    assert "H4" not in rules(out)


def test_dotenv_in_dev_group_does_not_count(tmp_path):
    root, rel = recipe(
        tmp_path,
        **{
            "pyproject.toml": '[project]\nname = "my-recipe"\nrequires-python = ">=3.11"\n'
            'dependencies = ["google-adk>=2.0"]\n'
            '[dependency-groups]\ndev = ["python-dotenv>=1.0.0"]\n'
        },
    )
    out = []
    chr.check_pyproject(out, root, rel, "my-recipe")
    assert "H6" in rules(out)


def test_single_bracket_uv_index_is_flagged(tmp_path):
    root, rel = recipe(
        tmp_path,
        **{
            "pyproject.toml": '[project]\nname = "my-recipe"\nrequires-python = ">=3.11"\n'
            '[tool.uv.index]\nurl = "https://pypi.org/simple"\ndefault = true\n'
        },
    )
    out = []
    chr.check_pyproject(out, root, rel, "my-recipe")
    assert "H5" in rules(out)


# ------------------------------------------------------------ model ids


def test_deprecated_model_id_is_one_finding_not_one_per_hit(tmp_path):
    root, rel = recipe(
        tmp_path,
        **{
            "a.py": 'm = "gemini-2.5-flash"\n',
            "b.py": 'n = "gemini-2.5-flash"\n',
            "c.md": "use gemini-2.0-flash\n",
        },
    )
    # H10 only applies where AGENTS.md declares the deprecated list.
    (Path(root) / "AGENTS.md").write_text("Do NOT use gemini-2.5-flash.\n")
    out = []
    chr.check_text_wide(out, root, rel)
    h10 = [f for f in out if f["rule"] == "H10"]
    assert len(h10) == 1
    assert "3 occurrence(s)" in h10[0]["what"]


def test_lockfile_is_not_scanned_for_model_ids(tmp_path):
    root, rel = recipe(tmp_path, **{"uv.lock": "gemini-2.5-flash\n"})
    (Path(root) / "AGENTS.md").write_text("Do NOT use gemini-2.5-flash.\n")
    out = []
    chr.check_text_wide(out, root, rel)
    assert "H10" not in rules(out)


# ------------------------------------------------------- skip reporting


def test_skipped_rules_are_reported_never_silent(tmp_path):
    """A checker that hides what it did not check makes every clean run a lie."""
    chr.SKIPPED.clear()
    root, rel = recipe(tmp_path, **{"manifest.yaml": "type: standalone\n"})
    chr.check_manifest([], root, rel, "/nonexistent/schema.json")
    # pyyaml absent -> degraded parse must be recorded, not swallowed
    import importlib.util

    if importlib.util.find_spec("yaml") is None:
        assert any("H19" in r for r, _ in chr.SKIPPED)


# --------------------------------- change-awareness and rule-source presence


def test_preexisting_violation_is_not_attributed_to_a_small_edit(tmp_path):
    """PR #1994 exposed this: a 16-line model-name change was being blamed for
    [tool.ruff] tables and missing test files it never touched."""
    root, rel = recipe(
        tmp_path,
        **{
            "pyproject.toml": '[project]\nname = "my-recipe"\nrequires-python = ">=3.10"\n[tool.ruff]\nx = 1\n'
        },
    )
    changed = tmp_path / "changed.txt"
    changed.write_text(f"{rel}/some_other_file.py\n")
    chr.CHANGED = {f"{rel}/some_other_file.py"}
    chr.NEW_RECIPE = False
    chr.FILTERED.clear()
    try:
        out = []
        chr.check_pyproject(out, root, rel, "my-recipe")
        assert out == [], (
            "pre-existing pyproject violations must not be reported"
        )
        assert chr.FILTERED, "and they must be counted, not silently dropped"
    finally:
        chr.CHANGED = None
        chr.NEW_RECIPE = True


def test_new_recipe_is_audited_in_full(tmp_path):
    root, rel = recipe(
        tmp_path,
        **{
            "pyproject.toml": '[project]\nname = "wrong"\nrequires-python = ">=3.10"\n'
        },
    )
    chr.CHANGED = {f"{rel}/pyproject.toml"}
    chr.NEW_RECIPE = True
    try:
        out = []
        chr.check_pyproject(out, root, rel, "my-recipe")
        assert {f["rule"] for f in out} >= {"H3", "H4"}
    finally:
        chr.CHANGED = None


def test_h10_is_skipped_when_agents_md_is_absent(tmp_path):
    """Applying today's deprecated-model list to a branch that predates it is
    anachronistic. PR #1994's head has no AGENTS.md at all."""
    root, rel = recipe(tmp_path, **{"a.py": 'm = "gemini-2.5-flash"\n'})
    chr.SKIPPED.clear()
    out = []
    chr.check_text_wide(out, root, rel)
    assert "H10" not in rules(out)
    assert any(r == "H10" for r, _ in chr.SKIPPED)


def test_h10_fires_when_agents_md_declares_the_list(tmp_path):
    root, rel = recipe(tmp_path, **{"a.py": 'm = "gemini-2.5-flash"\n'})
    (Path(root) / "AGENTS.md").write_text("Do NOT use gemini-2.5-flash.\n")
    chr.SKIPPED.clear()
    out = []
    chr.check_text_wide(out, root, rel)
    assert "H10" in rules(out)


def test_h24_is_skipped_without_policy_yml(tmp_path):
    root, _rel = recipe(tmp_path, name="legacy")
    frozen_rel = "python/agents/legacy"
    (Path(root) / "python" / "agents" / "legacy").mkdir(parents=True)
    chr.SKIPPED.clear()
    out = []
    chr.check_layout(out, root, frozen_rel, "legacy")
    assert "H24" not in rules(out)
    assert any(r == "H24" for r, _ in chr.SKIPPED)


def test_new_recipe_detection_needs_the_complete_file_list(tmp_path):
    """PR #2302 regression: `gh pr view --json files` caps at 100.

    With a truncated list, pyproject.toml was absent, NEW_RECIPE came out False,
    and every finding on a brand-new 787-file recipe was filtered as pre-existing.
    """
    root, rel = recipe(
        tmp_path,
        **{
            "pyproject.toml": '[project]\nname = "my-recipe"\nrequires-python = ">=3.10"\n'
        },
    )

    truncated = {f"{rel}/some/other/file{i}.py" for i in range(100)}
    chr.CHANGED, chr.NEW_RECIPE = (
        truncated,
        any(
            c.startswith(rel + "/")
            and c.endswith(("manifest.yaml", "pyproject.toml"))
            for c in truncated
        ),
    )
    chr.FILTERED.clear()
    try:
        out = []
        chr.check_pyproject(out, root, rel, "my-recipe")
        assert out == [], (
            "truncated list should look like an edit, not a new recipe"
        )
    finally:
        chr.CHANGED, chr.NEW_RECIPE = None, True

    complete = truncated | {f"{rel}/pyproject.toml"}
    chr.CHANGED, chr.NEW_RECIPE = complete, True
    try:
        out = []
        chr.check_pyproject(out, root, rel, "my-recipe")
        assert "H4" in rules(out), (
            "complete list must attribute findings to the PR"
        )
    finally:
        chr.CHANGED, chr.NEW_RECIPE = None, True


# ------------------------------------------------ H26: env-read defaults


def env_hits(src):
    return chr._env_read_defaults(src, "a.py")


def test_getenv_with_a_default_is_flagged():
    assert env_hits('import os\nx = os.getenv("X", "d")\n')


def test_environ_get_with_a_default_is_flagged():
    assert env_hits('import os\nx = os.environ.get("X", "d")\n')


def test_setdefault_is_flagged():
    assert env_hits('import os\nos.environ.setdefault("X", "d")\n')


def test_keyword_default_is_flagged():
    assert env_hits('import os\nx = os.getenv("X", default="d")\n')


def test_or_fallback_is_flagged():
    assert env_hits('import os\nx = os.getenv("X") or "fallback"\n')


def test_bare_getenv_is_clean():
    assert not env_hits('import os\nx = os.getenv("X")\n')


def test_dict_style_access_is_clean():
    """os.environ["X"] cannot carry a default, so it is the encouraged shape."""
    assert not env_hits('import os\nx = os.environ["X"]\n')


def test_or_none_is_not_a_default():
    assert not env_hits('import os\nx = os.environ.get("X") or None\n')


def test_a_default_in_a_docstring_is_not_a_finding():
    """AST, not regex -- prose describing the pattern must not fire."""
    assert not env_hits(
        '"""Call os.getenv("X", "d") to read it."""\nimport os\n'
    )


def test_an_unrelated_dot_get_is_not_an_env_read():
    assert not env_hits('d = {}\nx = d.get("X", "default")\n')


def test_syntax_error_does_not_crash_the_check():
    assert env_hits("def broken(:\n") == []


# ------------------------------------------------ H27: licence headers

FULL = "\n".join(
    "# " + ln if ln else "#"
    for ln in [
        "Copyright 2026 Google LLC",
        "",
        'Licensed under the Apache License, Version 2.0 (the "License");',
        "you may not use this file except in compliance with the License.",
        "You may obtain a copy of the License at",
        "",
        "    https://www.apache.org/licenses/LICENSE-2.0",
        "",
        "Unless required by applicable law or agreed to in writing, software",
        'distributed under the License is distributed on an "AS IS" BASIS,',
        "WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.",
        "See the License for the specific language governing permissions and",
        "limitations under the License.",
    ]
)


def test_full_header_is_recognised():
    assert chr._header_state(FULL + "\nimport os\n", "#")[0] == "full"


def test_one_line_copyright_is_partial():
    assert (
        chr._header_state("# Copyright 2026 Google LLC\nimport os\n", "#")[0]
        == "partial"
    )


def test_no_header_is_none():
    assert chr._header_state("import os\n", "#")[0] == "none"


def test_header_after_a_ruff_pragma_still_counts():
    assert chr._header_state("# ruff: noqa\n" + FULL, "#")[0] == "full"


def test_slash_comment_style_is_supported():
    js = FULL.replace("#", "//")
    assert chr._header_state(js, "//")[0] == "full"


def test_a_type_with_no_headers_anywhere_is_not_drift(tmp_path):
    """Every .tf file lacking a header is the convention, not 12 violations.

    Pooling extensions produced exactly this false positive on PR #2302.
    """
    root, rel = recipe(
        tmp_path,
        **{f"terraform/f{i}.tf": 'resource "x" {}\n' for i in range(6)},
    )
    out = []
    chr.check_license_headers(out, root, rel)
    assert out == []


def test_a_minority_breaking_the_convention_is_flagged(tmp_path):
    files = {f"m{i}.py": FULL + "\nimport os\n" for i in range(8)}
    files["odd.py"] = "# Copyright 2026 Google LLC\nimport os\n"
    root, rel = recipe(tmp_path, **files)
    out = []
    chr.check_license_headers(out, root, rel)
    assert "H27" in rules(out)
    assert "1 .py file(s) differ" in out[0]["what"]


def test_minority_breaking_a_clear_convention_says_so(tmp_path):
    files = {f"m{i}.py": FULL + "\nimport os\n" for i in range(8)}
    files["odd.py"] = "# Copyright 2026 Google LLC\nimport os\n"
    root, rel = recipe(tmp_path, **files)
    out = []
    chr.check_license_headers(out, root, rel)
    assert "differ from the 8" in out[0]["what"]


def test_two_competing_conventions_are_described_as_such(tmp_path):
    """PR #2373: 78 files use a one-line notice, 9 use Apache. Calling the 78
    'truncated Apache headers' misdescribes what is actually there."""
    files = {
        f"short{i}.py": "# Copyright 2026 Google LLC\nimport os\n"
        for i in range(8)
    }
    files.update({f"full{i}.py": FULL + "\nimport os\n" for i in range(2)})
    root, rel = recipe(tmp_path, **files)
    out = []
    chr.check_license_headers(out, root, rel)
    assert "two different headers" in out[0]["what"]


def test_no_apache_header_anywhere_is_reported_once(tmp_path):
    files = {f"m{i}.py": "import os\n" for i in range(6)}
    root, rel = recipe(tmp_path, **files)
    out = []
    chr.check_license_headers(out, root, rel)
    assert len(out) == 1
    assert "no .py file in this recipe" in out[0]["what"]


def test_config_formats_stay_consistency_only(tmp_path):
    """Required for source, consistency-only for config: 6 header-less .tf files
    are the terraform convention, not six violations."""
    root, rel = recipe(
        tmp_path,
        **{f"terraform/f{i}.tf": 'resource "x" {}\n' for i in range(6)},
    )
    out = []
    chr.check_license_headers(out, root, rel)
    assert out == []


def test_a_single_source_file_establishes_nothing(tmp_path):
    root, rel = recipe(tmp_path, **{"only.py": "import os\n"})
    out = []
    chr.check_license_headers(out, root, rel)
    assert out == []


# ------------------------------------------------------------------ H48


def manifest_with(tmp_path, ownership, name="my-recipe"):
    return recipe(
        tmp_path,
        name=name,
        **{
            "manifest.yaml": "type: standalone\n"
            'description: "A real description here"\n'
            f"ownership:\n{ownership}"
        },
    )


def h48(out):
    return [f for f in out if f["rule"] == "H48"]


def test_company_name_as_team_is_reported(tmp_path):
    for value in (
        '"google"',
        '"Google LLC"',
        '"Google Cloud"',
        '"ADK Samples Team"',
        '"n/a"',
        '"me"',
        '"TEAM"',
    ):
        out = []
        root, rel = manifest_with(
            tmp_path / value.strip('"'), f'  team: {value}\n  poc: "someone"\n'
        )
        chr.check_manifest(out, root, rel, None)
        assert h48(out), f"{value} should be flagged as a non-team"


def test_team_equal_to_the_poc_handle_is_reported(tmp_path):
    out = []
    root, rel = manifest_with(
        tmp_path, '  team: "lspataroG"\n  poc: "lspatarog"\n'
    )
    chr.check_manifest(out, root, rel, None)
    assert "poc" in h48(out)[0]["what"]
    assert "cannot be both" in h48(out)[0]["what"]


def test_team_equal_to_a_contributor_handle_is_reported(tmp_path):
    out = []
    root, rel = manifest_with(
        tmp_path,
        '  team: "someone"\n  poc: "other"\n  contributors:\n    - "someone"\n',
    )
    chr.check_manifest(out, root, rel, None)
    assert h48(out), "a team matching a contributor handle should be flagged"


def test_real_team_names_already_in_the_repo_are_not_flagged(tmp_path):
    """Calibration. A 'that looks like a username' heuristic flags every one of
    these, and each is a real owning team in google/adk-samples today."""
    for value in (
        '"DEE"',
        '"octo"',
        '"adk-kotlin"',
        '"attenu-io"',
        '"OpenEAGO"',
        '"RobustAI"',
        '"Verizon"',
        "FDE/Blackbelt",
        '"GS&I AI Apps and Platforms"',
        '"Developer Evangelism & Engineering (DevRel) - Cloud AI"',
    ):
        out = []
        root, rel = manifest_with(
            tmp_path / value.strip('"').replace("/", "-"),
            f'  team: {value}\n  poc: "someone"\n',
        )
        chr.check_manifest(out, root, rel, None)
        assert not h48(out), f"{value} is a real team and must not be flagged"


def test_placeholder_team_stays_h17_only(tmp_path):
    """Two comments on one line is noise; the placeholder is H17's finding."""
    out = []
    root, rel = manifest_with(
        tmp_path,
        '  team: "TODO: Replace with your team name"\n  poc: "someone"\n',
    )
    chr.check_manifest(out, root, rel, None)
    assert "H17" in rules(out) and not h48(out)


def test_h48_survives_the_changed_file_filter(tmp_path, monkeypatch):
    """It is reported even when the PR did not touch manifest.yaml -- the whole
    point of the rule is that nothing else ever catches the value."""
    monkeypatch.setattr(chr, "CHANGED", {"contrib/python/my-recipe/agent.py"})
    monkeypatch.setattr(chr, "NEW_RECIPE", False)
    out = []
    root, rel = manifest_with(tmp_path, '  team: "google"\n  poc: "someone"\n')
    chr.check_manifest(out, root, rel, None)
    assert h48(out)


# ---------------------------------------------- the degraded (no pyyaml) parse

TEMPLATE_MANIFEST = """\
type: "standalone"     # Options: [standalone | module]
status: "active"        # Options: [active | inactive]
language: "python"      # Options: [python | java | go | kotlin | typescript]
description: "A real description of a real recipe, long enough to pass."
# deployable: true      # (optional) omit if false
ownership:
  team: "google"
  poc: "someone"
"""

FULL_SCHEMA = {
    "properties": {
        "type": {"enum": ["standalone", "module"]},
        "status": {"enum": ["active", "inactive"]},
        "language": {"enum": ["python", "java", "go", "kotlin", "typescript"]},
        "description": {},
        "ownership": {},
        "deployable": {},
        "license": {},
        "tags": {},
        "architecture": {},
        "large": {},
    },
    "required": ["type", "status", "language", "description", "ownership"],
}


def test_inline_comments_do_not_fake_an_enum_violation(tmp_path, monkeypatch):
    """The manifest TEMPLATE ships every enum with a trailing `# Options: [...]`
    comment. A fallback parser that keeps it reads the value as
    '"standalone"  # Options: [standalone | module]' and H19 reports three
    CI-FAILs that are pure fiction. It did, on contrib/python/clause-agent."""
    monkeypatch.setitem(
        __import__("sys").modules, "yaml", None
    )  # force degraded
    schema = tmp_path / "schema.json"
    schema.write_text(json.dumps(FULL_SCHEMA))
    root, rel = recipe(tmp_path, **{"manifest.yaml": TEMPLATE_MANIFEST})
    out = []
    chr.check_manifest(out, root, rel, str(schema))
    assert "H19" not in rules(out), [f["what"] for f in out]


def test_degraded_parse_still_reads_the_ownership_block(tmp_path, monkeypatch):
    """Degrading must not silently disable H48 -- a checker that quietly skips
    a rule is worse than no checker."""
    monkeypatch.setitem(__import__("sys").modules, "yaml", None)
    root, rel = recipe(tmp_path, **{"manifest.yaml": TEMPLATE_MANIFEST})
    out = []
    chr.check_manifest(out, root, rel, None)
    assert h48(out) and 'is "google"' in h48(out)[0]["what"]


def test_a_commented_out_key_is_not_a_key(tmp_path):
    """`# deployable: true` in the template is documentation, not a declaration."""
    assert (
        chr._parse_manifest("# deployable: true\ntype: standalone\n")[0].get(
            "deployable"
        )
        is None
    )


def test_scalar_values_survive_a_hash_inside_them():
    """A `#` that is not preceded by whitespace is part of the value."""
    assert chr._scalar_value("issue-#42  # a real comment") == "issue-#42"
    assert chr._scalar_value('"quoted # hash"  # comment') == "quoted # hash"
    assert chr._scalar_value("plain value") == "plain value"


# --------------------------------- H39 H40 H41 H42 H43 H44 H47 (the silent gap)


def test_h39_a_stub_committed_as_a_real_value(tmp_path):
    root, rel = recipe(
        tmp_path,
        **{
            ".env.example": "GOOGLE_CLOUD_PROJECT=your-project-id\nREAL_ONE=us-central1\n"
        },
    )
    out = []
    chr.check_text_wide(out, root, rel)
    hits = [f for f in out if f["rule"] == "H39"]
    assert len(hits) == 1 and "your-project-id" in hits[0]["what"]


def test_h39_does_not_fire_on_a_stub_shaped_substring(tmp_path):
    """A URL that CONTAINS example.com is a real value; the stub is the whole
    value. A substring rule flags every docs link in every recipe."""
    root, rel = recipe(
        tmp_path,
        **{
            ".env.example": "DOCS_URL=https://example.com/docs/getting-started\n"
            "REGION=us-central1\nTIMEOUT=30\n"
        },
    )
    out = []
    chr.check_text_wide(out, root, rel)
    assert not [f for f in out if f["rule"] == "H39"]


def test_h39_and_h14_never_both_fire_on_one_line(tmp_path):
    root, rel = recipe(tmp_path, **{".env.example": "KEY=changeme\n"})
    out = []
    chr.check_text_wide(out, root, rel)
    assert [f["rule"] for f in out] == ["H14"]


def test_h40_language_disagrees_with_the_path(tmp_path):
    root = tmp_path
    (root / "core" / "python" / "thing").mkdir(parents=True)
    (root / "core" / "python" / "thing" / "manifest.yaml").write_text(
        'type: standalone\nlanguage: "go"\n'
    )
    out = []
    chr.check_layout(out, str(root), "core/python/thing", "thing")
    hits = [f for f in out if f["rule"] == "H40"]
    assert len(hits) == 1 and '"go"' in hits[0]["what"]


def test_h40_stays_quiet_when_the_path_has_no_language_segment(tmp_path):
    """core/<name>/ is a placement problem, not a language mismatch. Calling it
    one describes the wrong defect to the author."""
    root = tmp_path
    (root / "core" / "thing").mkdir(parents=True)
    (root / "core" / "thing" / "manifest.yaml").write_text(
        'type: standalone\nlanguage: "python"\n'
    )
    out = []
    chr.check_layout(out, str(root), "core/thing", "thing")
    assert not [f for f in out if f["rule"] == "H40"]


def test_h41_and_h23_and_h47_never_double_report(tmp_path):
    """One misplaced skill, one comment -- not three saying the same thing."""
    cases = {
        "skills/store-ops": "H41",  # too shallow
        "skills/a/b/c": "H23",  # too deep
        "skills/python/store-ops": "H47",  # right depth, language folder
        "skills/retail/store-ops": None,  # correct
    }
    for rel, expected in cases.items():
        root = tmp_path / rel.replace("/", "_")
        (root / rel).mkdir(parents=True)
        out = []
        chr.check_layout(out, str(root), rel, rel.rsplit("/", 1)[-1])
        got = [f["rule"] for f in out if f["rule"] in ("H23", "H41", "H47")]
        assert got == ([expected] if expected else []), f"{rel} -> {got}"


def test_h42_mixed_pr_is_reported_once_anchored_in_the_recipe(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(
        chr,
        "CHANGED",
        {
            ".agents/skills/some-skill/SKILL.md",
            "contrib/python/my-recipe/manifest.yaml",
            "contrib/python/my-recipe/agent.py",
        },
    )
    out = []
    root, rel = recipe(tmp_path)
    chr.check_pr_shape(out, root, rel)
    assert len(out) == 1
    assert out[0]["rule"] == "H42"
    assert out[0]["path"] == "contrib/python/my-recipe/manifest.yaml"


def test_h42_silent_on_a_skill_only_or_recipe_only_pr(tmp_path, monkeypatch):
    root, rel = recipe(tmp_path)
    for changed in (
        {".agents/skills/x/SKILL.md"},
        {"contrib/python/my-recipe/agent.py"},
        None,
    ):
        monkeypatch.setattr(chr, "CHANGED", changed)
        out = []
        chr.check_pr_shape(out, root, rel)
        assert out == [], changed


def test_h43_only_reports_files_git_actually_tracks(tmp_path, monkeypatch):
    """An untracked .env is a developer who ran the recipe. Reporting it as
    committed is the most alarming false positive this script could produce --
    on the real repo the filesystem shows 17 and git tracks none."""
    root, rel = recipe(
        tmp_path, **{".env": "SECRET=x\n", ".env.example": "A=b\n"}
    )
    monkeypatch.setattr(chr, "_TRACKED", {})
    monkeypatch.setattr(chr, "_tracked_files", lambda r, s: set())
    out = []
    chr.check_layout(out, root, rel, "my-recipe")
    assert not [f for f in out if f["rule"] == "H43"]

    monkeypatch.setattr(
        chr,
        "_tracked_files",
        lambda r, s: {f"{rel}/.env", f"{rel}/.env.example"},
    )
    out = []
    chr.check_layout(out, root, rel, "my-recipe")
    hits = [f for f in out if f["rule"] == "H43"]
    assert len(hits) == 1 and hits[0]["path"].endswith("/.env")


def test_h43_example_variants_are_not_real_env_files(tmp_path, monkeypatch):
    root, rel = recipe(tmp_path)
    monkeypatch.setattr(chr, "_TRACKED", {})
    monkeypatch.setattr(
        chr,
        "_tracked_files",
        lambda r, s: {
            f"{rel}/.env.local.example",
            f"{rel}/.env.test.sample",
            f"{rel}/.env.template",
        },
    )
    out = []
    chr.check_layout(out, root, rel, "my-recipe")
    assert not [f for f in out if f["rule"] == "H43"]


def test_h43_says_so_when_git_cannot_answer(tmp_path, monkeypatch):
    monkeypatch.setattr(chr, "SKIPPED", [])
    monkeypatch.setattr(chr, "_tracked_files", lambda r, s: None)
    root, rel = recipe(tmp_path)
    chr.check_layout([], root, rel, "my-recipe")
    assert any(r == "H43" for r, _ in chr.SKIPPED)


def test_h44_pruned_directory_name(tmp_path, monkeypatch):
    """From git, not the filesystem: dist/ and build/ are exactly what a local
    build leaves behind and .gitignore hides, and SKILL.md invokes this
    checker against a developer's own tree."""
    root, rel = recipe(tmp_path, **{"dist/thing.py": "x = 1\n"})
    monkeypatch.setattr(chr, "_TRACKED", {})
    monkeypatch.setattr(
        chr, "_tracked_files", lambda r, s: {f"{rel}/dist/thing.py"}
    )
    out = []
    chr.check_layout(out, root, rel, "my-recipe")
    hits = [f for f in out if f["rule"] == "H44"]
    assert len(hits) == 1 and hits[0]["path"].endswith("/dist")


def test_h44_ignores_an_untracked_build_directory(tmp_path, monkeypatch):
    root, rel = recipe(tmp_path, **{"dist/thing.py": "x = 1\n"})
    monkeypatch.setattr(chr, "_TRACKED", {})
    monkeypatch.setattr(chr, "_tracked_files", lambda r, s: set())
    out = []
    chr.check_layout(out, root, rel, "my-recipe")
    assert not [f for f in out if f["rule"] == "H44"]


def test_h43_reports_one_grouped_finding_not_one_per_file(
    tmp_path, monkeypatch
):
    """H10 and H26 both group; a recipe with twelve stray files should not
    collect twelve comments."""
    root, rel = recipe(tmp_path)
    monkeypatch.setattr(chr, "_TRACKED", {})
    monkeypatch.setattr(
        chr,
        "_tracked_files",
        lambda r, s: {f"{rel}/.env", f"{rel}/.DS_Store", f"{rel}/a.pyc"},
    )
    out = []
    chr.check_layout(out, root, rel, "my-recipe")
    hits = [f for f in out if f["rule"] == "H43"]
    assert len(hits) == 1
    assert "3 such file(s)" in hits[0]["what"]


def test_h43_does_not_call_every_pem_a_private_key(tmp_path, monkeypatch):
    """A public CA bundle is an ordinary committed file, and "you committed a
    private key" is the most alarming thing this checker can say."""
    root, rel = recipe(tmp_path)
    monkeypatch.setattr(chr, "_TRACKED", {})
    monkeypatch.setattr(
        chr,
        "_tracked_files",
        lambda r, s: {
            f"{rel}/certs/server-ca.pem",
            f"{rel}/service-account-template.json",
            f"{rel}/.vscode/launch.json",
        },
    )
    out = []
    chr.check_layout(out, root, rel, "my-recipe")
    assert not [f for f in out if f["rule"] == "H43"]


def test_h43_still_catches_a_real_private_key(tmp_path, monkeypatch):
    root, rel = recipe(tmp_path)
    monkeypatch.setattr(chr, "_TRACKED", {})
    monkeypatch.setattr(
        chr, "_tracked_files", lambda r, s: {f"{rel}/server-key.pem"}
    )
    out = []
    chr.check_layout(out, root, rel, "my-recipe")
    assert [f for f in out if f["rule"] == "H43"]


def test_tracked_file_cache_is_per_recipe(tmp_path):
    """The CI lane checks several recipes in one process. A single cached set
    answers the second recipe with the first recipe's files."""
    chr._TRACKED.clear()
    chr._TRACKED[("/root", "a")] = {"a/.env"}
    chr._TRACKED[("/root", "b")] = set()
    assert chr._tracked_files("/root", "b") == set()
    assert chr._tracked_files("/root", "a") == {"a/.env"}


def test_words_that_name_real_teams_are_not_generic(tmp_path):
    """The trailing-noun stripper is why this list must stay short: "Cloud
    Org" strips to "cloud", so a plausible team word on the list flags every
    real team whose name ends in it. `DevRel` is the everyday short form of a
    team that owns five recipes in this repository."""
    for value in (
        "DevRel",
        "Community",
        "Engineering",
        "Cloud Org",
        "Samples",
        "Developer Relations",
        "Public Sector",
    ):
        out = []
        root, rel = manifest_with(
            tmp_path / value.replace(" ", "-"),
            f'  team: "{value}"\n  poc: "someone"\n',
        )
        chr.check_manifest(out, root, rel, None)
        assert not h48(out), f"{value} is a plausible real team"


def test_a_group_alias_is_an_owner(tmp_path):
    """A mailing list is the one value immune to the failure this rule exists
    to prevent: it does not leave when a person does."""
    out = []
    root, rel = manifest_with(
        tmp_path, '  team: "adk-samples-team@google.com"\n  poc: "someone"\n'
    )
    chr.check_manifest(out, root, rel, None)
    assert not h48(out)


def test_a_url_is_still_not_a_team(tmp_path):
    out = []
    root, rel = manifest_with(
        tmp_path, '  team: "https://github.com/orgs/x/teams/y"\n  poc: "a"\n'
    )
    chr.check_manifest(out, root, rel, None)
    assert h48(out)


# ------------------------------------ crash inputs that erased a whole recipe


def _all_checks(root, rel, name="my-recipe"):
    """Every check the CI lane runs, in the order the lane runs them.

    AGENTS.md is written first because check_text_wide skips its whole
    directory walk without one -- and that walk is where the file-size and
    symlink handling lives. A crash test that never reaches the walk passes
    for no reason at all.
    """
    Path(root, "AGENTS.md").write_text("Use gemini-3.5-flash instead.\n")
    out = []
    chr.check_pyproject(out, root, rel, name)
    chr.check_uv_lock(out, root, rel, name, "")
    chr.check_dotenv_bootstrap(out, root, rel)
    chr.check_manifest(out, root, rel, None)
    chr.check_readme(out, root, rel)
    chr.check_layout(out, root, rel, name)
    chr.check_text_wide(out, root, rel)
    chr.check_env_defaults(out, root, rel)
    chr.check_license_headers(out, root, rel)
    return out


def test_a_dangling_symlink_does_not_erase_the_recipes_review(tmp_path):
    """git stores mode 120000 and never checks the target, so a dangling
    symlink is trivially committable. os.path.getsize on it raised
    FileNotFoundError, which the lane catches per recipe — so the whole
    recipe went unreviewed and the PR was reported clean. One file evaded the
    entire deterministic lane."""
    root, rel = recipe(tmp_path, **{"manifest.yaml": "type: standalone\n"})
    os.symlink("/nonexistent/target", Path(root) / rel / "dangling.txt")
    findings = _all_checks(root, rel)
    assert findings, "the recipe produced nothing at all"


def test_a_deep_expression_does_not_erase_the_recipes_review(tmp_path):
    """ast.parse raises RecursionError, not SyntaxError, on a generated
    constant table — a plausible accident, not only an attack."""
    root, rel = recipe(
        tmp_path,
        **{
            "manifest.yaml": "type: standalone\n",
            "generated.py": "x = " + "1+" * 60000 + "1\n",
        },
    )
    assert _all_checks(root, rel), "the recipe produced nothing at all"


@pytest.mark.parametrize(
    ("name", "content"),
    [
        ("manifest.yaml", "- a list\n- where a mapping belongs\n"),
        ("manifest.yaml", "just a string\n"),
    ],
)
def test_a_manifest_that_is_not_a_mapping_does_not_crash(
    tmp_path, name, content
):
    root, rel = recipe(tmp_path / content[:6].strip(), **{name: content})
    assert isinstance(_all_checks(root, rel), list)


@pytest.mark.parametrize(
    "pyproject",
    [
        '[tool.uv]\nindex = "https://pypi.org/simple"\n',  # not array-of-tables
        'project = "oops"\n',  # not a table
        '[project]\nname = "x"\n[build-system]\nrequires = "x"\n',
    ],
)
def test_a_mistyped_pyproject_does_not_crash_the_rule_that_catches_it(
    tmp_path, pyproject
):
    """H5 exists to catch a malformed index. A malformed index killing the run
    instead means the recipe is reported clean."""
    root, rel = recipe(
        tmp_path / str(abs(hash(pyproject)))[:6],
        **{"manifest.yaml": "type: standalone\n", "pyproject.toml": pyproject},
    )
    assert isinstance(_all_checks(root, rel), list)


# ------------------------------------------- H21 is scoped by language, not path


@pytest.mark.parametrize(
    ("rel", "language", "expected", "forbidden"),
    [
        (
            "contrib/typescript/ts-thing",
            "typescript",
            {"README.md"},
            {"pyproject.toml", "uv.lock", ".env.example"},
        ),
        (
            "contrib/go/go-thing",
            "go",
            {"README.md", "go.mod"},
            {"pyproject.toml", "uv.lock"},
        ),
        (
            "contrib/python/py-thing",
            "python",
            {"README.md", "pyproject.toml", "uv.lock", ".env.example"},
            set(),
        ),
        (
            "skills/retail/store-ops",
            "typescript",
            {"README.md", "SKILL.md", "EVAL.yaml"},
            {"pyproject.toml", "uv.lock"},
        ),
    ],
)
def test_h21_asks_each_language_for_its_own_files(
    tmp_path, rel, language, expected, forbidden
):
    """It hardcoded the Python list for every recipe in every language, so a
    new TypeScript, Go, Java or Kotlin recipe collected four confident
    CI-FAIL comments demanding files it should never have. policy.yml has
    scoped these under by_language all along."""
    root = tmp_path
    (root / rel).mkdir(parents=True)
    (root / rel / "manifest.yaml").write_text(
        f'type: standalone\nlanguage: "{language}"\n'
    )
    out = []
    chr.check_layout(out, str(root), rel, rel.rsplit("/", 1)[-1])
    asked = {f["path"].rsplit("/", 1)[-1] for f in out if f["rule"] == "H21"}
    assert expected <= asked, f"{language}: missing {expected - asked}"
    assert not (forbidden & asked), (
        f"{language}: wrongly asked for {forbidden & asked}"
    )


@pytest.mark.parametrize(
    ("label", "pyproject", "manifest"),
    [
        (
            "requires-python unquoted",
            '[project]\nname = "my-recipe"\nrequires-python = 3.11\n',
            None,
        ),
        (
            "requires-python a list",
            '[project]\nname = "my-recipe"\nrequires-python = [">=3.11"]\n',
            None,
        ),
        (
            "dependency as a table",
            '[project]\nname = "my-recipe"\ndependencies = [{name = "x"}]\n',
            None,
        ),
        (
            "testpaths a number",
            "[tool.pytest.ini_options]\ntestpaths = 3\n",
            None,
        ),
        (
            "description a mapping",
            None,
            "type: standalone\ndescription:\n  text: hello\n",
        ),
        ("description a number", None, "type: standalone\ndescription: 2026\n"),
    ],
)
def test_a_mistyped_value_never_erases_the_recipes_review(
    tmp_path, label, pyproject, manifest
):
    """Every one of these is a realistic typo — forgetting quotes around
    3.11 most of all — and every one of them raised out of the checker, which
    the lane catches per recipe. The recipe then goes unreviewed and the PR is
    reported clean, losing every other finding in it.

    H4's own code path crashed on exactly the defect H4 exists to catch."""
    files = {"manifest.yaml": manifest or "type: standalone\n"}
    if pyproject:
        files["pyproject.toml"] = pyproject
    root, rel = recipe(tmp_path / label.replace(" ", "-"), **files)
    findings = _all_checks(root, rel)
    assert isinstance(findings, list), label


def test_an_unquoted_requires_python_is_reported_not_swallowed(tmp_path):
    root, rel = recipe(
        tmp_path,
        **{
            "manifest.yaml": "type: standalone\n",
            "pyproject.toml": '[project]\nname = "my-recipe"\n'
            "requires-python = 3.11\n",
        },
    )
    out = []
    chr.check_pyproject(out, root, rel, "my-recipe")
    h4 = [f for f in out if f["rule"] == "H4"]
    assert h4 and "not a string" in h4[0]["what"]


def test_h3_expects_the_namespaced_name_for_a_vertical_skill(tmp_path):
    """check_recipe_pyproject namespaces skills/ as <vertical>-<solution>, so
    comparing against the bare basename told the author of every vertical
    skill to set the one value CI would then reject. It fired on both shipped
    skills in this repo."""
    rel = "skills/retail/store-ops"
    root = tmp_path
    (root / rel).mkdir(parents=True)
    (root / rel / "pyproject.toml").write_text(
        '[project]\nname = "retail-store-ops"\n'
    )
    out = []
    chr.check_pyproject(out, str(root), rel, "store-ops")
    assert not [f for f in out if f["rule"] == "H3"], (
        "the correct namespaced name was reported as wrong"
    )

    (root / rel / "pyproject.toml").write_text(
        '[project]\nname = "store-ops"\n'
    )
    out = []
    chr.check_pyproject(out, str(root), rel, "store-ops")
    h3 = [f for f in out if f["rule"] == "H3"]
    assert h3 and "retail-store-ops" in h3[0]["what"]


def test_h3_is_unchanged_for_an_ordinary_recipe(tmp_path):
    rel = "contrib/python/thing"
    (tmp_path / rel).mkdir(parents=True)
    (tmp_path / rel / "pyproject.toml").write_text(
        '[project]\nname = "thing"\n'
    )
    out = []
    chr.check_pyproject(out, str(tmp_path), rel, "thing")
    assert not [f for f in out if f["rule"] == "H3"]


def _case_sensitive_exists(monkeypatch):
    """Make os.path.exists case-sensitive for the duration of a test.

    The CI runner is Linux; this developer machine is macOS, where
    os.path.exists("EVAL.yaml") is already True when eval.yaml is on disk. A
    test written against the local filesystem therefore passes whether the
    fix is present or not, which is how the first version of this one shipped
    green and useless.
    """
    real = os.path.exists

    def exists(path):
        if not real(path):
            return False
        directory, base = os.path.split(path)
        try:
            return base in os.listdir(directory or ".")
        except OSError:
            return real(path)

    monkeypatch.setattr(chr.os.path, "exists", exists)


def test_h21_accepts_a_lowercase_eval_yaml(tmp_path, monkeypatch):
    """policy.case_insensitive_files lists EVAL.yaml and validate_structure
    honours it, so `eval.yaml` passes CI. A bare os.path.exists on a
    case-sensitive runner reported it missing — a file that is right there."""
    rel = "skills/retail/store-ops"
    (tmp_path / rel).mkdir(parents=True)
    for name in ("README.md", "SKILL.md", "eval.yaml"):
        (tmp_path / rel / name).write_text("x\n")
    (tmp_path / rel / "manifest.yaml").write_text(
        'type: standalone\nlanguage: "typescript"\n'
    )
    _case_sensitive_exists(monkeypatch)
    out = []
    chr.check_layout(out, str(tmp_path), rel, "store-ops")
    missing = {f["path"].rsplit("/", 1)[-1] for f in out if f["rule"] == "H21"}
    assert "EVAL.yaml" not in missing, f"still asked for EVAL.yaml: {missing}"


def test_h21_still_reports_a_genuinely_absent_eval_yaml(tmp_path, monkeypatch):
    rel = "skills/retail/no-eval"
    (tmp_path / rel).mkdir(parents=True)
    for name in ("README.md", "SKILL.md"):
        (tmp_path / rel / name).write_text("x\n")
    (tmp_path / rel / "manifest.yaml").write_text(
        'type: standalone\nlanguage: "typescript"\n'
    )
    _case_sensitive_exists(monkeypatch)
    out = []
    chr.check_layout(out, str(tmp_path), rel, "no-eval")
    missing = {f["path"].rsplit("/", 1)[-1] for f in out if f["rule"] == "H21"}
    assert "EVAL.yaml" in missing


@pytest.mark.parametrize(
    "key_line",
    [
        "on: true",
        "no: something",
        "2026: notes",
        "1.0: notes",
        "2026-01-01: notes",
        "~: notes",
    ],
)
def test_a_manifest_key_yaml_reads_as_a_non_string_does_not_crash(
    tmp_path, key_line
):
    """`on:` is YAML 1.1's Norway problem — it parses as the boolean True —
    and a bare year is an int, a date is a date. re.escape on any of them
    raises TypeError, which the lane catches per recipe: every finding for
    that recipe is discarded and the PR reported clean. The key IS invalid
    under additionalProperties: false, so the rule must say so."""
    schema = tmp_path / "schema.json"
    schema.write_text(json.dumps(FULL_SCHEMA))
    root, rel = recipe(
        tmp_path / key_line.split(":")[0].strip("~ "),
        **{
            "manifest.yaml": "type: standalone\nstatus: active\n"
            'language: python\ndescription: "A real description here."\n'
            f"{key_line}\n"
        },
    )
    out = []
    chr.check_manifest(out, root, rel, str(schema))
    assert isinstance(out, list)
    assert any(f["rule"] == "H19" for f in out), (
        "the invalid key went unreported"
    )


def test_h27_anchors_on_a_file_the_pr_changed(tmp_path, monkeypatch):
    """H27 counts the whole recipe, but _is_ours filters it on
    `path in CHANGED`. Anchoring on the first offender in WALK order threw
    the finding away unless that exact file happened to be in the diff — a
    one-in-eight chance on a sixteen-file offender list, so the fix to the
    arithmetic quietly traded a false positive for a false negative."""
    files = {f"headed{i}.py": FULL + "\nimport os\n" for i in range(8)}
    files.update({f"bare{i}.py": "import os\n" for i in range(3)})
    root, rel = recipe(tmp_path, **files)
    # The PR touches the LAST bare file, not the first in walk order.
    changed = f"{rel}/bare2.py"
    monkeypatch.setattr(chr, "CHANGED", {changed})
    monkeypatch.setattr(chr, "FILTERED", [])
    out = []
    chr.check_license_headers(out, root, rel)
    h27 = [f for f in out if f["rule"] == "H27"]
    assert h27, "the finding was filtered away as pre-existing"
    assert h27[0]["path"] == changed
    assert "8" in h27[0]["what"], "the count is not the whole-recipe tally"


def test_h27_counts_the_whole_recipe_not_just_the_changed_files(
    tmp_path, monkeypatch
):
    """The tally is a claim about the RECIPE's convention. Counting only
    changed files made a PR touching two of the unheaded files report "no .py
    file in this recipe carries the standard Apache header" while most of
    them did — the filter chose the branch as well as the numbers."""
    files = {f"headed{i}.py": FULL + "\nimport os\n" for i in range(9)}
    files.update({f"bare{i}.py": "import os\n" for i in range(2)})
    root, rel = recipe(tmp_path, **files)
    monkeypatch.setattr(chr, "CHANGED", {f"{rel}/bare0.py", f"{rel}/bare1.py"})
    monkeypatch.setattr(chr, "FILTERED", [])
    out = []
    chr.check_license_headers(out, root, rel)
    h27 = [f for f in out if f["rule"] == "H27"]
    assert h27
    what = h27[0]["what"]
    assert "9" in what, f"the 9 headed files were not counted: {what}"
    assert "no .py file in this recipe" not in what


@pytest.mark.parametrize(
    ("label", "body"),
    [
        ("a bare date", "2026-01-01\n"),
        ("a timestamp", "2026-01-01 10:00:00\n"),
        ("a set", "!!set\n? a\n? b\n"),
        ("binary", "!!binary |\n  aGVsbG8=\n"),
        ("a list", "- a\n- b\n"),
        ("a scalar", "just a string\n"),
    ],
)
def test_any_non_mapping_manifest_is_skipped_not_fatal(tmp_path, label, body):
    """The guard was a blocklist of the types someone thought of — list, str,
    int, float, bool — and yaml.safe_load also returns dates, timestamps, sets
    and bytes. Each of those still reached .get() and discarded the recipe's
    entire review. The question is "is it a mapping", so it asks that now."""
    root, rel = recipe(
        tmp_path / label.replace(" ", "-"), **{"manifest.yaml": body}
    )
    chr.SKIPPED = []
    out = []
    chr.check_manifest(out, root, rel, None)
    assert any(r.startswith("H17") for r, _ in chr.SKIPPED), (
        "the unreadable manifest was not reported as unchecked"
    )


def test_an_absurdly_long_version_does_not_crash_the_checker(tmp_path):
    """int() on a string of more than 4300 digits raises, and this one is
    reachable straight from PR content: a one-line way for a contributor to
    make the deterministic lane skip their own recipe."""
    root, rel = recipe(
        tmp_path,
        **{
            "manifest.yaml": "type: standalone\n",
            "pyproject.toml": '[project]\nname = "my-recipe"\n'
            'requires-python = ">=3.' + "9" * 5000 + '"\n',
        },
    )
    out = []
    chr.check_pyproject(out, root, rel, "my-recipe")
    assert isinstance(out, list)


@pytest.mark.parametrize(
    "team", ["チーム", "Команда", "大数据平台组", "Ελλάδα", "Équipe Données"]
)
def test_a_team_name_in_a_non_latin_script_is_a_team(tmp_path, team):
    """`[^A-Za-z0-9]+` is ASCII-only, so any name written wholly in another
    script matched "names nobody" — the exact false positive the closed
    GENERIC_TEAMS list exists to avoid."""
    out = []
    root, rel = manifest_with(
        tmp_path / str(abs(hash(team)))[:6],
        f'  team: "{team}"\n  poc: "someone"\n',
    )
    chr.check_manifest(out, root, rel, None)
    assert not h48(out), f"{team} was reported as naming nobody"


@pytest.mark.parametrize("team", ["---", "!!", "..."])
def test_a_punctuation_only_team_still_names_nobody(tmp_path, team):
    out = []
    root, rel = manifest_with(
        tmp_path / str(abs(hash(team)))[:6],
        f'  team: "{team}"\n  poc: "someone"\n',
    )
    chr.check_manifest(out, root, rel, None)
    assert h48(out)


def test_h10_does_not_flag_documentation_that_forbids_the_model(tmp_path):
    """Two recipes in this repo carry "don't use deprecated ones
    (gemini-2.0-flash, gemini-2.5-flash)" in their own AGENTS.md. Reporting
    it is a confidently wrong comment from the one lane whose reason for
    existing is that it cannot produce one. The existing tests wrote
    AGENTS.md at the repo root, outside the scanned subtree, which is why
    they never saw it."""
    root, rel = recipe(
        tmp_path,
        **{
            "AGENTS.md": "Do NOT use gemini-2.0-flash or gemini-2.5-flash — "
            "both are deprecated.\n",
        },
    )
    Path(root, "AGENTS.md").write_text("Use gemini-3.5-flash instead.\n")
    out = []
    chr.check_text_wide(out, root, rel)
    assert not [f for f in out if f["rule"] == "H10"]


def test_h10_still_flags_a_real_use(tmp_path):
    root, rel = recipe(tmp_path, **{"agent.py": 'MODEL = "gemini-2.5-flash"\n'})
    Path(root, "AGENTS.md").write_text("Use gemini-3.5-flash instead.\n")
    out = []
    chr.check_text_wide(out, root, rel)
    assert [f for f in out if f["rule"] == "H10"]


def test_h10_anchors_on_a_file_the_pr_changed(tmp_path, monkeypatch):
    """It anchored on the first hit in walk order, and _is_ours then dropped
    the finding unless the PR happened to touch that exact file — which a
    recipe's own docs mentioning the ids made routine."""
    # zzz FIRST: os.walk yields filesystem order, which here is creation
    # order, so an unsorted implementation anchors on the changed file by
    # luck and the test cannot tell the difference. Sorted, aaa comes first
    # and the anchor has to be chosen deliberately.
    root, rel = recipe(
        tmp_path,
        **{
            "zzz_changed.py": 'MODEL = "gemini-2.5-flash"\n',
            "aaa_first.py": 'FALLBACK = "gemini-2.0-flash"\n',
        },
    )
    Path(root, "AGENTS.md").write_text("Use gemini-3.5-flash instead.\n")
    # BOTH changed, so `_anchor_in_diff` cannot decide it: what is left is
    # the order of `hits`, and os.walk's order is the filesystem's. Sorted,
    # the same pull request always produces the same comment.
    monkeypatch.setattr(
        chr, "CHANGED", {f"{rel}/zzz_changed.py", f"{rel}/aaa_first.py"}
    )
    monkeypatch.setattr(chr, "FILTERED", [])
    out = []
    chr.check_text_wide(out, root, rel)
    h10 = [f for f in out if f["rule"] == "H10"]
    assert h10, "the finding was filtered away as pre-existing"
    assert h10[0]["path"].endswith("aaa_first.py"), (
        f"anchored on {h10[0]['path']} — the order os.walk happened to give"
    )


def test_h10_anchors_on_a_changed_file_when_only_one_changed(
    tmp_path, monkeypatch
):
    root, rel = recipe(
        tmp_path,
        **{
            "aaa_first.py": 'FALLBACK = "gemini-2.0-flash"\n',
            "zzz_changed.py": 'MODEL = "gemini-2.5-flash"\n',
        },
    )
    Path(root, "AGENTS.md").write_text("Use gemini-3.5-flash instead.\n")
    monkeypatch.setattr(chr, "CHANGED", {f"{rel}/zzz_changed.py"})
    monkeypatch.setattr(chr, "FILTERED", [])
    out = []
    chr.check_text_wide(out, root, rel)
    h10 = [f for f in out if f["rule"] == "H10"]
    assert h10 and h10[0]["path"].endswith("zzz_changed.py")


@pytest.mark.parametrize(
    "model",
    [
        "gemini-2.5-flash-image",
        "gemini-2.5-flash-lite",
        "gemini-2.5-flash-preview",
    ],
)
def test_h10_does_not_flag_a_current_model_by_prefix(tmp_path, model):
    """`gemini-2.5-flash-image` is a CURRENT model and the pattern had no
    right-hand boundary, so it matched the prefix: skills/retail/virtual-tryon
    carries 21 occurrences and not one deprecated id, and any PR touching it
    was told to replace a correct image model with a text one."""
    root, rel = recipe(tmp_path / model, **{"agent.py": f'MODEL = "{model}"\n'})
    Path(root, "AGENTS.md").write_text("Use gemini-3.5-flash instead.\n")
    out = []
    chr.check_text_wide(out, root, rel)
    assert not [f for f in out if f["rule"] == "H10"], f"{model} was flagged"


def test_h10_still_flags_a_pinned_deprecated_version(tmp_path):
    """A trailing `-001` is a version pin of the deprecated model itself; a
    trailing letter starts a different model name."""
    root, rel = recipe(
        tmp_path, **{"agent.py": 'MODEL = "gemini-2.5-flash-001"\n'}
    )
    Path(root, "AGENTS.md").write_text("Use gemini-3.5-flash instead.\n")
    out = []
    chr.check_text_wide(out, root, rel)
    assert [f for f in out if f["rule"] == "H10"]


def test_whole_recipe_rules_are_also_scoped_to_their_own_directory():
    """H22/H23 sit in WHOLE_RECIPE_RULES and DIRECTORY_RULES both, and the
    two conditions are independent. `_is_ours` returned on the first set, so
    the directory branch was unreachable for them: a PR that ADDS recipe A
    set NEW_RECIPE=True, and every OTHER recipe the walker reached -- none of
    whose files the PR touches at all -- collected CI-FAILs for a folder name
    and a skill depth that predate the PR by years."""
    added = "contrib/python/new-recipe"
    untouched = "contrib/python/old-recipe"
    chr.CHANGED = {f"{added}/manifest.yaml", f"{added}/agent.py"}
    chr.NEW_RECIPE = True
    try:
        for rule in ("H22", "H23"):
            assert chr._is_ours(rule, added), (
                f"{rule} must still fire on the recipe the PR is adding"
            )
            out = []
            chr.find(out, rule, chr.CI_FAIL, untouched, 1, "w", "e", "v")
            assert out == [], (
                f"{rule} fired on {untouched}, which the PR never touches"
            )
    finally:
        chr.CHANGED = None
        chr.NEW_RECIPE = True


def test_whole_recipe_rules_stay_off_an_edit_only_pr():
    """The original guard must survive: NEW_RECIPE=False silences them even
    though every file under the recipe is in CHANGED."""
    rel = "contrib/python/old-recipe"
    chr.CHANGED = {f"{rel}/agent.py"}
    chr.NEW_RECIPE = False
    try:
        for rule in ("H21", "H22", "H23"):
            assert not chr._is_ours(rule, rel)
    finally:
        chr.CHANGED = None
        chr.NEW_RECIPE = True


def test_h21_is_not_directory_scoped():
    """H21 is whole-recipe only. It must not acquire a directory condition
    from the restructure -- required files can be missing from a recipe whose
    own directory has no changed file under the exact prefix."""
    chr.CHANGED = {"contrib/python/new-recipe/agent.py"}
    chr.NEW_RECIPE = True
    try:
        assert chr._is_ours("H21", "contrib/python/other-recipe")
    finally:
        chr.CHANGED = None
        chr.NEW_RECIPE = True


def test_parse_manifest_tolerates_empty_and_missing_text():
    """`text.split` on None is an AttributeError that takes the whole run
    down. Both callers guard today; the parser must not depend on that."""
    assert chr._parse_manifest(None) == ({}, False)
    assert chr._parse_manifest("") == ({}, False)
