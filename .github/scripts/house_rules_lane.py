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
"""Run the deterministic house-rules checker over the recipes a PR touches.

Used by .github/workflows/ai-pr-review-house-rules.yml. This is the fifth
review lane and the only one with no model in it.

The other four lanes ask a model to judge repository conventions from prose
injected into its prompt, with no tools and no checkout. Most of those
conventions are not judgement calls at all -- a `[tool.ruff]` table either is
in the file or is not -- and a model that gets one wrong produces the single
most expensive comment this system can make: a confident, specific, false
"this will fail CI". `.agents/skills/github-pr-review/scripts/check_house_rules.py`
already decides those rules by reading the files, so this lane runs it and
turns its findings into the same shape the model lanes emit.

Two properties make that safe to run against a pull request's own code:

  NOTHING FROM THE PR IS EXECUTED.  The checker parses. It reads text, walks
  directories, calls tomllib and `ast.parse`, and runs `git ls-files`. It
  never imports, execs, or installs anything out of the tree under review, so
  a hostile recipe gets no more privilege than a hostile text file.

  THE CHECKER ITSELF COMES FROM THE BASE BRANCH.  --checker points at the base
  checkout, never at the PR's copy. Otherwise a PR could rewrite the script
  that reviews it, which is the same reason the workflow injects the rules
  from the base branch rather than the head.

Usage:
  python3 house_rules_lane.py \
    --checker <base>/.agents/skills/github-pr-review/scripts/check_house_rules.py \
    --repo-root <head checkout> \
    --changed-files changed.txt \
    --out findings.json

Exit codes:
  0  findings written (possibly an empty list)
  2  CI fault -- the checker could not be loaded or run
"""

import argparse
import importlib.util
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))
from ci_message import (
    EXIT_OK,
    guard,
    infra_fault,
    report_infra_fault,
)

CHECKER = "house_rules_lane.py"

# The manifest schema comes from the BASE checkout — this file's own directory
# — never from the tree under review.
SCHEMA_PATH = (
    Path(__file__).resolve().parents[1] / "schemas" / "manifest-schema.json"
)

# A PR touching more recipes than this gets the first N checked. The job has a
# ten-minute timeout and each recipe is a git subprocess plus six walks.
MAX_RECIPES = 40

# The areas a recipe can live in. Everything else in the repo is tooling.
RECIPE_AREAS = ("core/", "contrib/", "skills/")

# Files that mark a directory as a vertical skill's root when no manifest has
# been written yet. From policy.yml required_files.by_root.skills.
SOLUTION_MARKERS = ("SKILL.md", "EVAL.yaml")


def recipe_roots(
    changed: list[str], repo_root: Path | None = None
) -> list[str]:
    """Every recipe directory the PR touches, in a stable order.

    A recipe is identified by the presence of its manifest.yaml, not by
    counting path segments. Segment-counting got this wrong in both
    directions at once:

      core/rag-agent-search/app/agent.py  ->  "core/rag-agent-search/app"
          a real recipe's subdirectory, reported as a recipe of its own, which
          then collects CI-FAIL comments for every required file it lacks
      skills/store-ops/manifest.yaml      ->  nothing
          a solution placed directly under skills/ -- which is H41's exact
          target, so the rule could never see the thing it exists to report

    Both are real paths in this repository today. Walking up to the manifest
    answers the question actually being asked: which recipe does this file
    belong to?
    """
    roots = set()
    for raw in changed:
        path = raw.strip()
        if not path.startswith(RECIPE_AREAS):
            continue
        if repo_root is None:
            # No tree to consult. Fall back to counting segments, which is
            # what this did before and is wrong in the two ways described
            # above — but a caller with no checkout has nothing better, and
            # the production caller always passes one. The trailing-slash
            # requirement keeps `contrib/python/README.md`, a file sitting
            # BESIDE the recipes, from parsing as a recipe named README.md.
            parts = path.split("/")
            if len(parts) > 3:
                roots.add("/".join(parts[:3]))
            continue
        # Walk up from the file to the nearest directory holding a manifest,
        # stopping before the area directory itself.
        current = Path(path).parent
        found = False
        while len(current.parts) >= 2:
            if (repo_root / current / "manifest.yaml").is_file():
                roots.add(current.as_posix())
                found = True
                break
            current = current.parent
        if found:
            continue
        # No manifest anywhere above it. That is not "not a recipe" — it is
        # most likely a NEW recipe whose manifest has not been written yet,
        # which is the case most in need of review. Found on a live PR that
        # added contrib/python/software-bug-assistant with a Dockerfile and a
        # README and no manifest: the lane reported nothing to do.
        #
        # Requires four segments, so a file sitting BESIDE the recipes
        # (contrib/python/README.md) still resolves to nothing.
        parts = path.split("/")
        # Under skills/ a solution may sit at depth 2 (misplaced -- which is
        # H41's entire subject) or depth 3 (correct). With no manifest to
        # settle it, a marker file does: skills/store-ops/SKILL.md means the
        # root is skills/store-ops, and the four-segment rule would otherwise
        # resolve its scripts/ and tests/ as two separate recipes while
        # missing the real one.
        if parts[0] == "skills" and len(parts) >= 3 and repo_root is not None:
            shallow = "/".join(parts[:2])
            if any(
                (repo_root / shallow / marker).is_file()
                for marker in SOLUTION_MARKERS
            ):
                roots.add(shallow)
                continue
        if len(parts) > 3:
            roots.add("/".join(parts[:3]))
    return sorted(roots)


def load_checker(path: Path):
    """Import check_house_rules.py from an explicit path.

    Imported rather than shelled out to: the module keeps per-run state (the
    changed-file set, the git-tracked cache, the skipped-rule list) that has to
    be reset between recipes, and doing that in-process is both cheaper and
    honest about the coupling.
    """
    spec = importlib.util.spec_from_file_location("check_house_rules", path)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load a module from {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# The citation is what makes a rule comment checkable rather than arbitrary,
# but `evidence` can be six full repository paths joined with "; ". Appended
# whole it pushed bodies to 788 characters, past the 600-character shape gate
# in post_review_comments.py -- which then DROPPED them, so the longest and
# most-cited findings were the ones the author never saw. 14% of this repo's
# house-rule findings were lost that way.
MAX_EVIDENCE_CHARS = 180


def _short_evidence(evidence) -> str:
    """A citation that fits, keeping the first source and counting the rest."""
    text = str(evidence or "").strip()
    if len(text) <= MAX_EVIDENCE_CHARS:
        return text
    parts = [p.strip() for p in text.split(";") if p.strip()]
    if len(parts) > 1:
        head = parts[0]
        if len(head) <= MAX_EVIDENCE_CHARS - 20:
            return f"{head}; and {len(parts) - 1} more"
    return text[: MAX_EVIDENCE_CHARS - 1] + "…"


def to_reviewer_finding(finding: dict) -> dict:
    """Translate a checker finding into the shape the posting script reads.

    `window` is deliberately left out. The posting script treats a window as
    the model's claim about what the source says and rejects a finding whose
    claim is wrong -- that check exists because a model that invents a finding
    invents the source too. A deterministic checker read the file, so there is
    no claim to audit, and supplying a window would only add a way for this
    lane to fail.
    """
    body = str(finding.get("what") or "").strip()
    evidence = _short_evidence(finding.get("evidence"))
    if evidence and evidence not in body:
        # The citation is what makes a rule comment checkable rather than
        # arbitrary: a file the author can open beats a rule id they cannot.
        body = f"{body} ({evidence})"
    return {
        "path": finding.get("path"),
        "line": finding.get("line") or 1,
        "body": body,
        "verify_steps": finding.get("verify_steps") or "",
        "_rule": finding.get("rule"),
        "_ci": finding.get("ci"),
    }


def run_checker(module, repo_root: str, recipe: str, changed: set[str] | None):
    """Findings for one recipe, with the module's per-run state reset first."""
    module.SKIPPED = []
    module.FILTERED = []
    module.CHANGED = changed
    module.NEW_RECIPE = bool(changed) and any(
        c.startswith(recipe + "/")
        and c.endswith(("manifest.yaml", "pyproject.toml"))
        for c in changed
    )

    name = recipe.rsplit("/", 1)[-1]
    # The BASE schema, never the PR's copy. The workflow takes care to run the
    # base branch's checker so a PR cannot rewrite the script that reviews it;
    # reading the schema out of the PR head handed back exactly that — adding
    # a property there neuters H19's unknown-key check, and enum values from
    # it are echoed verbatim into a posted comment.
    schema = str(SCHEMA_PATH)
    pyproject = module.load_toml(
        str(Path(repo_root) / recipe / "pyproject.toml")
    )
    # Through the checker's own guard: `project = "oops"` in a pyproject is a
    # realistic typo, and .get() on a str raises out of here, which the caller
    # catches per recipe and turns into a silently unreviewed recipe.
    pyproject_name = module._table(pyproject or {}, "project").get("name", "")

    out: list[dict] = []
    module.check_pyproject(out, repo_root, recipe, name)
    module.check_uv_lock(out, repo_root, recipe, name, pyproject_name)
    module.check_dotenv_bootstrap(out, repo_root, recipe)
    module.check_manifest(out, repo_root, recipe, schema)
    module.check_readme(out, repo_root, recipe)
    module.check_layout(out, repo_root, recipe, name)
    module.check_text_wide(out, repo_root, recipe)
    module.check_env_defaults(out, repo_root, recipe)
    module.check_license_headers(out, repo_root, recipe)
    # NOT check_pr_shape: H42 is a property of the pull request, not of a
    # recipe, and `out` is per recipe so the checker's own once-guard cannot
    # see across them. main() calls it once, after the loop.
    return out, list(module.SKIPPED)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Deterministic house-rule findings for a pull request."
    )
    parser.add_argument(
        "--checker",
        required=True,
        type=Path,
        help="check_house_rules.py from the BASE checkout, never the PR's copy",
    )
    parser.add_argument(
        "--repo-root",
        required=True,
        type=Path,
        help="checkout of the PR head; read as data, never executed",
    )
    parser.add_argument(
        "--changed-files",
        required=True,
        type=Path,
        help="one PR-changed path per line",
    )
    parser.add_argument(
        "--out", required=True, type=Path, help="findings destination"
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()

    try:
        changed = [
            line.strip()
            for line in args.changed_files.read_text(
                encoding="utf-8", errors="replace"
            ).splitlines()
            if line.strip()
        ]
    except OSError as exc:
        return report_infra_fault(
            infra_fault(CHECKER, f"cannot read {args.changed_files}: {exc}")
        )

    roots = recipe_roots(changed, args.repo_root)
    if len(roots) > MAX_RECIPES:
        # Each recipe costs a git subprocess and six directory walks against a
        # ten-minute job timeout. Reviewing the first N and saying so beats a
        # red check that reviewed nothing.
        # A plain line: the repo routes contributor-facing annotations
        # through ci_message, and this is a note for whoever reads the job.
        print(f"{len(roots)} recipes touched; checking the first {MAX_RECIPES}")
        roots = roots[:MAX_RECIPES]
    if not roots:
        print("No recipe directories in this PR; nothing for this lane to do.")
        args.out.write_text("[]", encoding="utf-8")
        return EXIT_OK

    try:
        module = load_checker(args.checker)
    except Exception as exc:
        return report_infra_fault(
            infra_fault(CHECKER, f"cannot load {args.checker}: {exc}")
        )

    findings: list[dict] = []
    recipe_findings = 0
    failed: list[str] = []
    for recipe in roots:
        if not (args.repo_root / recipe).is_dir():
            # Deleted, or renamed away. Nothing to check and nothing wrong.
            continue
        try:
            raw, skipped = run_checker(
                module, str(args.repo_root), recipe, set(changed)
            )
        except Exception as exc:
            # One unreadable recipe must not cost the review every finding in
            # the others. A plain line, not an annotation: nothing a
            # contributor reading their PR can act on, and ci_message owns the
            # annotations a contributor does see.
            print(f"  house-rules check failed for {recipe}: {exc}")
            failed.append(recipe)
            continue
        print(f"{recipe}: {len(raw)} finding(s)")
        # A rule that could not be evaluated is not a rule that passed.
        for rule, why in skipped:
            print(f"  not checked — {rule}: {why}")
        # Nor is one dropped as pre-existing. Without this line a rule that
        # fired and was filtered is indistinguishable from a rule that found
        # nothing, which is what made an anchor landing outside the diff
        # invisible rather than merely wrong.
        if module.FILTERED:
            from collections import Counter

            counts = Counter(rule for rule, _ in module.FILTERED)
            print(
                "  not attributed to this PR: "
                + ", ".join(f"{r}x{n}" for r, n in sorted(counts.items()))
            )
        recipe_findings += len(raw)
        findings.extend(to_reviewer_finding(f) for f in raw)

    # H42 once for the whole run, anchored in the first recipe. Called inside
    # the loop it produced one identical comment per recipe on one line — and
    # at exactly three recipes the grouping pass collapsed them into "the same
    # thing in 2 other places", which is false: it is the same place, thrice.
    if roots:
        shape: list[dict] = []
        try:
            module.CHANGED = set(changed)
            module.check_pr_shape(shape, str(args.repo_root), roots[0])
        except Exception as exc:
            print(f"  PR-shape check failed: {exc}")
        findings.extend(to_reviewer_finding(f) for f in shape)

    # CI-failing findings first: an author acts on "this blocks the build" and
    # may never act on a convention nit.
    findings.sort(key=lambda f: (f.get("_ci") != "fail", f.get("path") or ""))

    args.out.write_text(json.dumps(findings, indent=1), encoding="utf-8")
    print(f"{len(findings)} finding(s) across {len(roots)} recipe(s).")

    if failed:
        print(f"{len(failed)} recipe(s) could not be checked: {failed}")
    # `recipe_findings`, not `findings`: the PR-shape check runs outside the
    # per-recipe loop and contributes to the total, so one advisory nit from
    # it was enough to make "every recipe failed" look like "we found
    # something" and the lane exited green on a wholly broken checker.
    if failed and not recipe_findings:
        # Every recipe raised and nothing was found: an empty findings file is
        # indistinguishable from a clean PR, and the PR goes green with no
        # review and nobody told. That is the one case worth failing for --
        # it means the checker is broken, not the contribution.
        return report_infra_fault(
            infra_fault(
                CHECKER,
                f"every recipe failed to check ({failed}); refusing to report "
                "a clean review",
            )
        )
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(guard(CHECKER, main))
