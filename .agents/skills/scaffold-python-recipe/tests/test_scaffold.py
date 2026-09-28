# Copyright 2026 Google LLC
# Licensed under the Apache License, Version 2.0
"""Unit tests for the scaffold-python-recipe skill script."""

import os
import shutil
from pathlib import Path

import pytest
import scaffold as m


def test_replace_in_file(tmp_path):
    f = tmp_path / "sample.txt"
    f.write_text("Hello <RECIPE_NAME> in <OUTPUT_DIRECTORY>", encoding="utf-8")

    m.replace_in_file(
        str(f),
        {"<RECIPE_NAME>": "my-recipe", "<OUTPUT_DIRECTORY>": "contrib"},
    )

    assert f.read_text(encoding="utf-8") == "Hello my-recipe in contrib"


def test_scaffold_creates_tree_and_replaces_placeholders(tmp_path):
    name = "my-test-recipe"

    ok = m.scaffold(name=name, output_dir=str(tmp_path))

    assert ok is True
    target = tmp_path / name
    assert target.is_dir()

    # A representative slice of the template tree was copied — covering the
    # top-level files, the app/ package (including app_utils/), and the
    # tests/ tree — so an accidental deletion or rename of any of these
    # is caught.
    expected_files = [
        "README.md",
        "pyproject.toml",
        "manifest.yaml",
        ".env.example",
        "app/__init__.py",
        "app/agent.py",
        "app/fast_api_app.py",
        "app/app_utils/__init__.py",
        "app/app_utils/telemetry.py",
        "app/app_utils/typing.py",
        "tests/test_runnability.py",
        "tests/unit/test_tools.py",
        "tests/integration/test_agent.py",
    ]
    for rel in expected_files:
        assert (target / rel).is_file(), f"missing scaffolded file: {rel}"

    # Placeholders were resolved.
    readme = target / "README.md"
    pyproject = target / "pyproject.toml"
    readme_text = readme.read_text(encoding="utf-8")
    assert f"# {name}" in readme_text
    assert "<RECIPE_NAME>" not in readme_text
    assert "<OUTPUT_DIRECTORY>" not in readme_text
    assert f'name = "{name}"' in pyproject.read_text(encoding="utf-8")

    # No unresolved <PLACEHOLDER> tokens leaked into any scaffolded file.
    for path in target.rglob("*"):
        if not path.is_file():
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            continue
        assert "<RECIPE_NAME>" not in text, f"unresolved token in {path}"
        assert "<OUTPUT_DIRECTORY>" not in text, f"unresolved token in {path}"


def test_scaffold_refuses_existing_target(tmp_path):
    name = "already-here"
    (tmp_path / name).mkdir()

    ok = m.scaffold(name=name, output_dir=str(tmp_path))

    assert ok is False


# ---------------------------------------------------------------------------
# is_safe_recipe_name / path-traversal guard (Finding 2)
# ---------------------------------------------------------------------------


def test_is_safe_recipe_name():
    assert m.is_safe_recipe_name("my-recipe") is True
    assert m.is_safe_recipe_name("recipe_123") is True
    # Unsafe: separators, parent refs, absolute paths, empties.
    assert m.is_safe_recipe_name("../evil") is False
    assert m.is_safe_recipe_name("a/b") is False
    assert m.is_safe_recipe_name("a\\b") is False
    assert m.is_safe_recipe_name("..") is False
    assert m.is_safe_recipe_name(".") is False
    assert m.is_safe_recipe_name("") is False
    assert m.is_safe_recipe_name("/abs") is False


def test_scaffold_rejects_path_traversal(tmp_path):
    intended = tmp_path / "sub"
    intended.mkdir()

    ok = m.scaffold(name="../evil", output_dir=str(intended))

    assert ok is False
    # Crucially: nothing was written outside the intended output directory.
    assert not (tmp_path / "evil").exists()


# ---------------------------------------------------------------------------
# recipe_name_error / documented naming rules enforced by the script itself
# ---------------------------------------------------------------------------


def test_recipe_name_error_accepts_valid_names():
    for good in ("my-recipe", "a", "rag-agent-search", "a" * 30):
        assert m.recipe_name_error(good) is None, good


@pytest.mark.parametrize(
    "bad",
    [
        "",  # empty
        "My-Recipe",  # uppercase
        "recipe_with_underscores",  # underscore
        "recipe_123",  # underscore + digits
        "recipe 123",  # space
        "-starts-with-hyphen",  # leading hyphen
        "ends-with-hyphen-",  # trailing hyphen
        "a" * 31,  # too long (> 30)
        "with/slash",  # path separator
    ],
)
def test_recipe_name_error_rejects_invalid_names(bad):
    assert m.recipe_name_error(bad) is not None, bad


def test_scaffold_rejects_invalid_name_before_writing(tmp_path):
    # A name that is "path-safe" but violates the documented rules (underscore)
    # must still be refused, and nothing should be written.
    ok = m.scaffold(name="bad_name", output_dir=str(tmp_path))

    assert ok is False
    assert not (tmp_path / "bad_name").exists()


# ---------------------------------------------------------------------------
# is_safe_output_dir / output_dir traversal guard
# ---------------------------------------------------------------------------


def test_is_safe_output_dir():
    assert m.is_safe_output_dir("contrib") is True
    assert m.is_safe_output_dir("core/python") is True
    # Absolute destinations are allowed at the function level (see docstring).
    assert m.is_safe_output_dir("/abs/tmp/dir") is True
    # Relative traversal is refused.
    assert m.is_safe_output_dir("../../other-repo") is False
    assert m.is_safe_output_dir("a/../b") is False
    assert m.is_safe_output_dir("a\\..\\b") is False


def test_scaffold_rejects_output_dir_traversal(tmp_path):
    # An output_dir with a '..' segment must be refused before any copy, so a
    # recipe can't be dropped outside the intended tree.
    outside = tmp_path / "outside"
    outside.mkdir()
    traversal = str(tmp_path / "inside" / ".." / "outside")

    ok = m.scaffold(name="ok-name", output_dir=traversal)

    assert ok is False
    assert not (outside / "ok-name").exists()


# ---------------------------------------------------------------------------
# resilient placeholder replacement (Finding 1)
# ---------------------------------------------------------------------------


def test_scaffold_ignores_replace_placeholder_errors(tmp_path, monkeypatch):
    def boom(*_args, **_kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(m, "replace_in_file", boom)

    ok = m.scaffold(name="rec", output_dir=str(tmp_path))

    # A per-file replacement failure is warned about, not fatal ...
    assert ok is True
    # ... and the template files were still copied.
    assert (tmp_path / "rec" / "README.md").is_file()


# ---------------------------------------------------------------------------
# output_dir trailing-slash normalization (Finding 3)
# ---------------------------------------------------------------------------


def test_scaffold_normalizes_output_dir_trailing_slash(tmp_path):
    out = str(tmp_path) + "/"

    ok = m.scaffold(name="rec", output_dir=out)

    assert ok is True
    readme = (tmp_path / "rec" / "README.md").read_text(encoding="utf-8")
    # The trailing slash is stripped, so no doubled slash appears before the
    # recipe name in the rendered <OUTPUT_DIRECTORY>/<RECIPE_NAME>/ path.
    assert f"{tmp_path}/rec/" in readme
    assert "//rec/" not in readme


# ---------------------------------------------------------------------------
# missing templates directory (Finding 4)
# ---------------------------------------------------------------------------


def test_scaffold_fails_when_templates_missing(tmp_path, monkeypatch):
    real_isdir = os.path.isdir
    templates_suffix = os.path.join("resources", "templates")

    def fake_isdir(path):
        if str(path).endswith(templates_suffix):
            return False
        return real_isdir(path)

    monkeypatch.setattr(m.os.path, "isdir", fake_isdir)

    ok = m.scaffold(name="rec", output_dir=str(tmp_path))

    assert ok is False
    assert not (tmp_path / "rec").exists()


# ---------------------------------------------------------------------------
# junk / cache files must not leak into scaffolded recipes (report gap)
# ---------------------------------------------------------------------------


@pytest.fixture
def templates_with_junk():
    """Create cache junk inside the real templates dir, cleaned up after."""
    skill_dir = Path(m.__file__).resolve().parent.parent
    templates = skill_dir / "resources" / "templates"
    junk_files = [
        templates / ".ruff_cache" / "CACHEDIR.TAG",
        templates / "__pycache__" / "stale.pyc",
    ]
    for f in junk_files:
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_bytes(b"junk")
    try:
        yield templates
    finally:
        for f in junk_files:
            shutil.rmtree(f.parent, ignore_errors=True)


def test_scaffold_omits_cache_junk(tmp_path, templates_with_junk):
    ok = m.scaffold(name="rec", output_dir=str(tmp_path))

    assert ok is True
    target = tmp_path / "rec"
    leaked = [
        p
        for p in target.rglob("*")
        if p.name in {".ruff_cache", "__pycache__"} or p.suffix == ".pyc"
    ]
    assert leaked == []


# ---------------------------------------------------------------------------
# is_allowed_output_dir / CLI --output-dir allow-list
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "output_dir",
    [
        "contrib",
        "contrib/python",
        "core/python",
        "contrib/python/",
        "core/python/",
    ],
)
def test_allows_documented_output_dirs(output_dir):
    """contrib/python is the documented home for new Python recipes. It was
    previously rejected, so scaffolding into it was impossible."""
    assert m.is_allowed_output_dir(output_dir) is True


@pytest.mark.parametrize(
    "output_dir",
    [
        # Vertical plugins need a template this skill does not ship — SKILL.md,
        # EVAL.yaml, scripts/, assets/, references/, tests/unit/ — so they must
        # be refused rather than filled with a recipe-shaped tree.
        "plugins",
        "plugins/retail",
        "plugins/retail/store-ops",
        # Retired roots and other stray locations.
        "python/agents",
        "core",
        "docs",
        "../../other-repo",
        "",
    ],
)
def test_rejects_everything_else(output_dir):
    assert m.is_allowed_output_dir(output_dir) is False


def test_allow_list_matches_the_skill_docs():
    """SKILL.md tells the assistant which locations to offer; drift between
    the prose and the check would send contributors down a dead end."""
    skill_md = (Path(__file__).resolve().parents[1] / "SKILL.md").read_text(
        encoding="utf-8"
    )
    for allowed in m.ALLOWED_OUTPUT_DIRS:
        assert f"`{allowed}/`" in skill_md, allowed
    assert "plugins/<vertical>/<solution>/" in skill_md
