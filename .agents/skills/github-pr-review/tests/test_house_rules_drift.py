"""Pin .github/review-rules.md and check_house_rules.py to the repo's real validators.

check_house_rules.py reimplements logic that lives in six places under .github/
and tools/. Nothing keeps them in step. When the repo changes a rule, this script
keeps confidently reporting the old one -- and it is the component most likely to
be believed, because it is a script rather than a model.

These tests read the ACTUAL repo files and fail when they diverge. They are
deliberately loose: they assert the anchor facts each rule depends on, not exact
source text, so ordinary refactors don't trip them.

Skipped automatically when run outside an adk-samples checkout.
"""

import json
import os
import re
from pathlib import Path

import pytest


def _find_repo():
    """Locate an adk-samples checkout, wherever the skill happens to be installed.

    Works when the skill lives inside the checkout (.agents/skills/<name>/tests) and
    when it lives anywhere else, via ADK_SAMPLES_ROOT. Returns None if neither, and
    the whole module then skips -- these tests are only meaningful against the real
    repo, and a teammate running the suite outside one should not see failures.
    """
    env = os.environ.get("ADK_SAMPLES_ROOT")
    if env and (Path(env) / "AGENTS.md").exists():
        return Path(env)
    for parent in Path(__file__).resolve().parents:
        if (parent / "AGENTS.md").exists() and (parent / ".github").is_dir():
            return parent
    return None


REPO = _find_repo()
RULES_MD = (REPO / ".github" / "review-rules.md") if REPO else None

pytestmark = pytest.mark.skipif(
    REPO is None,
    reason="no adk-samples checkout found; set ADK_SAMPLES_ROOT to run these",
)


def read(rel):
    p = REPO / rel
    if not p.exists():
        pytest.skip(f"{rel} not present in this checkout")
    return p.read_text(encoding="utf-8", errors="replace")


# ------------------------------------------------------------- H3 / H4 / H5


def test_h4_python_311_is_still_the_minimum():
    """H4 hardcodes 3.11. If the repo bumps it, the rule and script are wrong."""
    agents = read("AGENTS.md")
    assert re.search(r"[Mm]inimum python version:\s*3\.11", agents), (
        "AGENTS.md no longer says 3.11 -- update H4 and check_house_rules.check_pyproject"
    )


def test_h3_h4_h5_validator_still_exists():
    """The three CI-FAIL pyproject rules all cite this script."""
    src = read(".github/scripts/check_recipe_pyproject.py")
    for marker in ("name", "requires-python", "index"):
        assert marker in src, (
            f"check_recipe_pyproject.py no longer mentions {marker}"
        )


# -------------------------------------------------------------------- H1 / H2


def test_h1_h2_ruff_config_ban_is_still_repo_policy():
    agents = read("AGENTS.md")
    assert "ruff.toml" in agents and "root" in agents.lower(), (
        "AGENTS.md no longer bans standalone ruff config -- recheck H1/H2"
    )


# ------------------------------------------------------------------------ H10


def test_h10_deprecated_model_ids_match_agents_md():
    """The banned list and the replacement both come from AGENTS.md."""
    agents = read("AGENTS.md")
    rules = RULES_MD.read_text(encoding="utf-8")
    for model in ("gemini-2.0-flash", "gemini-2.5-flash"):
        assert model in agents, (
            f"{model} no longer listed as deprecated in AGENTS.md"
        )
        assert model in rules, (
            f"{model} missing from .github/review-rules.md H10"
        )
    m = re.search(r"[Uu]se\s+`?(gemini-[\w.\-]+)`?\s+instead", agents)
    assert m, "AGENTS.md no longer names a replacement model"
    assert m.group(1) in rules, (
        f"AGENTS.md now recommends {m.group(1)}; .github/review-rules.md H10 says otherwise"
    )


# ------------------------------------------------------------------------ H19


def test_h19_manifest_schema_keys_are_current():
    """H19 validates against the live schema, but the doc lists enums inline."""
    schema = json.loads(read(".github/schemas/manifest-schema.json"))
    rules = RULES_MD.read_text(encoding="utf-8")
    assert schema.get("additionalProperties") is False, (
        "schema no longer forbids extra keys -- H19's premise is gone"
    )
    for key in schema.get("required", []):
        assert key in rules, f"required manifest key {key!r} missing from H19"


def test_h19_enums_in_the_doc_match_the_schema():
    schema = json.loads(read(".github/schemas/manifest-schema.json"))
    rules = RULES_MD.read_text(encoding="utf-8")
    for field in ("type", "status", "language"):
        spec = schema.get("properties", {}).get(field, {})
        for value in spec.get("enum", []):
            assert value in rules, (
                f"{field} enum value {value!r} is in the schema but not in H19"
            )


# ------------------------------------------------------------------------ H21


def test_h21_required_files_match_policy_yml():
    text = read(".github/policy.yml")
    for required in (
        "README.md",
        "pyproject.toml",
        "uv.lock",
        ".env.example",
        "test_runnability.py",
    ):
        assert required in text, (
            f"{required} no longer in policy.yml -- H21's list is stale"
        )


def test_h22_folder_name_limit_matches_policy():
    text = read(".github/policy.yml")
    m = re.search(r"max_folder_name_length:\s*(\d+)", text)
    if not m:
        pytest.skip("policy.yml no longer declares max_folder_name_length")

    limit = int(m.group(1))
    assert limit == 30, (
        f"policy.yml says {limit}; check_house_rules.check_layout hardcodes 30"
    )


# ------------------------------------------------------------------------ H24


def test_h24_frozen_paths_are_still_frozen():
    text = read(".github/policy.yml")
    assert "frozen_paths" in text, (
        "policy.yml dropped frozen_paths -- H24 is obsolete"
    )
    assert "python/agents" in text, (
        "python/agents no longer frozen -- recheck H24"
    )


# ---------------------------------------------------------------- doc hygiene


def test_every_rule_declares_ci_fail_or_advisory():
    """Getting this wrong produces the most expensive comment the skill can make."""
    rules = RULES_MD.read_text(encoding="utf-8")
    reportable = rules.split("### Report these")[1].split(
        "### Already enforced"
    )[0]
    found = re.findall(
        r"\*\*(H\d+)\*\*\s*·\s*\*\*(CI-FAIL|advisory)\*\*", reportable
    )
    ids = re.findall(r"^\s*\d+\.\s+\*\*(H\d+)\*\*", reportable, re.M)
    assert ids, "no rules found in the 'Report these' section"
    tagged = {r for r, _ in found}
    missing = [r for r in ids if r not in tagged]
    assert not missing, (
        f"these reportable rules do not declare CI-FAIL or advisory: {missing}"
    )


# ------------------------------------------------------- rule coverage


def _reportable_rule_ids():
    """Every H-id in the 'Report these' section of the rules doc."""
    text = RULES_MD.read_text(encoding="utf-8")
    section = text.split("### Report these")[1].split("### Already enforced")[0]
    return sorted(
        set(re.findall(r"\*\*(H\d+)\*\*", section)), key=lambda r: int(r[1:])
    )


def _implemented_rule_ids():
    src = (
        Path(__file__).parent.parent / "scripts" / "check_house_rules.py"
    ).read_text(encoding="utf-8")
    return set(re.findall(r'find\(\s*out,\s*"(H\d+)"', src))


def test_every_reportable_rule_is_implemented_or_declared_model_judged():
    """The gap this test exists to close.

    Before it, H39 H40 H41 H42 H43 H44 and H47 were all listed as reportable in
    .github/review-rules.md, implemented nowhere, and absent from the script's
    own list of rules it deliberately skips. Nothing checked them and nothing
    said so -- the worst of the three states, because the script's silence reads
    as "no violations".
    """
    import check_house_rules

    covered = _implemented_rule_ids() | set(check_house_rules.MODEL_JUDGED)
    missing = [r for r in _reportable_rule_ids() if r not in covered]
    assert not missing, (
        f"reportable but unchecked: {missing}. Implement each in "
        "check_house_rules.py, or add it to MODEL_JUDGED with the reason a "
        "script gets it wrong."
    )


def test_model_judged_rules_are_actually_in_the_rules_doc():
    """The reverse drift: a rule retired from the doc but still excused here."""
    import check_house_rules

    reportable = set(_reportable_rule_ids())
    stale = [r for r in check_house_rules.MODEL_JUDGED if r not in reportable]
    assert not stale, (
        f"MODEL_JUDGED excuses {stale}, which the rules doc no longer asks "
        "anyone to report"
    )


def test_no_rule_is_both_implemented_and_excused():
    import check_house_rules

    both = _implemented_rule_ids() & set(check_house_rules.MODEL_JUDGED)
    assert not both, (
        f"{sorted(both)} are implemented AND listed as model-judged; the lane "
        "will report each of them twice"
    )
