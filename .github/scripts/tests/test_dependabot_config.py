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
"""Consistency tests for .github/dependabot.yml.

The repo opens no Dependabot PRs against recipes — neither version updates
nor security updates. Recipe owners own dependency freshness, the monthly
canary in recipe-canary.yml detects rot, and advisories stay visible as
alerts in the Security tab. The reasoning is in the header of dependabot.yml.

This has now inverted twice, so it is worth being precise about what the
tests guard today.

Originally the invariant was "every ecosystem in the tree has a config
entry", so a new Go or Java recipe failed loudly rather than silently going
unmanaged. When version updates were switched off (#2501) that flipped: the
*presence* of a recipe entry became the thing to catch, because it would
restart the flood the policy exists to stop.

Absence turned out to be too blunt. Removing the entries left the repo with
no way to say anything about an ecosystem at all — and security updates,
which ignore the question of whether an entry exists, kept opening PRs. One
advisory wave opened 13 in 34 minutes. Suppressing those needs `ignore`, and
`ignore` needs an entry to live in.

So the invariant is no longer about presence or absence. A recipe ecosystem
MUST appear, and MUST appear in the suppression shape: no version updates
(`open-pull-requests-limit: 0`) and no security updates (`ignore` of "*").
Both halves are load-bearing and each is checked, because dropping either
one quietly re-enables a flood the other does not cover.

What these tests cannot do is verify Dependabot's own behaviour. Whether
`ignore` really suppresses a grouped security update is only observable
after a merge to the default branch, on the next advisory.
"""

import os
from pathlib import Path

import close_orphan_dependabot_prs as orphans
import pytest
import recipe_manifests as rm
import yaml

CONFIG_PATH = Path(__file__).resolve().parent.parent.parent / "dependabot.yml"


@pytest.fixture(scope="module")
def config() -> dict:
    return yaml.safe_load(CONFIG_PATH.read_text(encoding="utf-8"))


# Ecosystems recipe_manifests.py can detect. Each must appear in
# dependabot.yml as a suppression entry. Derived from the detector list
# rather than retyped, so a newly supported ecosystem is covered here the
# moment it is added there.
#
# This is a FLOOR, not the whole set. DETECTORS is scoped to
# SCAN_ROOTS (core/, contrib/, skills/) because it answers "which recipe
# directories do the canary and orphan cleanup track". Dependabot's `**/*`
# glob is scoped to the repository, so it also sees the legacy python/,
# java/ and kotlin/ trees, and it parses manifest types no detector looks
# for — requirements.txt and Dockerfiles both exist here. dependabot.yml
# therefore suppresses more ecosystems than this set contains, and doing so
# must stay legal.
RECIPE_ECOSYSTEMS = {eco for eco, _ in rm.DETECTORS}


def is_suppression_entry(entry: dict) -> bool:
    """True iff `entry` is configured purely to switch updates off.

    Identified by shape rather than by an ecosystem allowlist: anything that
    opens no PRs of either kind is a suppression entry. An allowlist would
    have to be kept in step with dependabot.yml by hand, and the failure
    mode of forgetting is that a live entry gets silently exempted from the
    budget test below.
    """
    return suppresses_version_updates(entry) and suppresses_security_updates(
        entry
    )


def suppression_entries(config: dict) -> list[dict]:
    """Every suppression entry, in file order."""
    return [u for u in config["updates"] if is_suppression_entry(u)]


def recipe_entries(config: dict) -> list[dict]:
    """The config entries for detectable recipe ecosystems, in file order."""
    return [
        u
        for u in config["updates"]
        if u["package-ecosystem"] in RECIPE_ECOSYSTEMS
    ]


def suppresses_version_updates(entry: dict) -> bool:
    """True iff `entry` can open no version-update PR.

    `open-pull-requests-limit: 0` is what GitHub documents for "security
    updates only". Note that 0 is falsy, so this compares explicitly.
    """
    return entry.get("open-pull-requests-limit") == 0


def suppresses_security_updates(entry: dict) -> bool:
    """True iff `entry` can open no security-update PR.

    `ignore` is the only option in the file that reaches security updates at
    all — `cooldown`, `schedule` and `open-pull-requests-limit` are all
    documented as version-updates-only. A wildcard `dependency-name` is what
    makes it cover everything rather than one package.
    """
    return any(
        rule.get("dependency-name") == "*" for rule in entry.get("ignore", [])
    )


def test_config_is_valid_yaml_with_version_2(config):
    assert config["version"] == 2
    assert config["updates"]


def test_every_recipe_ecosystem_is_configured(config):
    """A detectable ecosystem with no entry here is an unsuppressed one.

    Suppression needs somewhere to live: an ecosystem absent from this file
    has no `ignore` rule, so Dependabot security updates open PRs against
    its recipes freely. That is exactly the state that produced 13 PRs in 34
    minutes on 2026-08-13.

    So the two lists are checked against each other. Adding a detector to
    recipe_manifests without adding an entry here fails, rather than quietly
    leaving the first recipe of that language exposed.
    """
    configured = {u["package-ecosystem"] for u in config["updates"]}
    missing = RECIPE_ECOSYSTEMS - configured
    assert not missing, (
        f"{sorted(missing)} are detectable recipe ecosystems with no entry "
        "in dependabot.yml. Without an entry there is no `ignore` rule, so "
        "security updates will open PRs against those recipes. Add a "
        "suppression entry for each — see the header of dependabot.yml."
    )


def test_recipe_ecosystems_are_configured_only_to_suppress(config):
    """The core invariant of the current policy.

    Recipes own their own dependency freshness; the repo opens no PRs
    against them. Version updates flooded first — 98 open, none mergeable,
    the review load landing on the CODEOWNERS catch-all rather than on the
    recipe's declared owner. Security updates then did the same thing from a
    different direction, because they ignore whether an entry exists.

    Both halves must therefore hold on every recipe entry, and they are
    asserted separately so a failure names which one was dropped.

    This is not a ban. It is a tripwire: re-enabling either kind of update
    should be a deliberate change with a reviewer attached, so update this
    test in the same PR and say why.
    """
    entries = recipe_entries(config)
    assert entries, "no recipe ecosystem entries found — see the test above"

    for entry in entries:
        eco = entry["package-ecosystem"]

        assert suppresses_version_updates(entry), (
            f"{eco} does not set `open-pull-requests-limit: 0`, so it can "
            "open version-update PRs. Recipe dependency freshness is the "
            "recipe owner's responsibility and rot is caught by the monthly "
            "recipe canary — see the header of dependabot.yml. If "
            "re-enabling this is intentional, update this test in the same "
            "change."
        )

        assert suppresses_security_updates(entry), (
            f'{eco} has no `ignore` rule for `dependency-name: "*"`, so '
            "Dependabot security updates can open PRs against every recipe "
            "in that ecosystem. `ignore` is the ONLY option here that "
            "reaches security updates — cooldown, schedule and "
            "open-pull-requests-limit are all version-updates-only. If "
            "re-enabling this is intentional, update this test in the same "
            "change."
        )


# Every suppression entry must list exactly these, in this order.
#
# "/" and "**/*" are not redundant. A `**/*` glob requires at least one path
# segment, so it does not match the repository root, and the root here holds
# /uv.lock and /pyproject.toml — this repo's own tooling dependencies. The
# glob alone would leave those two files as the one unsuppressed manifest
# location in the tree.
SUPPRESSION_DIRECTORIES = ["/", "**/*"]


def test_suppression_entries_cover_every_manifest_location(config):
    """The directories list is required for the security half to work.

    GitHub: "In order for Dependabot to use this configuration for security
    updates, the `directory` must be the path to the manifest files (or
    `directories` must contain paths or glob patterns matching the manifest
    file locations), and you should not specify a `target-branch`."

    So suppression applies exactly where the entry's directories point. A
    gap in that list is not a partial failure, it is a silent one: the
    ecosystem looks suppressed, and stays suppressed, right up until an
    advisory lands on a manifest the list does not cover.

    Both members are asserted because dropping either opens a real hole —
    "/" leaves the root lockfiles live, "**/*" leaves every recipe live.
    `target-branch` is checked alongside them because setting it
    disqualifies the whole entry from applying to security updates.

    Covers every suppression entry, not just the detectable-recipe ones, so
    the pip and docker entries are held to the same shape.
    """
    for entry in suppression_entries(config):
        eco = entry["package-ecosystem"]

        assert entry.get("directories") == SUPPRESSION_DIRECTORIES, (
            f"{eco} must use `directories: {SUPPRESSION_DIRECTORIES}`, not "
            f"{entry.get('directories') or entry.get('directory')!r}. "
            "Suppression only applies where an entry's directories match "
            "the manifest locations: without '**/*' every recipe stays "
            "live, and without '/' the root uv.lock and pyproject.toml do."
        )

        assert "target-branch" not in entry, (
            f"{eco} sets `target-branch`, which disqualifies the entry from "
            "applying to security updates. The `ignore` rule would stop "
            "being honoured and security PRs would resume."
        )


def config_comment_text() -> str:
    """The lowercased comment lines of dependabot.yml, joined.

    Comments only. Searching the whole file cannot tell prose from config:
    `ignore` is a YAML key here, so "is `ignore` explained?" asked of the raw
    text is answered by the suppression entries themselves, and is true
    no matter what the comments actually say.
    """
    return "\n".join(
        line.strip().lstrip("#").strip()
        for line in CONFIG_PATH.read_text(encoding="utf-8").splitlines()
        if line.strip().startswith("#")
    ).lower()


# Claims that were true before security updates were suppressed and are
# false now. The header carried the first two of these verbatim, so this is
# the exact text a revert or a bad merge would reintroduce.
CONTRADICTED_CLAIMS = (
    "no interaction between",
    "not affected by this file",
    "unaffected by this file",
)


def test_config_explains_that_ignore_is_what_stops_security_updates():
    """Guard against the most likely misreading of this file.

    The reasoning is genuinely counter-intuitive: security updates are a
    repository setting, so the natural assumption is that a config file
    cannot touch them, and that the recipe entries here are therefore
    pointless clutter to be tidied away. `ignore` is the one exception, and
    it is the whole mechanism.

    Nothing in the parsed config can assert that the explanation survives, so
    this pins the prose instead. Both directions are checked, because the
    positive one alone is weak: the correct explanation must be present AND
    the explanation it replaced must be absent. Without the negative check
    this test passes on a header stating the exact opposite of the truth —
    which is the likeliest way for this file to rot, since that claim was
    correct for months and is the natural thing to restore from memory.
    """
    comments = config_comment_text()

    assert "ignore" in comments, (
        "dependabot.yml no longer mentions `ignore` in its comments. It is "
        "the only option here that reaches security updates, so without the "
        "explanation the recipe entries look like dead config and the next "
        "reader deletes them, silently restoring the PR flood."
    )

    assert "security" in comments, (
        "dependabot.yml no longer explains what the suppression entries do "
        "about security updates."
    )

    for claim in CONTRADICTED_CLAIMS:
        assert claim not in comments, (
            f"dependabot.yml says security updates are {claim!r} this file. "
            "That was true until the entries below gained `ignore` rules, "
            "and it is now the opposite of how this repo works. Someone "
            "acting on it would delete the suppression and reopen the flood."
        )


def test_every_entry_is_a_known_static_entry(config):
    """Every entry that can open a PR must be a known static one.

    A static entry is configured, never discovered, so
    close_orphan_dependabot_prs.py can only know about it by being told. One
    present here but missing from STATIC_ENTRIES has its PRs classified as
    orphans and closed with --delete-branch — and since such an entry
    produces roughly one grouped PR a month, the --max-close circuit breaker
    would never trip on it.

    Suppression entries are exempt on both counts. They use a glob rather
    than a fixed directory (see the test above for why that is mandatory),
    and they open no PRs, so there is nothing for the orphan cleanup to
    misclassify.
    """
    for entry in config["updates"]:
        if is_suppression_entry(entry):
            continue
        assert "directories" not in entry, (
            f"{entry['package-ecosystem']} uses a `directories` glob. Globs "
            "cover the whole tree, which only the suppression entries need; "
            "an entry that actually opens PRs should target a fixed "
            "directory so the orphan cleanup can account for it."
        )

    configured_static = {
        (u["package-ecosystem"], u["directory"])
        for u in config["updates"]
        if "directory" in u
    }
    known_static = set(rm.static_pairs())

    unknown = configured_static - known_static
    assert not unknown, (
        f"{sorted(unknown)} are configured in dependabot.yml but missing from "
        "recipe_manifests.STATIC_ENTRIES, so the orphan cleanup would close "
        "their PRs with --delete-branch. Add them there."
    )

    stale = known_static - configured_static
    assert not stale, (
        f"{sorted(stale)} are in recipe_manifests.STATIC_ENTRIES but not in "
        "dependabot.yml. Harmless, but the list is now describing an entry "
        "that does not exist — remove it."
    )


def test_orphan_cleanup_still_sees_the_recipe_tree(config):
    """Removing entries here must not make live recipes look abandoned.

    close_orphan_dependabot_prs.live_pairs() derives liveness from
    recipe_manifests.scan() — the tree — not from this file, which is what
    keeps the 98 PRs that were open when the policy changed from being swept
    up and closed with --delete-branch on the next housekeeping run. That
    indirection is easy to "simplify" away later, so pin it.

    Asserting that the live set reaches BEYOND what this file configures is
    what makes the pin real: a live_pairs() rewritten to read dependabot.yml
    could only ever return a subset of it, and this goes red.

    The comparison is on (ecosystem, directory) pairs rather than ecosystems
    alone. Once every recipe ecosystem gained a suppression entry, the
    ecosystem sets matched by construction and the old assertion could no
    longer fail. Directories still tell the two apart: this file names only
    "/" and the "**/*" glob, while the tree yields concrete recipe paths.
    """
    configured_dirs: set[str] = set()
    for entry in config["updates"]:
        if "directory" in entry:
            configured_dirs.add(entry["directory"])
        configured_dirs.update(entry.get("directories", []))

    tracked_dirs = {directory for _, directory in orphans.live_pairs()}

    assert tracked_dirs - configured_dirs, (
        "close_orphan_dependabot_prs.live_pairs() no longer tracks any "
        "directory beyond the ones dependabot.yml names literally, which is "
        "what it looks like when liveness starts being read from this file "
        "instead of from the tree. The cleanup treats anything it cannot see "
        "as dead and closes it with --delete-branch."
    )


def test_github_actions_entry_uses_a_plain_directory(config):
    """GitHub documents `directory: "/"` for github-actions; it is not a glob,
    and Dependabot looks in /.github/workflows regardless."""
    entry = next(
        u
        for u in config["updates"]
        if u["package-ecosystem"] == "github-actions"
    )
    assert entry["directory"] == "/"
    assert "directories" not in entry


def test_github_actions_entry_declares_no_semver_cooldown(config):
    """github-actions releases (v1, v2, ...) are not semver. Dependabot
    rejects the WHOLE config if semver-major-days appears here, which would
    silently disable every ecosystem."""
    entry = next(
        u
        for u in config["updates"]
        if u["package-ecosystem"] == "github-actions"
    )
    assert "semver-major-days" not in entry.get("cooldown", {})


# Intervals that blow the budget on arithmetic alone: at one grouped PR per
# run they yield roughly 20 and 4 PRs a month against a ceiling of 1-2.
#
# A denylist rather than an allowlist, because GitHub's interval enum is
# fixed (daily, weekly, monthly, quarterly, semiannually, yearly, cron) and
# these are the only two members that overrun. An allowlist would also have
# rejected `cron`, which is the documented way to express a custom monthly
# schedule and is perfectly within budget — the frequency just lives in a
# cron expression this test cannot evaluate.
OVERRUNNING_INTERVALS = {"daily": 20, "weekly": 4}


def test_the_repo_opens_at_most_one_dependabot_pr_a_month(config):
    """Pin the budget this whole policy exists to enforce.

    Everything else here guards the entries that open NOTHING. That leaves
    the one path that can still open a PR completely unguarded, which is the
    wrong way round — suppression could stay perfect while `github-actions`
    quietly went daily with a limit of 20, and the symptom the policy exists
    to prevent would be back with every test green.

    So this asserts the ceiling directly on every entry that is not pure
    suppression: at most one PR each, no more often than monthly. It is
    deliberately about the OUTCOME rather than about github-actions
    specifically, so a future non-recipe ecosystem has to be budgeted rather
    than merely added.

    Membership is by shape, not by ecosystem name. An entry is exempt only
    if it demonstrably opens nothing; an ecosystem allowlist would exempt an
    entry that merely looks familiar.
    """
    for entry in config["updates"]:
        if is_suppression_entry(entry):
            continue

        eco = entry["package-ecosystem"]
        limit = entry.get("open-pull-requests-limit")
        interval = entry.get("schedule", {}).get("interval")

        # `<= 1`, not `== 1`: 0 means the entry opens no version updates at
        # all, which is strictly safer than the ceiling and must not be
        # rejected for being too strict.
        assert isinstance(limit, int) and limit <= 1, (
            f"{eco} sets `open-pull-requests-limit: {limit!r}`. Entries that "
            "can open PRs are capped at 1 so the repo's total stays within "
            "the 1-2 PRs a month this policy exists to guarantee. Note the "
            "Dependabot default is 5 when the key is omitted."
        )

        assert interval not in OVERRUNNING_INTERVALS, (
            f"{eco} runs {interval}, which blows the PR budget: at one "
            "grouped PR per run that is roughly "
            f"{OVERRUNNING_INTERVALS[interval]} a month against a ceiling "
            "of 1-2. Use monthly or slower."
        )


def test_day_is_only_set_on_weekly_schedules(config):
    """`day` is a weekly-only key.

    GitHub documents it as "run WEEKLY updates for a package manager on a
    specific day of the week"; `monthly` always runs on the first of the
    month and offers no day selection. The github-actions entry carried
    `day: monday` while it was weekly, and the key is easy to leave behind
    when changing the interval.

    Same failure class as the semver cooldown above: a key Dependabot does
    not accept in context risks rejecting the whole file, which disables
    every entry including the suppression ones — so the visible symptom
    would be recipe PRs resuming, nowhere near the line that caused it.
    """
    for entry in config["updates"]:
        schedule = entry.get("schedule", {})
        if "day" not in schedule:
            continue
        assert schedule["interval"] == "weekly", (
            f"{entry['package-ecosystem']} sets `day: {schedule['day']}` "
            f"with `interval: {schedule['interval']}`. `day` only applies to "
            "weekly schedules — drop it, or make the interval weekly."
        )


def test_group_by_is_not_set_anywhere(config):
    """`group-by` at entry level is not valid config and would have Dependabot
    reject the whole file. The real home for the key is
    `groups.<name>.group-by`; both levels are checked so the test's scope
    matches its name.
    """
    for entry in config["updates"]:
        assert "group-by" not in entry, (
            f"{entry['package-ecosystem']}: `group-by` at entry level is not "
            "valid config and would have Dependabot reject the whole file"
        )
        for name, group in entry.get("groups", {}).items():
            assert "group-by" not in group, (
                f"{entry['package-ecosystem']}.groups.{name}: `group-by` "
                "changes how updates are batched into PRs. Nothing needs it "
                "while github-actions is the only entry that opens one — add "
                "it deliberately or not at all."
            )


# Filename -> `package-ecosystem`, for every manifest type Dependabot can
# open a pull request for. Suffix-matched types (.tf, .csproj) are handled
# separately below.
#
# This deliberately duplicates nothing in recipe_manifests.DETECTORS. That
# list is scoped to SCAN_ROOTS and to the ecosystems the canary and orphan
# cleanup care about; this one is scoped to "anything Dependabot might open
# a PR for, anywhere in the repository", which is a different and larger
# question. Keeping them separate is what lets the test below catch a
# manifest type no detector knows about.
MANIFEST_ECOSYSTEMS = {
    "uv.lock": "uv",
    "go.mod": "gomod",
    "pom.xml": "maven",
    "build.gradle": "gradle",
    "build.gradle.kts": "gradle",
    "package.json": "npm",
    "pnpm-lock.yaml": "npm",
    "bun.lockb": "bun",
    "requirements.txt": "pip",
    "Pipfile": "pip",
    "poetry.lock": "pip",
    "setup.py": "pip",
    "Dockerfile": "docker",
    "docker-compose.yml": "docker-compose",
    "docker-compose.yaml": "docker-compose",
    "Gemfile": "bundler",
    "Cargo.toml": "cargo",
    "composer.json": "composer",
    ".gitmodules": "gitsubmodule",
    ".pre-commit-config.yaml": "pre-commit",
    "mix.exs": "mix",
    "elm.json": "elm",
    "pubspec.yaml": "pub",
    "devcontainer.json": "devcontainers",
    "Package.swift": "swift",
    "flake.nix": "nix",
    "Chart.yaml": "helm",
}

SUFFIX_ECOSYSTEMS = {
    ".tf": "terraform",
    ".tf.json": "terraform",
    ".csproj": "nuget",
    ".vbproj": "nuget",
    ".fsproj": "nuget",
}

REPO_ROOT = CONFIG_PATH.parent.parent


def ecosystem_for(filename: str) -> str | None:
    """The Dependabot ecosystem a manifest filename belongs to, if any."""
    if filename in MANIFEST_ECOSYSTEMS:
        return MANIFEST_ECOSYSTEMS[filename]
    for suffix, eco in SUFFIX_ECOSYSTEMS.items():
        if filename.endswith(suffix):
            return eco
    if filename.startswith("requirements") and filename.endswith(".txt"):
        return "pip"
    return None


def ecosystems_present_in_tree() -> dict[str, str]:
    """Every ecosystem with a manifest in the repo -> one example path."""
    found: dict[str, str] = {}
    for dirpath, dirnames, filenames in os.walk(REPO_ROOT):
        dirnames[:] = [d for d in dirnames if d not in rm.SKIP_DIRS]
        for filename in filenames:
            eco = ecosystem_for(filename)
            if eco and eco not in found:
                rel = os.path.relpath(
                    os.path.join(dirpath, filename), REPO_ROOT
                )
                found[eco] = rel
    return found


def test_every_ecosystem_in_the_tree_is_configured(config):
    """The backstop for the mistake this policy keeps making.

    Coverage here has been wrong three times, each time the same way: the
    suppression list was written from a remembered set of ecosystems rather
    than a measured one. pip and docker were missed because no detector
    looks for requirements.txt or a Dockerfile. terraform was missed because
    nobody thinks of .tf as a dependency manifest. Each gap was invisible
    until someone went looking.

    So stop relying on going looking. This walks the actual tree, maps every
    manifest filename to its ecosystem, and fails if any of them has no
    entry in dependabot.yml. Adding a recipe in a new language now fails
    here instead of quietly opening PRs six months later.

    An entry is required even for an ecosystem that cannot currently raise
    an alert — terraform and pre-commit are absent from GitHub's
    dependency-graph supported-ecosystems table today, so neither can open
    a security PR. That list is GitHub's to change without telling anyone,
    and the cost of a redundant entry is eight lines.
    """
    configured = {u["package-ecosystem"] for u in config["updates"]}
    present = ecosystems_present_in_tree()

    missing = {
        eco: path for eco, path in present.items() if eco not in configured
    }
    assert not missing, (
        "dependabot.yml has no entry for "
        f"{sorted(missing)}, but manifests for them exist in the tree "
        f"(e.g. {sorted(missing.values())}). Without an entry there is no "
        "`ignore` rule, so nothing stops Dependabot opening PRs for them. "
        "Add a suppression entry for each, copying an existing one."
    )
