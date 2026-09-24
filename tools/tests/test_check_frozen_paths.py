#!/usr/bin/env python3
"""Unit tests for tools/check_frozen_paths.py.

The interesting logic is pure string handling, so most tests are tables of
path → expectation. `load_frozen_paths` and `main` touch the filesystem and
stdin respectively and get their own small set of tests.
"""

from pathlib import Path

import check_frozen_paths as m
import pytest

FROZEN = [
    "python/agents",
    "java/agents",
    "go/agents",
    "kotlin/agents",
    "typescript/agents",
]


# ---------------------------------------------------------------------------
# frozen_prefix_for — which retired root, if any, does a path sit under
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path,expected",
    [
        # Inside retired roots.
        ("python/agents/foo/agent.py", "python/agents"),
        ("python/agents/foo/tests/test_x.py", "python/agents"),
        ("java/agents/hello/pom.xml", "java/agents"),
        ("go/agents/example/main.go", "go/agents"),
        ("typescript/agents/thing/package.json", "typescript/agents"),
        # The retired root itself, and a file directly inside it.
        ("python/agents", "python/agents"),
        ("python/agents/README.md", "python/agents"),
        # Active locations are untouched.
        ("contrib/python/foo/agent.py", None),
        ("core/python/foo/agent.py", None),
        ("plugins/python/foo/SKILL.md", None),
        # The language README sits OUTSIDE the frozen prefix on purpose —
        # the deprecation notice has to stay editable.
        ("python/README.md", None),
        ("java/README.md", None),
        # Repo infrastructure.
        ("tools/validate.py", None),
        (".github/workflows/python-tests.yml", None),
        ("README.md", None),
        # Component-boundary matching: a sibling directory whose name merely
        # starts with the frozen prefix must not match.
        ("python/agents-archive/foo.py", None),
        ("pythonic/agents/foo.py", None),
    ],
)
def test_frozen_prefix_for(path, expected):
    assert m.frozen_prefix_for(path, FROZEN) == expected


def test_frozen_prefix_for_tolerates_leading_slash():
    assert m.frozen_prefix_for("/python/agents/foo/x.py", FROZEN) == (
        "python/agents"
    )


def test_frozen_prefix_for_empty_config_matches_nothing():
    assert m.frozen_prefix_for("python/agents/foo/x.py", []) is None


# ---------------------------------------------------------------------------
# retired_recipe_dir — collapse a file path to the recipe it belongs to
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path,prefix,expected",
    [
        ("python/agents/foo/agent.py", "python/agents", "python/agents/foo"),
        (
            "python/agents/foo/app/sub/deep.py",
            "python/agents",
            "python/agents/foo",
        ),
        ("java/agents/hello/pom.xml", "java/agents", "java/agents/hello"),
        # Nothing after the prefix — degrade to the prefix itself.
        ("python/agents", "python/agents", "python/agents"),
    ],
)
def test_retired_recipe_dir(path, prefix, expected):
    assert m.retired_recipe_dir(path, prefix) == expected


# ---------------------------------------------------------------------------
# suggested_destination — the "move it here" hint
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "recipe_dir,expected",
    [
        ("python/agents/foo", "contrib/python/foo"),
        ("java/agents/hello", "contrib/java/hello"),
        ("go/agents/example", "contrib/go/example"),
        ("typescript/agents/thing", "contrib/typescript/thing"),
        ("kotlin/agents/demo", "contrib/kotlin/demo"),
        # Too short to name a recipe — generic hint.
        ("python/agents", "contrib/python/<recipe>"),
    ],
)
def test_suggested_destination(recipe_dir, expected):
    assert m.suggested_destination(recipe_dir) == expected


# ---------------------------------------------------------------------------
# find_violations — grouping and deduplication
# ---------------------------------------------------------------------------


def test_no_violations_for_active_paths():
    changed = [
        "contrib/python/foo/agent.py",
        "core/python/bar/README.md",
        "tools/validate.py",
        "python/README.md",
    ]
    assert m.find_violations(changed, FROZEN) == {}


def test_many_files_in_one_recipe_collapse_to_one_entry():
    changed = [
        "python/agents/foo/agent.py",
        "python/agents/foo/README.md",
        "python/agents/foo/tests/test_agent.py",
    ]
    violations = m.find_violations(changed, FROZEN)
    assert list(violations) == ["python/agents/foo"]
    # The example file kept is the first one seen.
    assert violations["python/agents/foo"] == "python/agents/foo/agent.py"


def test_multiple_recipes_and_languages_each_reported():
    changed = [
        "python/agents/foo/agent.py",
        "python/agents/bar/agent.py",
        "java/agents/hello/pom.xml",
        "contrib/python/ok/agent.py",
    ]
    violations = m.find_violations(changed, FROZEN)
    assert set(violations) == {
        "python/agents/foo",
        "python/agents/bar",
        "java/agents/hello",
    }


def test_blank_lines_are_ignored():
    changed = ["", "   ", "python/agents/foo/agent.py", ""]
    assert list(m.find_violations(changed, FROZEN)) == ["python/agents/foo"]


def test_mixed_pr_still_flags_the_retired_part():
    """A PR that moves a recipe adds files under contrib/ and touches the
    retired copy. Only the retired side is flagged."""
    changed = [
        "contrib/python/foo/agent.py",
        "contrib/python/foo/manifest.yaml",
        "python/agents/foo/agent.py",
    ]
    assert list(m.find_violations(changed, FROZEN)) == ["python/agents/foo"]


# ---------------------------------------------------------------------------
# load_frozen_paths — policy.yml reading
# ---------------------------------------------------------------------------


def _write_policy(tmp_path: Path, body: str) -> Path:
    policy = tmp_path / "policy.yml"
    policy.write_text(body, encoding="utf-8")
    return policy


def test_load_frozen_paths_reads_and_normalises(tmp_path):
    policy = _write_policy(
        tmp_path,
        "frozen_paths:\n  - python/agents\n  - /java/agents/\n",
    )
    assert m.load_frozen_paths(policy) == ["python/agents", "java/agents"]


@pytest.mark.parametrize(
    "body",
    [
        "frozen_paths: []\n",
        "frozen_paths:\n",
        "recipe_naming:\n  max_folder_name_length: 30\n",
        "",
    ],
)
def test_load_frozen_paths_missing_or_empty(tmp_path, body):
    assert m.load_frozen_paths(_write_policy(tmp_path, body)) == []


def test_real_policy_file_declares_the_retired_roots():
    """Guards against the policy entry being dropped by accident."""
    frozen = m.load_frozen_paths()
    assert "python/agents" in frozen
    for language in ("java", "go", "kotlin", "typescript"):
        assert f"{language}/agents" in frozen


# ---------------------------------------------------------------------------
# main — stdin plumbing and exit codes
# ---------------------------------------------------------------------------


def test_main_passes_on_clean_input(monkeypatch, capsys):
    monkeypatch.setattr(
        "sys.stdin", _FakeStdin("contrib/python/foo/agent.py\n")
    )
    assert m.main() == 0
    assert "[PASS]" in capsys.readouterr().out


def test_main_fails_and_annotates_on_retired_path(monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", _FakeStdin("python/agents/foo/agent.py\n"))
    assert m.main() == 1
    out = capsys.readouterr().out
    assert "::error file=python/agents/foo/agent.py::" in out
    assert "contrib/python/foo" in out
    assert "ACTION REQUIRED" in out


def test_main_passes_on_empty_input(monkeypatch, capsys):
    monkeypatch.setattr("sys.stdin", _FakeStdin(""))
    assert m.main() == 0
    assert "[PASS]" in capsys.readouterr().out


def test_main_skips_when_no_frozen_paths(monkeypatch, capsys):
    monkeypatch.setattr(m, "load_frozen_paths", lambda *a, **k: [])
    monkeypatch.setattr("sys.stdin", _FakeStdin("python/agents/foo/agent.py\n"))
    assert m.main() == 0
    assert "[SKIP]" in capsys.readouterr().out


class _FakeStdin:
    """Minimal stand-in for sys.stdin supporting the single read() call."""

    def __init__(self, text: str):
        self._text = text

    def read(self) -> str:
        return self._text
