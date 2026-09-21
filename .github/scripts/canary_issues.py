#!/usr/bin/env python3
"""
Drive the issue lifecycle for .github/workflows/recipe-canary.yml.

Reads the canary's per-job results and decides, for each recipe, what to do
about its tracking issue: open one, nudge it, escalate it, or close it.

The lifecycle, advanced ONE RUNG PER FAILING RUN
------------------------------------------------
    run 1   open an issue, notify the recipe's owner
    run 2   reminder comment
    run 3   the recipe should be marked `status: inactive`
    run 4   warn that deletion is scheduled
    run 5   propose deleting the recipe, tagging a maintainer
    passing close the issue

On the monthly cron that is roughly a month per rung, so about four months
from the first failure to a deletion proposal. The ladder can stretch — a
missed or delayed run costs a rung, which is the safe direction — but it can
never compress, so an owner always gets four separate notices before deletion
is proposed. See the comment on LADDER for why an age-in-days gate did not
survive contact with a monthly cron.

The issue carries the state deliberately: no new manifest field, no
archaeology over git history to work out when something started failing, and
a rung that cannot fire twice because the label recording it is right there
on the issue.

Why "ask" and not "do"
----------------------
Three rungs want to change files on `main`, which is protected: 1 approving
review AND a code-owner review. A workflow authenticating with GITHUB_TOKEN
cannot push there, and its approvals do not count toward either requirement —
that is exactly why 98 Dependabot PRs sat unmergeable for months.

So this script does not write to the repository at all. It has no YAML
library, opens no pull request, and the `report` job that runs it is declared
`contents: read`. Every file change the ladder calls for is left to a human,
said plainly on the issue. That is a real limitation, not a temporary one to
paper over: an earlier version promised "a pull request will be opened
automatically" whenever a CANARY_APP_TOKEN secret was set, and since no code
ever opened one, setting that secret would have made the canary lie on a
public issue.

Building the write side means a comment-preserving YAML editor (these
manifests are mostly comments), an identity that can push, and its own
review — tracked separately.

Notifying the owner
-------------------
`manifest.ownership.poc` is the source of truth, but only 3 of 12 declared
owners can actually be ASSIGNED — the rest have no repo access, and GitHub
silently drops an invalid assignee, creating the issue unassigned with no
error. So the @-mention in the body is the real delivery mechanism and
assignment is a bonus applied only when it will work.

Usage:
    python canary_issues.py --results results.json
    python canary_issues.py --results results.json --dry-run
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
REPO = os.environ.get("GITHUB_REPOSITORY", "google/adk-recipes")

# Who hears about it when a recipe has no usable owner, and who is tagged for
# a decision once the owner has not responded.
MAINTAINER = "happyhuman"

TITLE_PREFIX = "Recipe canary:"

LABEL_TRACKING = "recipe-canary"
LABEL_ROTTING = "recipe:rotting"
LABEL_DELETION = "recipe:deletion-scheduled"

LABEL_REMINDED = "recipe:reminded"
LABEL_PROPOSED = "recipe:deletion-proposed"

# The ladder, in order. The LABEL IS THE STATE — the whole state machine is
# "which of these labels does the issue already carry", which is durable,
# visible, and free to inspect.
#
# Escalation advances by RUN, not by clock: one rung per failing scheduled
# run, in this order. An earlier version gated each rung on an age in days
# (30/60/90/120) as well, and the interaction between the two was a bug.
# Thresholds sit 30 days apart while a monthly cron advances the clock by
# 28-31, so whether a run crossed one threshold, two, or none depended on
# month length and on the arbitrary phase between issue creation and the
# cron. An issue opened on 31 January could reach 1 April at age 60 and fire
# "here is a reminder" and "this is being marked inactive" in the same run,
# with no grace between them at all; `.days` flooring separately lost a rung
# outright for issues opened in short months.
#
# Advancing one rung per run removes the arithmetic entirely. The ladder can
# stretch — a missed or delayed run costs a month, which is the safe
# direction — but it can never compress, so an owner always gets four
# separate monthly notices before deletion is proposed. Age is still shown in
# the comment text, where being approximate is fine; it just no longer drives
# an irreversible decision.
LADDER = (
    LABEL_REMINDED,
    LABEL_ROTTING,
    LABEL_DELETION,
    LABEL_PROPOSED,
)

# Minimum days between rungs, as a floor rather than a driver. A floor can
# only ever delay a rung, never skip or double-fire one, so it does not
# reintroduce the collapse above. It exists so that changing the cron to
# something more frequent cannot quietly turn a four-month ladder into a
# four-week one.
MIN_DAYS_BETWEEN_STAGES = 21

# Upper bound on how many failing recipes one run will act on. Sized well
# above routine rot — the canary tests 11 recipes today and a normal month
# fails none — and well below "something is systemically wrong". Mirrors
# DEFAULT_MAX_CLOSE in close_orphan_dependabot_prs.py, which exists for the
# same reason: destructive-ish automation should refuse a batch that implies
# its own inputs are broken.
DEFAULT_MAX_ISSUES = 8


class GhError(RuntimeError):
    """A `gh` invocation failed. Never silently swallowed: a canary that
    cannot file issues must fail its run, not report success having told
    nobody."""


def gh(*args: str, check: bool = True) -> str:
    result = subprocess.run(
        ["gh", *args], capture_output=True, text=True, check=False
    )
    if check and result.returncode != 0:
        raise GhError(
            f"gh {' '.join(args)} failed ({result.returncode}): "
            f"{result.stderr.strip()}"
        )
    return result.stdout.strip()


def issue_title(recipe: str) -> str:
    """Stable across runs — it is the dedupe key.

    Anything varying (a version, a date, the error text) would stop the canary
    recognising its own issue and it would file a fresh one every month.
    """
    return f"{TITLE_PREFIX} {recipe} is failing"


def read_owner(recipe: str, repo_root: Path | None = None) -> str | None:
    """`ownership.poc` from the recipe's manifest, without a YAML dependency.

    Returns None when the manifest is missing, unreadable, or has no poc — the
    caller falls back to the maintainer rather than staying silent, because a
    broken recipe with a broken manifest is the last thing that should vanish.
    """
    root = REPO_ROOT if repo_root is None else repo_root
    manifest = root / recipe / "manifest.yaml"
    try:
        lines = manifest.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    in_ownership = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith("ownership:"):
            in_ownership = True
            continue
        if in_ownership:
            if stripped and not line.startswith((" ", "\t")):
                break  # left the ownership block
            if stripped.startswith("poc:"):
                value = stripped.split(":", 1)[1].strip()
                value = value.split("#", 1)[0].strip().strip("\"'")
                return value or None
    return None


def is_assignable(user: str) -> bool:
    """GitHub drops an unknown assignee silently, so ask before assigning."""
    result = subprocess.run(
        ["gh", "api", f"repos/{REPO}/assignees/{user}"],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.returncode == 0


def open_issues_by_title() -> dict[str, dict]:
    """Every open canary issue, indexed by title. ONE `gh` call.

    Fetched once per run rather than once per recipe. The query is identical
    every time and returns the same page of up to 200 issues, so calling it
    inside the recipe loop meant n subprocesses and n x 200 titles scanned to
    answer a question one call already answers. It is also n chances to hit a
    rate limit on a run whose whole job is to be dependable.

    Taking a single snapshot is safe because each recipe is visited exactly
    once, and the only issue this run creates for a recipe is created after
    that recipe's lookup.
    """
    raw = gh(
        "issue",
        "list",
        "--repo",
        REPO,
        "--state",
        "open",
        "--label",
        LABEL_TRACKING,
        "--limit",
        "200",
        "--json",
        "number,title,createdAt,labels",
    )
    index: dict[str, dict] = {}
    for issue in json.loads(raw or "[]"):
        issue["labelNames"] = {lbl["name"] for lbl in issue["labels"]}
        index[issue["title"]] = issue
    return index


def find_issue(
    recipe: str, index: dict[str, dict] | None = None
) -> dict | None:
    """The open canary issue for this recipe, if one exists.

    Exact title match, never a prefix: `rag-agent-search` must not be handed
    the issue belonging to `rag-agent-search-v2`, which would escalate one
    recipe's ladder on another's failures.

    `index` is the snapshot from `open_issues_by_title`. Omitting it fetches
    one for this single lookup, which is convenient for a one-off call and
    wrong inside a loop.
    """
    if index is None:
        index = open_issues_by_title()
    return index.get(issue_title(recipe))


def age_days(created_at: str, now: datetime | None = None) -> int:
    created = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
    current = now or datetime.now(timezone.utc)
    return (current - created).days


def failing_recipes(results: list[dict]) -> dict[str, list[dict]]:
    """Recipes with at least one FAILED job, mapped to those jobs.

    `infra` outcomes are ignored on purpose. A PyPI timeout or a runner
    problem is not the recipe's fault, and filing against an owner for it is
    how a notification channel loses its credibility.
    """
    failures: dict[str, list[dict]] = {}
    for entry in results:
        if entry.get("outcome") == "fail":
            failures.setdefault(entry["recipe"], []).append(entry)
    return failures


def recovered_recipes(results: list[dict]) -> set[str]:
    """Recipes with at least one `pass` job and no `fail` job.

    Recovery must be a POSITIVE observation, never the absence of a failure.
    `failing_recipes` ignores `infra` outcomes on purpose, so a month where
    every job for a recipe came back `infra` — a registry outage, a runner
    problem — leaves that recipe in neither bucket. Reading "not failing" as
    "recovered" would close the tracking issue claiming the recipe installs
    and passes, having tested nothing, and would reset an escalation clock up
    to 119 days old.

    A recipe with no conclusive result is left exactly as it was: the issue
    stays open, no rung advances, and the next run picks up where this one
    left off.
    """
    outcomes: dict[str, set[str]] = {}
    for entry in results:
        outcomes.setdefault(entry["recipe"], set()).add(entry.get("outcome"))
    return {
        recipe
        for recipe, seen in outcomes.items()
        if "pass" in seen and "fail" not in seen
    }


def _mention(owner: str | None) -> tuple[str, str]:
    """(mention line, note) for the issue body."""
    if owner:
        return (
            f"@{owner}",
            "You are listed as `ownership.poc` for this recipe.",
        )
    return (
        f"@{MAINTAINER}",
        "This recipe's `manifest.yaml` declares no usable `ownership.poc`, "
        "so a maintainer is being notified instead. Setting a valid `poc` "
        "would route future notices to the right person.",
    )


def build_body(recipe: str, jobs: list[dict], run_url: str) -> str:
    """`jobs` is EVERY job for the recipe, passing ones included.

    The passing versions are what make the report actionable: "broken on 3.13,
    fine on 3.11" tells the owner it is an interpreter problem, where "broken"
    alone sends them looking at their own code. An earlier version of this
    function was handed only the failures and silently lost that.
    """
    owner = read_owner(recipe)
    mention, note = _mention(owner)
    failed = [j for j in jobs if j.get("outcome") == "fail"]
    versions = ", ".join(sorted({j["python"] for j in failed}))
    passing = sorted({j["python"] for j in jobs if j.get("outcome") == "pass"})

    lines = [
        f"{mention} — the monthly recipe canary could not get "
        f"`{recipe}` working.",
        "",
        note,
        "",
        "## What failed",
        "",
        f"Python {versions}.",
    ]
    if passing:
        lines += [
            "",
            f"It still passes on Python {', '.join(passing)} — so this is a "
            f"version-specific break, not a wholesale one. The recipe's "
            f"`requires-python` claims support for the failing version.",
        ]
    lines += ["", "| Python | step | detail |", "|---|---|---|"]
    for job in sorted(failed, key=lambda j: j["python"]):
        detail = (job.get("detail") or "").replace("|", "\\|")[:300]
        lines.append(f"| {job['python']} | {job.get('step', '?')} | {detail} |")

    lines += [
        "",
        f"[Full logs]({run_url})",
        "",
        "## What this means",
        "",
        "The canary installs each recipe from its **committed lockfile** "
        "(`uv sync --frozen`) and runs its tests. It does not update "
        "dependencies and never opens version-bump PRs — this repo leaves "
        "dependency freshness to recipe owners. So this is not a stale-"
        "dependency nag: as far as the canary can tell, the recipe does not "
        "work for someone who clones it today.",
        "",
        "## What happens if nobody acts",
        "",
        "The canary runs monthly. Each run that still fails advances one "
        "stage — so this is roughly a month per step, and the schedule only "
        "ever slips later, never sooner.",
        "",
        "| next failing run | then |",
        "|---|---|",
        "| 1st | a reminder comment here |",
        "| 2nd | the recipe should be marked `status: inactive` |",
        "| 3rd | notice that deletion is scheduled |",
        f"| 4th | removal of the recipe proposed, for @{MAINTAINER} to decide |",
        "",
        "Fix the recipe and this issue closes itself on the next run.",
        "",
        "---",
        "<sub>Opened by `.github/workflows/recipe-canary.yml`. Reply here if "
        "the canary is wrong — that is a bug worth fixing.</sub>",
    ]
    return "\n".join(lines)


def ensure_labels() -> None:
    """Create the canary's labels if the repo does not have them yet."""
    for name, colour, description in [
        (LABEL_TRACKING, "0e8a16", "Opened by the monthly recipe canary"),
        (LABEL_REMINDED, "c5def5", "Canary reminder sent (rung 1 of 4)"),
        (LABEL_ROTTING, "fbca04", "Should be status: inactive (rung 2 of 4)"),
        (LABEL_DELETION, "d93f0b", "Deletion scheduled (rung 3 of 4)"),
        (LABEL_PROPOSED, "b60205", "Deletion proposed (rung 4 of 4)"),
    ]:
        subprocess.run(
            [
                "gh",
                "label",
                "create",
                name,
                "--repo",
                REPO,
                "--color",
                colour,
                "--description",
                description,
                "--force",
            ],
            capture_output=True,
            text=True,
            check=False,
        )


def open_issue(recipe: str, jobs: list[dict], run_url: str, dry: bool) -> None:
    owner = read_owner(recipe)
    body = build_body(recipe, jobs, run_url)
    args = [
        "issue",
        "create",
        "--repo",
        REPO,
        "--title",
        issue_title(recipe),
        "--body",
        body,
        "--label",
        LABEL_TRACKING,
    ]
    # The dry-run guard comes BEFORE is_assignable, which shells out to
    # `gh api`. A dry run is supposed to touch nothing and work offline;
    # making an API call in it meant a run that changes nothing could still
    # fail, or burn rate limit, for no reason.
    if dry:
        print(f"  [dry-run] would open: {issue_title(recipe)}")
        return
    if owner and is_assignable(owner):
        args += ["--assignee", owner]
    print(f"  opened: {gh(*args)}")


def comment(number: int, body: str, dry: bool) -> None:
    if dry:
        print(f"  [dry-run] would comment on #{number}")
        return
    gh("issue", "comment", str(number), "--repo", REPO, "--body", body)


def add_label(number: int, label: str, dry: bool) -> None:
    if dry:
        print(f"  [dry-run] would label #{number} {label}")
        return
    gh("issue", "edit", str(number), "--repo", REPO, "--add-label", label)


def _stage_body(label: str, recipe: str, age: int, mention: str) -> str:
    """The comment posted when `label` is reached."""
    if label == LABEL_REMINDED:
        return (
            f"{mention} — still failing, {age} days on. Nothing has changed "
            f"on the canary's side; the recipe still does not install and "
            f"pass from its committed lockfile.\n\nEach monthly run that "
            f"still fails advances one stage. The next one marks this "
            f"recipe `status: inactive`."
        )
    if label == LABEL_ROTTING:
        return (
            f"{mention} — {age} days and still failing, so this recipe is "
            f"being marked **`status: inactive`**.\n\nThat is not deletion. "
            f"It records that nobody is currently accountable for the recipe "
            f"working, and it is reversible.\n\n"
            f"{_write_note(recipe, 'status: inactive')}"
        )
    if label == LABEL_DELETION:
        # `mention` is ALREADY @MAINTAINER when the recipe declares no usable
        # poc, so naming them again renders "@someone @someone".
        audience = (
            mention
            if mention == f"@{MAINTAINER}"
            else f"@{MAINTAINER} {mention}"
        )
        return (
            f"{audience} — {age} days. **Deletion is "
            f"scheduled.** One more failing run and the canary proposes "
            f"removing `{recipe}` from the repository.\n\nA maintainer "
            f"decides whether that happens; the canary only proposes it. To "
            f"stop the clock, fix the recipe or say here that it should be "
            f"kept."
        )
    return (
        f"@{MAINTAINER} — {age} days, and the last stage of the ladder. "
        f"Removal of `{recipe}` is now due.\n\n"
        f"{_write_note(recipe, 'deletion')}\n\nNothing is removed without a "
        f"human deciding to."
    )


def escalate(
    recipe: str, issue: dict, dry: bool, now=None, advance: bool = True
) -> None:
    """Advance the ladder by AT MOST ONE rung.

    The labels on the issue are the state: the next rung is the first entry
    in `LADDER` the issue does not already carry. Firing exactly one per run
    is what makes the ladder impossible to collapse — see the comment on
    `LADDER` for why gating on an age in days did not survive contact with a
    monthly cron.

    `advance=False` reports without moving anything, which is what a manual
    `workflow_dispatch` run gets: re-running the canary by hand to check
    something should never march a recipe closer to deletion.
    """
    number = issue["number"]
    age = age_days(issue["createdAt"], now)
    labels = issue["labelNames"]

    next_rung = next((lbl for lbl in LADDER if lbl not in labels), None)
    if next_rung is None:
        # Every rung has fired. Stay quiet rather than nag monthly forever;
        # the issue is still open and still says everything it needs to.
        return

    if not advance:
        print(f"  {recipe}: not a scheduled run, ladder held at {next_rung}")
        return

    # Floor, not driver: rung N cannot fire before N * MIN_DAYS_BETWEEN_STAGES
    # days. Under the monthly cron this never binds — it is there so that
    # making the cron more frequent cannot quietly compress a four-month
    # ladder into a four-week one.
    rung = LADDER.index(next_rung)
    floor = (rung + 1) * MIN_DAYS_BETWEEN_STAGES
    if age < floor:
        print(
            f"  {recipe}: {next_rung} held, {age}d < {floor}d floor for "
            f"rung {rung + 1}"
        )
        return

    owner = read_owner(recipe)
    mention = f"@{owner}" if owner else f"@{MAINTAINER}"
    comment(number, _stage_body(next_rung, recipe, age, mention), dry)
    add_label(number, next_rung, dry)


def _write_note(recipe: str, what: str) -> str:
    """What the canary can actually do about a file change: nothing, yet.

    This script does not write to the repository. It has no YAML library, it
    opens no pull request, and the `report` job that runs it is declared
    `contents: read`, so it could not push if it tried. Every file change the
    ladder calls for is a human's to make.

    An earlier version switched on a `CANARY_APP_TOKEN` env var and promised
    "a pull request will be opened automatically" when it was set. No code
    ever opened one, so setting that secret would have made the canary lie on
    a public issue. The branch is gone; when the write side is built it can
    come back with an implementation behind it.
    """
    return (
        f"**A maintainer needs to apply `{what}` to `{recipe}` by hand.** The "
        f"canary has no write identity: `GITHUB_TOKEN` cannot push to a "
        f"branch that requires a code-owner review, so it will not pretend "
        f"to have done it."
    )


def close_recovered(recipe: str, issue: dict, dry: bool) -> None:
    body = (
        f"`{recipe}` installs from its lockfile and passes its tests again, "
        f"so the canary is closing this.\n\n"
    )
    if LABEL_ROTTING in issue["labelNames"]:
        # The label records that the canary ASKED for the manifest change,
        # not that anyone made it — the canary cannot write files and does
        # not read `status` back. Saying "this recipe was marked inactive"
        # as fact would be wrong most of the time.
        body += (
            f"While this was failing the canary asked for `{recipe}` to be "
            f"marked `status: inactive`. It has no way to tell whether that "
            f"happened, so: if the manifest does say `inactive`, set it back "
            f"to `active`.\n\n{_write_note(recipe, 'status: active')}"
        )
    else:
        body += "No further action needed."
    if dry:
        print(f"  [dry-run] would close #{issue['number']}")
        return
    comment(issue["number"], body, dry)
    gh("issue", "close", str(issue["number"]), "--repo", REPO)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--results", required=True, type=Path)
    parser.add_argument("--run-url", default="")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--max-issues",
        type=int,
        default=DEFAULT_MAX_ISSUES,
        help=(
            "Refuse to act if more than this many recipes are failing in one "
            "run. That many at once is a systemic problem rather than that "
            "many independent rotting recipes, and filing against every "
            "owner for it is how the channel gets muted."
        ),
    )
    parser.add_argument(
        "--no-escalate",
        action="store_true",
        help=(
            "Open and close issues as usual, but do not advance the "
            "escalation ladder. The workflow passes this on every run that "
            "is not `schedule`-triggered: re-running the canary by hand, or "
            "against a single recipe, must never move something closer to "
            "deletion."
        ),
    )
    args = parser.parse_args(argv)

    results = json.loads(args.results.read_text(encoding="utf-8"))
    if not results:
        print(
            "No canary results. Refusing to act: an empty result set and a "
            "run where every job vanished are indistinguishable, and acting "
            "would close every open issue as recovered.",
            file=sys.stderr,
        )
        return 1

    failures = failing_recipes(results)
    recovered = recovered_recipes(results)
    by_recipe: dict[str, list[dict]] = {}
    for entry in results:
        by_recipe.setdefault(entry["recipe"], []).append(entry)
    all_recipes = set(by_recipe)
    inconclusive = all_recipes - set(failures) - recovered
    print(
        f"{len(all_recipes)} recipes checked, {len(failures)} failing, "
        f"{len(recovered)} passing, {len(inconclusive)} inconclusive."
    )

    # Circuit breaker, mirroring close_orphan_dependabot_prs.py's
    # DEFAULT_MAX_CLOSE. Routine rot is a handful of recipes; half the repo
    # failing at once means something systemic — a bad runner image, an
    # ecosystem-wide yank — and filing an issue against every owner for it is
    # exactly how a notification channel gets muted. Escalation is what stops;
    # the failures are still printed and the run still fails.
    if len(failures) > args.max_issues:
        print(
            f"Refusing to act on {len(failures)} failing recipes in one run "
            f"(limit {args.max_issues}). That many at once is a systemic "
            f"problem, not {len(failures)} independent rotting recipes. "
            f"Investigate, then re-run with --max-issues if it really is "
            f"this bad.\nFailing: {', '.join(sorted(failures))}",
            file=sys.stderr,
        )
        return 1

    if not args.dry_run:
        ensure_labels()

    # One snapshot for the whole run, not one lookup per recipe.
    issues = open_issues_by_title()

    errors: list[tuple[str, Exception]] = []
    for recipe in sorted(all_recipes):
        # One recipe's API failure must not skip every recipe after it. The
        # loop used to abort on the first GhError, leaving an arbitrary tail
        # of the list unexamined with nothing saying which.
        try:
            _process(recipe, by_recipe, failures, recovered, args, issues)
        except GhError as exc:
            print(f"{recipe}: ERROR {exc}", file=sys.stderr)
            errors.append((recipe, exc))

    if errors:
        names = ", ".join(r for r, _ in errors)
        print(
            f"\n{len(errors)} recipe(s) could not be processed: {names}. "
            f"The rest were handled; re-run once the cause is fixed.",
            file=sys.stderr,
        )
        return 1

    return 0


def _process(
    recipe: str,
    by_recipe: dict[str, list[dict]],
    failures: dict[str, list[dict]],
    recovered: set[str],
    args: argparse.Namespace,
    issues: dict[str, dict] | None = None,
) -> None:
    """Decide and apply this recipe's issue action."""
    issue = find_issue(recipe, issues)
    if recipe in failures:
        if issue is None:
            print(f"{recipe}: FAILING, opening issue")
            open_issue(recipe, by_recipe[recipe], args.run_url, args.dry_run)
        else:
            print(
                f"{recipe}: FAILING, issue #{issue['number']} already "
                f"open ({age_days(issue['createdAt'])}d)"
            )
            escalate(
                recipe,
                issue,
                args.dry_run,
                advance=not args.no_escalate,
            )
    elif issue is None:
        return
    elif recipe in recovered:
        print(f"{recipe}: recovered, closing #{issue['number']}")
        close_recovered(recipe, issue, args.dry_run)
    else:
        print(
            f"{recipe}: inconclusive (no passing job this run), leaving "
            f"#{issue['number']} open"
        )


if __name__ == "__main__":
    raise SystemExit(main())
