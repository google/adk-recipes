# Admin runbooks

Multi-file procedures. Each lists the files to touch and the order to touch them —
never the values inside them. Read the file to get the value.

These are procedures for a **human** to carry out. State them; do not execute them.

---

## Add a new language

The single most-missed thing in this repo, because the language list is duplicated
across config, tooling and docs. Touch all of these or the language is half-supported
in ways CI will not tell you about.

1. **`.github/schemas/manifest-schema.json`** — add the value to the `language` enum.
   Until this lands, every manifest declaring the language fails schema validation, so
   do it first.
2. **`.github/policy.yml`** — add sections under `required_files.by_language`,
   `required_dirs.by_language`, and `excluded_paths` (build output, dependency caches,
   lockfiles). Placeholders already exist for some languages; an empty list is a valid
   answer, a missing key is not.
3. **`.github/CODEOWNERS`** — add `/core/<lang>/**` and `/contrib/<lang>/**` lines.
   Remember last-match-wins: place them where the intended rule ends up last.
4. **`.github/dependabot.yml`** — add a `package-ecosystem` entry. Counter-intuitive
   but required: the entry exists to **suppress** updates, not request them. Read that
   file's header before writing it, and check
   `.github/scripts/tests/test_dependabot_config.py`, which asserts the suppression
   shape.
5. **`.github/workflows/`** — a `<lang>-format.yml` and a `<lang>-tests.yml`, modelled
   on an existing pair.
6. **`.github/workflows/validate-recipe-structure.yml`** — add the new root to the
   workflow's path filters, including the retired `<lang>/agents/**` path if one exists.
7. **`tools/validate_manifest.py`** — `LANGUAGE_NAMESPACE_DIRS` is a hardcoded set.
   A language missing here is accepted by the schema and rejected by the validator.
8. **`.github/policy.yml`, `frozen_paths`** — only if the language has a retired
   `<lang>/agents` root to close.
9. **Docs** — `docs/recipe-handbook/languages/<lang>.md`, plus the language mentions in
   `docs/recipe-checklist.md` and `docs/recipe-handbook/anatomy.md`.

**Verify:** `uv run validate all` from the repo root, and confirm the new
`<lang>-format` / `<lang>-tests` workflows appear on a PR touching that root.

**Gotcha:** searching for an existing language name to find every touchpoint misses
`.github/CODEOWNERS`, which has no file extension. Do not filter the search by
extension.

---

## Change who reviews a path

1. **`.github/CODEOWNERS`** — edit or add the line. Rules are **last-match-wins**, so a
   new line at the bottom overrides everything above it for the paths it matches.

The file's own header documents how to change the catch-all reviewer and how to add a
language folder. Read it rather than reasoning about it.

**Gotcha:** the change itself needs approval from the *current* owner of that path, so
whoever is being replaced approves their own replacement.

---

## Change a limit or threshold

1. **`.github/policy.yml`** — edit the value. That is the whole procedure.

The file is the single source of truth by design: workflows read it through
`.github/scripts/load_policy.py`, and docs link to it rather than restating the numbers.

**Before promising it is a one-line change**, confirm nothing hardcodes the value
independently. That is the consumer trace in `drift-checks.md`. Known offenders exist —
`tools/validate_manifest.py` keeps its own language set — so the claim is a strong
default, not a guarantee.

**Gotcha, staleness values specifically:** the numbers are absolute days since last
activity, and `stale-sweep.yml` passes `actions/stale` the *difference* between the
nudge and the close value. Change one and you change the gap. The workflow asserts the
difference is positive.

---

## Add a required file or directory

1. **`.github/policy.yml`** — add to `required_files` or `required_dirs`, under
   `always`, `by_root[<root>]`, or `by_language[<lang>]` depending on how wide the rule
   should be. No code change is needed; `tools/validate_structure.py` reads the policy.

**Before adding a directory**, read the `required_dirs` header. An empty directory
passes, so the bar is "the recipe is broken without it", not "the recipe is unusual
without it" — several directories were deliberately removed under that bar.

**Case sensitivity:** entries are matched exactly, so a contributor on a
case-insensitive filesystem can pass locally and fail on CI. `case_insensitive_files`
relaxes that per-entry, and its header lists which files must never be added to it.

**Existing recipes are not grandfathered.** Adding to `always` or a populated
`by_root` breaks every recipe that lacks the file, at once.

---

## Retire, move, or rename a recipe

1. **Move the files.** Recipes belong at `<root>/<language>/<recipe>`, except under
   `skills/`, where the middle folder is a `<vertical>`.
2. **`.github/policy.yml`, `frozen_paths`** — if closing a whole root. A PR that adds
   or modifies files under a frozen path fails; deletions and renames are exempt, so
   migrating out is never blocked.
3. **Dependabot PRs against the old path** are closed automatically by
   `dependabot-housekeeping.yml`; nothing to do.
4. **Check inbound references** — other recipes' READMEs, docs pages, and
   `docs-links.yml` will fail a PR that leaves a dangling internal link.

**Verify:** `uv run validate placement`, which exists precisely because the per-recipe
checkers only see recipes the collector already found, and a misplaced recipe is
invisible to that collector.

**Gotcha:** an escape hatch exists for legacy paths via a PR label, and only users with
write access can apply labels.

---

## Exempt something from a sweep

- **A pull request or issue** — apply the keep-open label named in
  `stale_policy.keep_open_label`. Issues additionally have `exempt_labels`, and are
  exempt when assigned or milestoned. Only users with write access can apply labels, so
  this is a maintainer override rather than a contributor escape hatch.
- **A branch** — add it to `stale_policy.branches.protected`. Shell globs work. The
  default branch and any branch with a protection rule are skipped automatically.
- **Never remove the canary label from `exempt_labels`.** The recipe canary stores its
  escalation ladder in the issue's labels and finds its issue by title among *open*
  issues. Closing one does not pause the ladder, it erases it, and the two workflows
  then fight monthly forever.

---

## Add a repo skill

1. **`.agents/skills/<name>/SKILL.md`** — frontmatter needs `name` and `description`.
   The description is what makes the skill trigger; write it around what the user will
   say, not around what the skill does internally.
2. **`scripts/` and `tests/`** — only if the skill needs code. The code goes in
   `scripts/`, its tests in `tests/`. `.github/workflows/tools-tests.yml` runs them, so
   code without tests is code CI does not cover.
3. **`docs/recipe-handbook/skills-catalog.md`** — add an entry if the skill helps
   contributors prepare a recipe. `docs-links.yml` validates the links.

**Gotcha:** `AGENTS.md` forbids mixing `.agents/skills/` changes with recipe or
vertical-skill changes in the same PR.

---

## Add a validator

1. **`tools/validate_<name>.py`** — expose `main(scope: str | None) -> int`. The scope
   convention is documented in `tools/README.md`.
2. **`tools/validate.py`** — register it in `SUBCOMMANDS`.
3. **`tools/tests/`** — covered by `tools-tests.yml`.
4. **A workflow** — registering the subcommand makes it runnable locally; it does not
   make CI run it.

---

## Set up CI cloud auth

Read `.github/terraform/README.md`. It covers Workload Identity Federation between
GitHub Actions and Google Cloud, and outputs the provider value the workflows consume.
