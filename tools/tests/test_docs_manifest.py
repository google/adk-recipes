#!/usr/bin/env python3
"""Keep docs/recipe-handbook/manifest.md in step with the manifest schema.

The page restates the schema's fields for humans, and a restatement drifts
the moment someone edits the schema alone. These tests turn "the page lists
every field" into an executed assertion, in both directions, and check that
the page's complete example is itself a valid manifest.
"""

import re
from pathlib import Path

import validate_manifest as m

REPO_ROOT = Path(__file__).resolve().parents[2]
DOC_PATH = REPO_ROOT / "docs" / "recipe-handbook" / "manifest.md"

# Schema fields the page deliberately leaves out. `large` opts a recipe into
# the relaxed size tier and is kept out of contributor docs on purpose.
UNDOCUMENTED = {"large"}

# A table row whose first cell is a backticked field name: "| `ownership.poc` |".
_FIELD_ROW = re.compile(r"^\|\s*`([a-z_]+(?:\.[a-z_]+)*)`\s*\|", re.MULTILINE)


def _schema_fields(properties: dict, prefix: str = "") -> set[str]:
    """Every settable field, dotted for nested ones (`ownership.poc`).

    An object with properties of its own is a container, not a field: the
    page documents its children, so the container itself is not listed.
    """
    fields: set[str] = set()
    for name, spec in properties.items():
        dotted = f"{prefix}{name}"
        children = spec.get("properties")
        if spec.get("type") == "object" and children:
            fields |= _schema_fields(children, f"{dotted}.")
        else:
            fields.add(dotted)
    return fields


def _doc_fields(text: str) -> set[str]:
    return set(_FIELD_ROW.findall(text))


def test_doc_lists_every_schema_field():
    expected = _schema_fields(m.load_schema()["properties"]) - UNDOCUMENTED
    missing = expected - _doc_fields(DOC_PATH.read_text(encoding="utf-8"))
    assert not missing, (
        f"manifest-schema.json defines {sorted(missing)} but "
        f"{DOC_PATH.relative_to(REPO_ROOT)} has no table row for them."
    )


def test_doc_names_no_field_the_schema_lacks():
    known = _schema_fields(m.load_schema()["properties"])
    extra = _doc_fields(DOC_PATH.read_text(encoding="utf-8")) - known
    assert not extra, (
        f"{DOC_PATH.relative_to(REPO_ROOT)} documents {sorted(extra)}, which "
        "manifest-schema.json does not define."
    )


def test_undocumented_fields_are_still_in_the_schema():
    """An entry here for a field the schema dropped is dead weight."""
    known = _schema_fields(m.load_schema()["properties"])
    assert known >= UNDOCUMENTED, sorted(UNDOCUMENTED - known)


def test_undocumented_fields_stay_off_the_page():
    text = DOC_PATH.read_text(encoding="utf-8")
    shown = {f for f in UNDOCUMENTED if f"`{f}`" in text}
    assert not shown, f"{sorted(shown)} are meant to stay undocumented"


def test_complete_example_is_a_valid_manifest(tmp_path):
    text = DOC_PATH.read_text(encoding="utf-8")
    section = text.split("## Complete example", 1)[1]
    example = re.search(r"```yaml\n(.*?)```", section, re.DOTALL)
    assert example, "no ```yaml block under '## Complete example'"
    manifest = tmp_path / "manifest.yaml"
    manifest.write_text(example.group(1), encoding="utf-8")
    assert m.validate_manifest(manifest, m.load_schema()) == []
