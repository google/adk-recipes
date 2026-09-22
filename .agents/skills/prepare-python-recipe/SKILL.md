---
name: prepare-python-recipe
description: >
  End-to-end orchestration to prepare or update a Python recipe under
  core/python/, contrib/python/, or skills/<vertical>/<solution>/ so it
  passes every check in .github/workflows/python-validate-recipe.yml.
  Runs eight phases in
  order on an already-in-place recipe: manifest.yaml generation,
  environment-variable extraction, pyproject.toml alignment, ruff
  format+check, per-recipe `uv lock`, runnability-test generation,
  compile-and-run verification of the generated test file, and a final
  pass through the repo's own `validate manifest` / `validate structure`
  validators. Assumes
  the user has
  already done the manual prep (deactivated any venv, `git pull` and
  `uv sync` from the repo root, placed the recipe at its target path,
  renamed if needed). Delegates to the existing sub-skills
  (generate-manifest, extract-python-environment-variables,
  align-recipe-pyproject, generate-python-runnability-test) so the master
  never duplicates their logic. Pauses at fixed decision points
  (description mismatch, existing test regeneration) AND is free to
  interrupt for clarification any time a phase's output
  looks ambiguous, unexpected, or would benefit from a human judgment
  call — this is an interactive skill by design. Use when the user wants
  to "prepare a recipe", "update a recipe end to end", "run all the
  checks and fixes", "make this recipe PR-ready", or invokes it by name.
metadata:
  author: Google
  license: Apache-2.0
  version: 1.1.0
---

# Prepare Python Recipe

Master orchestration skill. Runs the other Python-recipe skills in the right order, with the right inputs, in a single pipeline. Use when the user wants a recipe brought fully up to standard in one go.

**This is an interactive skill.** It's expected to pause and ask questions when doing so genuinely improves the outcome — not just at the fixed checkpoints below, but any time a phase's output is ambiguous, surprising, or would benefit from a judgment call. See rule 5 (fixed checkpoints) and rule 6 (judgment-based interruptions) for the difference.

---

## Canonical placeholder strings

Two ownership placeholders are written by `generate-manifest` and enforced by `tools/validate_manifest.py`. They must be the EXACT strings below — never invent, translate, or rephrase them:

```
OWNERSHIP_TEAM_PLACEHOLDER = "TODO: Replace with your team name"
OWNERSHIP_POC_PLACEHOLDER  = "TODO: Replace with your GitHub user ID"
```

`generate-manifest` is the single source of truth for these values. This skill NEVER replaces them mid-pipeline — they are intentionally left in place so CI validation fails until a human fills them in. Replacing them lives in the user's post-pipeline TODO list (see the summary's "What you still need to do" section).

---

## Prerequisites (manual, done by the user BEFORE invoking this skill)

The skill assumes the user has already:

1. **Deactivated** any active Python virtual environment.
2. **Pulled latest** from `origin` (`git pull` at the repo root).
3. **Synced repo root deps** (`uv sync` at the repo root).
4. **Placed the recipe at its target path** — either freshly scaffolded, moved from another location, or renamed to its final basename under `core/python/<name>/`, `contrib/python/<name>/`, or `skills/<vertical>/<solution>/`.
5. **Committed the original recipe** so `git diff` shows what the skill changed.

If the user has NOT done these and asks you to run the skill anyway, tell them to complete the prerequisites first and stop. Do NOT run `git pull`, `git commit`, deactivate their venv, or move/rename directories on their behalf — those are deliberately out of scope.

---

## What This Skill Does

Runs eight ordered phases against a target recipe. Each phase either invokes an existing sub-skill (or its underlying script) or runs a repo-standard command:

1. **Manifest** — generate `manifest.yaml` if missing. Ownership placeholders (`ownership.team`, `ownership.poc`) are LEFT AS-IS — never replaced mid-pipeline. See "Canonical placeholder strings" above.
2. **Environment variables** — extract env vars used by the recipe into `.env.example`; ensure `load_dotenv()` is bootstrapped and `python-dotenv` is a dep.
3. **Align pyproject.toml** — remove `[tool.ruff*]`, raise `requires-python` floor, ensure `[project].name` matches folder, reconcile description with manifest, ensure `[[tool.uv.index]]` declares public PyPI as default (needed to bypass corp Airlock), and report stale sub-3.11 version references plus a `testpaths` that would exclude the runnability test.
4. **Lint** — `ruff format` + `ruff check --fix` on the recipe (from the repo root, so the root ruff config wins). **Must run AFTER Phase 3** — align removes any recipe-local `[tool.ruff*]` block, and that removal is what makes the root config the effective one. Running lint before align would check against the recipe's (often more permissive) local config and miss violations that CI will later catch.
5. **Recipe `uv lock`** — regenerate `uv.lock` so it reflects the post-align `pyproject.toml`. Does NOT install into `.venv/` — that's a heavier step the user runs after they've reviewed the diff. `uv lock` just resolves and records; `uv sync` would download and install every wheel, which is scope-creep for a "prepare" pipeline.
6. **Runnability test** — generate `tests/test_runnability.py` if missing (or ask before overwriting), plus a `tests/conftest.py` path shim when the recipe isn't installable.
7. **Verify (compile + run)** — `py_compile` the runnability test, then run it with pytest. The compile step is a syntax check; running it is what proves the test's `import` can actually resolve (`--collect-only` would not — the guarded test shape puts the import inside the test function). The test is side-effect-free by construction.
8. **Validate (repo validators)** — run `uv run validate manifest` and `uv run validate structure` on the recipe. This is the phase that catches everything the seven build phases don't model: required files, required directories (`tests/unit/` for vertical skills), size limits, naming.

At the end, print a summary table and remind the user to `git diff` and commit — the skill never commits.

**Why Phase 8 exists.** The pipeline used to end at Phase 7 and report a clean run while `uv run validate structure` failed — the recipe passed every phase the skill modelled and was still rejected by CI. Reimplementing policy checks inside this skill would guarantee drift, so the pipeline defers to the repo's own validators as the last word.

**Deployability is deliberately NOT a phase here.** The serving files a container needs (`Dockerfile`, `fast_api_app.py`, `app_utils/`, the serving dependencies) are the job of a separate opt-in skill, `make-python-recipe-deployable`. Most recipes neither need nor want them, and adding them can require an ADK major migration the owner must do by hand — so it must never run unasked as part of "prepare". If the user wants a deployable recipe, finish this pipeline first, then invoke that skill.

---

## Rules for the Agent

1. **Ask for `--recipe-dir` up front** if the user hasn't given one. All eight phases operate on the same recipe.

2. **Confirm before starting**. The pipeline touches many files. Show the user the plan (the eight phases + the target recipe path) and ask for a single "yes, go ahead" before Phase 1. Do NOT prompt again for each phase unless a decision is required (see rules 5 and 6).

3. **Invoke sub-skill SCRIPTS directly** (not the sub-skills' own agent-facing SKILL.md). Reason: sub-skills each have their own "want me to apply?" prompt. In master-orchestration mode the user has already opted into apply for the whole pipeline; individual prompts would be noise. Command lines for each sub-script are given in each phase below.

4. **Exception for pure-instructions skills**: `generate-manifest` has no script — it's a pure-instructions skill. For that one only, load its SKILL.md (via the `skill` tool) and follow it inline.

5. **Fixed checkpoints — always pause here**:
   - **Description mismatch** — if Phase 3 returns `needs_input` for `description-matches-manifest`, show both sides and ask the user to pick `pyproject`, `manifest`, or `delete`.
   - **Test file exists** — before Phase 6, if `tests/test_runnability.py` already exists, ask whether to regenerate (default: keep existing). Regeneration uses `--overwrite`.
   - **Entry point not found (Phase 6)** — if the runnability-test generator errors because no `agent.py` was found, surface the message and offer to re-run with `--agent-file <path>` once the user says where the entry point is. This is the one `error` case with a defined recovery instead of a hard stop.
   - **Anything else a sub-script flags as `error`** — surface the message, stop the pipeline, do NOT retry (the entry-point case above is the only exception).

6. **Judgment-based interruptions — pause when it genuinely helps.** This skill is interactive by design. Beyond the fixed checkpoints in rule 5, feel free to interrupt any time doing so meaningfully improves the outcome. Some situations where a pause is appropriate:
   - A sub-script returns unexpected detections (e.g. the runnability-test generator reports `has_root_agent: false` — a legit recipe should always have one; something may be wrong).
   - The manifest generator inferred an `architecture.agent = "multi"` where you counted only one agent, or vice versa.
   - The environment-variable extractor added an unusually large number of new vars (say, ≥ 10) — worth having the user glance at the list.
   - The align script's proposed rewrite of `requires-python` drops support for a version the recipe's README claims to support.
   - `uv lock` in Phase 5 records a suspicious dependency in `uv.lock` (say, a package that renames or shadows a well-known one — reviewable via `git diff uv.lock`).
   - The recipe has a non-standard layout the sub-skills don't recognise (multiple `agent.py` files, no `app/` package, etc.) and you're unsure which to use.
   - Any time the "right answer" for a step depends on knowledge outside the recipe itself (project conventions, team decisions, downstream consumers).

   Do NOT interrupt for:
   - Progress updates ("Phase 3 done, moving to Phase 4?") — just move on.
   - Cosmetic curiosity ("I noticed a TODO in agent.py, want to discuss?") — out of scope.
   - "Just to make sure" prompts where the answer wouldn't change what you do next.

   When you interrupt, present the specific concern, show the relevant data, and offer concrete options — don't just say "does this look OK?".

7. **Halt on hard error**. If any phase's script exits with a non-zero code that isn't `refused_overwrite` (handled by rule 5), stop. Print the phase name, the error, and what's already been done. Do NOT continue past a hard error.

   **Carve-out for Phase 3 (align).** The align script exits `1` whenever any check is left in `report_only` status (e.g. missing `[build-system]`, a non-PyPI default index) — those are deferrals-by-design, NOT hard errors (see Phase 3b). Do NOT halt on Phase 3's exit code alone: decide from its JSON. Halt only if a check has status `error` (or an unexpected `needs_input` / `would_fix`); a non-zero exit whose only non-clean checks are `report_only` means "continue".

8. **Report progress compactly**. After each phase, one line: `Phase N (<name>): <one-line outcome>`. Do NOT dump raw JSON. Do NOT re-render each sub-skill's own table — the summary at the end covers it. Judgment-based interruptions from rule 6 are separate from progress lines and should be their own turn (question, then wait for the answer).

9. **Never commit**. The skill is done when the summary is printed. Let the user `git diff` and commit.

---

## Input

| Field | Required | Description |
|---|---|---|
| Recipe directory | Yes | Path to the recipe root (e.g. `core/python/cross-session-memory`, `contrib/python/my-recipe`, `skills/retail/store-ops`). Passed to every sub-script as `--recipe-dir`. |

If the user has not specified the recipe directory, ask for it before proceeding.

---

## Pipeline

### Phase 0 — plan + confirm (do this first, always)

**Step 0a — Verify the recipe directory actually exists.** A path typo shouldn't cost the user the whole plan-confirmation round-trip only to fail in Phase 1:

```bash
[ -d <RECIPE_DIR> ] || { echo "Recipe directory not found: <RECIPE_DIR>"; exit 1; }
```

If it isn't a directory, stop immediately with that message — do NOT show the plan or prompt.

**Step 0b — Verify the recipe folder name matches CI's naming rules.** `python-validate-recipe.yml`'s Check 1 (folder-name regex + max length) rejects folders that don't match `^[a-z][a-z-]*$` or exceed `.github/policy.yml` `recipe_naming.max_folder_name_length`. Historically the pipeline was BLIND to this — it would run every phase against a folder named `data_science` or `MyBadName`, report success, and let CI reject the PR later (or worse: Phase 3's `project-name-matches-folder` would propagate the bad name into `[project].name`). This check catches it up front.

```bash
MAX_LEN=$(uv run --no-project --with pyyaml python3 .github/scripts/load_policy.py recipe_naming.max_folder_name_length)
uv run --no-project python3 .agents/skills/prepare-python-recipe/scripts/check_folder_name.py \
  --recipe-dir <RECIPE_DIR> --max-length "$MAX_LEN"
```

The check exits 0 silently on a compliant name; on violation it exits 1 with the specific offending characters, the length overrun (if any), and a suggested compliant name derived from the current one (lowercase, `_` → `-`, drop disallowed characters, truncate on a hyphen boundary). The suggestion is ADVISORY — the script never renames anything.

**If it fails, HALT the pipeline before Phase 1.** Print the script's stderr verbatim (it already includes the suggestion and the manual `git mv` command). Do NOT show the plan, do NOT prompt to proceed, do NOT ask "want me to rename?" — renaming a recipe directory is the user's decision, not the skill's. They rename by hand and re-invoke the skill.

**Only proceed past this step if the folder-name check passed.**

**Step 0c — For a recipe under `skills/`, check the required directories.** `.github/policy.yml` `required_dirs.by_root.skills` mandates a fixed shape for every vertical skill — `scripts/`, `assets/`, `references/`, and `tests/unit/`. None of the eight phases creates these, so a missing one survives the whole pipeline and fails `validate structure` in Phase 8 (and CI). Surfacing it here means the user can create the directory before anything else runs, rather than reading about it in the final summary.

Skip this step entirely for `core/` and `contrib/` recipes — `required_dirs.by_root` is empty for both.

```bash
uv run --no-project --with pyyaml python3 -c "
import pathlib, sys, yaml
recipe = pathlib.Path('<RECIPE_DIR>')
policy = yaml.safe_load(open('.github/policy.yml'))
needed = policy.get('required_dirs', {}).get('by_root', {}).get('skills', []) or []
missing = [d for d in needed if not (recipe / d).is_dir()]
print('MISSING_DIRS: ' + (', '.join(missing) if missing else '(none)'))
"
```

This is INFORMATIONAL, not a halt. An empty directory satisfies the check, and git cannot commit an empty directory, so the fix is a `.gitkeep`:

```bash
mkdir -p <RECIPE_DIR>/tests/unit && touch <RECIPE_DIR>/tests/unit/.gitkeep
```

Mention any missing directories in the Step 0d plan message and offer to create them with `.gitkeep` files as part of the run. If the user agrees, create them right after they confirm the plan and before Phase 1; record them in the summary's "Files created" list. If they decline, carry the item into the final TODO list. Do NOT create them unasked — an empty scaffold directory the user didn't want is still clutter.

**Step 0d — Show the plan and get confirmation.** Before composing the plan, glance at the recipe for anything non-standard (package not called `app/`, `.env.example` outside root, missing `tests/`, extra Python source dirs, deprecated model literals per `AGENTS.md`). If any will affect what the pipeline does, flag them briefly in the plan message so the user isn't surprised mid-pipeline. Skip the flags entirely for a standard recipe.

Then flag the assumptions the pipeline is making and show the user the plan. Do NOT frame these as "prerequisites" — they're a heads-up so the user can push back if any assumption is wrong, not a preflight checklist for the user to tick off:

> A few things I'm assuming — say so if any aren't true:
>   - You've deactivated any active venv.
>   - You've run `git pull` and `uv sync` at the repo root.
>   - `<RECIPE_DIR>` is already at its target path (and renamed to its final basename).
>
> I'll run the prepare-python-recipe pipeline on `<RECIPE_DIR>` — 8 phases:
> 1. Generate manifest.yaml (if missing)
> 2. Extract env vars into .env.example
> 3. Align pyproject.toml
> 4. Ruff format + check --fix
> 5. uv lock inside the recipe (regenerates uv.lock; does NOT install .venv/)
> 6. Generate tests/test_runnability.py (if missing)
> 7. Verify the runnability test compiles and runs
> 8. Run the repo validators (`validate manifest`, `validate structure`)
>
> Nothing gets committed — you'll `git diff` at the end. Proceed?

If Step 0c found missing required directories, add one line before "Nothing gets committed":

> `<RECIPE_DIR>` is a vertical skill and is missing `tests/unit/`, which
> `.github/policy.yml` requires. Want me to create it with a `.gitkeep`?

Get a yes-or-no. If no, stop.

### Phase 1 — manifest.yaml

**1a. Check whether manifest exists.**

```bash
[ -f <RECIPE_DIR>/manifest.yaml ] && echo exists || echo missing
```

**1b. If missing** — load the `generate-manifest` skill (via the `skill` tool with `name="generate-manifest"`) and follow its instructions for this recipe. That skill writes `manifest.yaml` with the canonical ownership placeholders — LEAVE THEM AS-IS.

**1b. If exists** — skip generation. Do NOT read `ownership.team` / `ownership.poc` and do NOT prompt about them. Whatever they are (real values or the canonical placeholders), leave them untouched — the user handles ownership post-pipeline.

Progress line: `Phase 1 (manifest): generated | pre-existing.`

### Phase 2 — extract env vars

Invoke the script directly:

```bash
uv run --no-project python3 .agents/skills/extract-python-environment-variables/scripts/extract_env_vars.py \
  --recipe-dir <RECIPE_DIR>
```

(No `--dry-run` — master runs it in apply mode. Use `uv run --no-project python3`, not a bare `python3`: the script imports `tomllib`, which is stdlib only on Python 3.11+, and uv's managed interpreter guarantees that.)

The script:
- Appends any newly-discovered env vars to `.env.example`
- Injects `load_dotenv()` into the package `__init__.py` if missing
- Adds `python-dotenv>=1.0.0` to `[project].dependencies` if missing
- Replaces hardcoded model name strings (e.g. `"gemini-3.5-flash"`) in source files with bare `os.getenv(...)` calls and records the substitution in `.env.example`

Read the script's stdout (structured `[INFO]` / `[PASS]` / `[WARN]` log lines, not a table). Extract the counts: how many vars added, whether `load_dotenv` was injected, whether `python-dotenv` was added, and how many hardcoded model names were replaced (if any).

Progress line: `Phase 2 (env vars): <N> vars added to .env.example, load_dotenv <injected|already present>, python-dotenv <added|already present>[, <M> hardcoded model name(s) replaced in source].`

### Phase 3 — align pyproject.toml

**Must run BEFORE Phase 4 (lint).** Align removes any recipe-local `[tool.ruff*]` block — after removal the recipe uses the root `pyproject.toml`'s ruff config. If Phase 4 (lint) ran first, it would check against whatever local config the recipe shipped with (often more permissive than root), miss violations that root's config would catch, and CI would fail on files the pipeline claimed were clean. Do NOT reorder these two phases without changing this reasoning.

Invoke the align script directly. Two-pass logic:

**3a. Dry-run first** to detect whether description mismatch will need user input:

```bash
uv run --no-project --with tomlkit --with 'ruamel.yaml' --with packaging \
  python .agents/skills/align-recipe-pyproject/scripts/align_pyproject.py \
  --recipe-dir <RECIPE_DIR> --dry-run
```

Parse the JSON. If any check has `status == "needs_input"` for `description-matches-manifest`, pause: show both `pyproject_description` and `manifest_description` from `details`, ask the user to pick `pyproject`, `manifest`, or `delete`.

**3b. Apply** — with the description-source flag if you got one:

```bash
uv run --no-project --with tomlkit --with 'ruamel.yaml' --with packaging \
  python .agents/skills/align-recipe-pyproject/scripts/align_pyproject.py \
  --recipe-dir <RECIPE_DIR> \
  [--description-source=<CHOICE>]
```

**The align script exits `1` (non-zero) whenever any check is `report_only`** — that is expected, not a hard error, so do NOT apply rule 7's halt to it. Decide from the JSON, not the exit code: if the apply run's only non-clean checks are `report_only`, note them in the summary (the master does NOT auto-fix these) and continue; halt only if a check has status `error`. Four rules can produce `report_only`:
  - `build-system-present` (missing `[build-system]` — backend choice is editorial). **Note the knock-on for Phase 6:** with no `[build-system]` the recipe is never installed, so the runnability test's `import` cannot resolve on its own and the generator will emit a `tests/conftest.py` path shim. Mention both together in the summary rather than as two unrelated items.
  - `default-pypi-index` (a default index is declared but points somewhere other than public PyPI — divergence may be intentional)
  - `stale-python-version-refs` (files still referencing a Python below the 3.11 floor). Relay `details.files` in the summary and flag **executable** files first — a bootstrap script whose interpreter allowlist still accepts 3.10 will build a venv the recipe then refuses to install into. Prose files are lower priority.
  - `runnability-test-in-testpaths` (a `testpaths` setting that excludes `tests/test_runnability.py`, so a bare `pytest` never collects it)

Progress line: `Phase 3 (align): <N> fix(es) applied; <M> report-only issue(s) left.`

### Phase 4 — ruff

Run both from the repo root (not the recipe dir) so the root `pyproject.toml`'s ruff config wins. This runs AFTER Phase 3 on purpose (see Phase 3's header comment for why):

```bash
uv run ruff format <RECIPE_DIR>
uv run ruff check --fix <RECIPE_DIR>
```

`ruff check`'s exit code matters here — do NOT treat every non-zero code the same way:
- **Exit 1** — genuine violations ruff can't auto-fix (typically `C901` complex-structure, `PLR0912/0915` too-many-branches/statements). Do NOT stop the pipeline — note them in the summary as a Manual TODO so the user can refactor or add per-file `# noqa` markers after review.
- **Exit 2** — ruff itself errored (invalid config, a file-system problem, or a bug in ruff), NOT a violation count. Phase 4 effectively did not run, so treat this as a hard error under rule 7: stop the pipeline and surface the message. Do not mistake it for "violations remain" and continue.

Note on `E402` and `__init__.py`: Phase 2 (env-var extraction) already suppresses `E402` on any trailing relative import (`from . import agent`) in the recipe's package `__init__.py` — the canonical ADK-recipe pattern where env-bootstrap side effects (`load_dotenv()`, `os.environ.setdefault(...)`) intentionally precede a `from . import ...` line so env vars are populated before agent submodules load. If you see `E402` on an `__init__.py` in Phase 4's output, something went wrong upstream (Phase 2 didn't detect the pattern, or a new file appeared between Phases 2 and 4). Note that in the progress line so it isn't quietly buried.

Progress line: `Phase 4 (lint): <N> file(s) formatted, <M> issue(s) auto-fixed, <K> unfixable issue(s) left.`

### Phase 5 — recipe `uv lock`

Now that `pyproject.toml` is stable (Phase 3 aligned it, Phase 4 didn't touch it), regenerate the lockfile so it matches:

```bash
uv lock --python 3.11
```

Run this WITH `workdir = <RECIPE_DIR>` (do not `cd` — pass the working directory via the tool call).

**Why `--python 3.11` explicitly?** CI's `.github/workflows/python-dependency-policy.yml` pins Python 3.11 when it runs `uv lock --check` on every recipe. If we lock here with whatever interpreter the user happens to have installed (typically 3.12 or newer on modern machines), the recipe locks cleanly locally but the CI check fails on the PR with a confusing `The requested interpreter resolved to Python 3.11.15, which is incompatible with the project's Python requirement: >=3.12` — mis-reported by the workflow as "lockfile is out of date". Forcing 3.11 here surfaces the same incompatibility at pipeline time, when the user can fix it or push back, rather than at PR time. Phase 3's `python-version-floor` check should have already caught this and rewritten `requires-python`, so the lock should succeed — but pinning defends against edge cases where the check was too permissive or the recipe had a compatible-release ceiling the rewrite couldn't lower.

**Why `uv lock` and not `uv sync`?** The pipeline's job is to prepare the recipe, not to install and validate its runtime environment. `uv lock` resolves dependencies against the aligned `pyproject.toml` and writes `uv.lock` — that's the artefact CI and downstream consumers need. `uv sync` would additionally download every wheel into `.venv/`, which:
- Is slow (minutes of network I/O).
- Sets up a dev environment the user may or may not want.
- Would fail in ways the pipeline can't cleanly report (build errors in C extensions, network flakiness).

The user gets a real `.venv/` by running `uv sync` themselves after reviewing the diff — see the "Next steps" block at the end of the summary.

If `uv lock --python 3.11` fails, halt and surface the error verbatim (rule 7). The most common cause is a `requires-python` specifier that excludes 3.11 (`>=3.12`, `~=3.12`, etc.) that Phase 3 could not rewrite — the fix is to either lower the recipe's floor to `>=3.11` or, if the recipe genuinely needs newer features, raise the issue with the repo maintainers to update CI's pinned interpreter.

Progress line: `Phase 5 (recipe lock): uv lock --python 3.11 completed.`

### Phase 6 — runnability test

**6a. Check whether the test exists.**

```bash
[ -f <RECIPE_DIR>/tests/test_runnability.py ] && echo exists || echo missing
```

**6b. If missing** — generate:

```bash
uv run --no-project python3 .agents/skills/generate-python-runnability-test/scripts/generate_runnability_test.py \
  --recipe-dir <RECIPE_DIR>
```

(Use `uv run --no-project python3`, not a bare `python3`, so a modern managed interpreter runs it — matching this sub-skill's own SKILL.md and the rest of the pipeline.)

**6b. If exists** — pause and ask the user: keep existing (default) or regenerate. If they choose regenerate, re-run the same command with `--overwrite` appended.

If the script errors (no `agent.py` found), surface the message and offer to re-run with `--agent-file <path>` when the user tells you where the entry point is.

**Read the report's `import_support`, `conftest_action`, and `warnings` fields.** The generated test does `import <module>`, which only resolves if the recipe root is on `sys.path` — not automatic under pytest. When the recipe declares no `[build-system]` the generator also writes a `tests/conftest.py` path shim (`conftest_action: wrote`) and explains why in `warnings`. Relay every warning; do not silently drop them. If `conftest_action` is `skipped`, an existing `tests/conftest.py` was left untouched and may not provide the shim — carry that into the summary as a Manual TODO.

Progress line: `Phase 6 (runnability test): generated | kept existing | regenerated[; tests/conftest.py path shim written (recipe has no [build-system])].`

### Phase 7 — verify (compile + run the runnability test)

Two escalating checks on the generated (or existing) `tests/test_runnability.py`. The test is designed to be side-effect-free (it patches `vertexai.init` and `google.auth.default`), so neither step needs `.env`, ADC, or network.

**7a. Check the test file exists.** If Phase 6 skipped generation (agent.py not found, so no test was written) or the user chose not to regenerate an existing broken test, there may be nothing to check. Skip and record it.

```bash
[ -f <RECIPE_DIR>/tests/test_runnability.py ] && echo exists || echo missing
```

**7b. Compile** — is it valid Python?

```bash
uv run --no-project python3 -m py_compile <RECIPE_DIR>/tests/test_runnability.py
```

Use `uv run --no-project python3` here — not a bare `python`/`python3`. Two reasons: `python` may not be on PATH at all on some systems, and (more importantly) a guarded test with multiple patches emits a parenthesized `with (...)` block, which is **Python 3.10+ syntax**. Compiling it under an older system interpreter would report a spurious `SyntaxError` on a file that is actually valid. uv's managed interpreter is 3.11+, so this is a true syntax check rather than a version artifact.

**7c. Run it** — does the import actually resolve?

```bash
uv run --no-project --with pytest pytest tests/test_runnability.py -q
```

Run this WITH `workdir = <RECIPE_DIR>` so pytest's rootdir matches what a user would get running the test themselves.

**Why run it rather than `--collect-only`.** `py_compile` only parses; it passes happily on a test whose `import app.agent` can never resolve. But `--collect-only` is no better here: the *guarded* test shape (the common one for ADK recipes) puts the import INSIDE the test function, under a `with patch(...)` block, so collection imports the test module without ever touching the recipe's module. Only actually running the test exercises the import.

**A third-party `ModuleNotFoundError` is EXPECTED here, not a finding.** Phase 5 ran `uv lock`, not `uv sync`, so the recipe's dependencies are NOT installed. Classify by the module named in the error:

- The name is a **dependency** (`vertexai`, `google.adk`, `pandas`) → expected. Report `deps not installed`. Note that this outcome is **inconclusive** about the import path: the dependency failure fires before the recipe's own import is reached, so the test proves nothing either way. Fall back to Phase 6's `import_support` field for that question (`installable` / `pythonpath-ini` / `existing-conftest` / `generated-conftest` all mean the path is provided; only `unresolved` is a problem).
- The name is the recipe's **own top-level module** (the first segment of Phase 6's `module_name` — e.g. `scripts` from `scripts.agent`) → REAL finding. The import path is broken and the test can never pass, whatever else is installed.

Report per outcome:
- Compile 0, test passes → **pass.** Progress line: `Phase 7 (verify): compile OK, test passes.`
- Compile 0, fails on a dependency → **pass with note.** Progress line: `Phase 7 (verify): compile OK; test not run (<module> not installed — run uv sync). Import path: <import_support from Phase 6>.`
- Compile 0, fails on the recipe's own module → **fail.** Progress line: `Phase 7 (verify): compile OK but <module> is not importable.` Record as a Manual TODO; the usual cause is a missing `[build-system]` (Phase 3 report-only) with no conftest shim.
- Compile 0, fails an ASSERTION (`root_agent is None`) → **fail.** A genuine recipe defect, not an environment one. Report it verbatim.
- Compile non-zero → **fail.** Print the stderr verbatim in the summary as a Manual TODO and SKIP 7c (a file that doesn't parse cannot run). Do NOT attempt to diagnose, retry, or auto-fix. Progress line: `Phase 7 (verify): compile FAILED — <one-line snippet>.`
- File missing → **skip.** Progress line: `Phase 7 (verify): skipped (no tests/test_runnability.py to check).`

Never halt the pipeline on Phase 7 — Phase 8 still runs and the summary still prints.

Note: a `deps not installed` result means Phase 7 has NOT proven the recipe runs. Say so plainly in the summary rather than implying a clean bill of health; the real confirmation is the manual `uv sync && uv run pytest` step under "Next steps".

### Phase 8 — validate (the repo's own validators)

Runs LAST. Phases 1–7 model what the pipeline knows how to *build*; this phase asks the repo whether the result is actually acceptable. It is deliberately a thin wrapper: the checks live in `tools/` and `.github/policy.yml`, and reimplementing any of them here would guarantee drift.

**Historical gap this closes:** the pipeline ended at Phase 7 and printed a clean summary for a recipe that `validate structure` rejected for a missing `tests/unit/` directory. Every modelled phase passed; CI still failed the PR.

**8a. Validate the manifest.**

```bash
uv run validate manifest <RECIPE_DIR>
```

**8b. Validate the structure.**

```bash
uv run validate structure <RECIPE_DIR>
```

Both run from the repo root with a repo-root-relative `<RECIPE_DIR>`, never an absolute path. Both exit non-zero on failure — do NOT apply rule 7's halt (Phase 8 is last, and its failures are findings to report, not crashes).

**Interpreting the output — two distinct kinds of failure:**

- **Ownership placeholder failures are EXPECTED.** `ownership.team` / `ownership.poc` still hold the canonical placeholders, and the validator fails deliberately until a human replaces them. Report these as `expected` in the summary, not as a problem the pipeline caused. They are already item 1 of the TODO list.
- **Everything else is a REAL finding.** Missing required files or directories, size-limit violations, schema errors, naming violations. List each verbatim in Section 3 with the fix.

Progress line: `Phase 8 (validate): manifest <clean|placeholders only|N issue(s)>; structure <clean|N issue(s)>.`

If the only failures across both validators are the two ownership placeholders, the recipe is in the expected end state — say so plainly rather than presenting it as a failure.

---

## Respond

While the pipeline runs, print a short progress line per phase (see above). Do NOT dump raw JSON. Do NOT re-render sub-skill tables.

**Track three things as the pipeline runs** so you can report them at the end:
- Every file the pipeline created or modified (across all eight phases). Observe this from each sub-script's stdout plus your own knowledge of what each phase touches (Phase 0c → `tests/unit/.gitkeep` and any other required dir, if the user opted in; Phase 1 → `manifest.yaml`; Phase 2 → `.env.example`, package `__init__.py`, `pyproject.toml`, any source files where a hardcoded model name was replaced; Phase 3 → `pyproject.toml`; Phase 4 → any `.py` under the recipe; Phase 5 → `uv.lock`; Phase 6 → `tests/test_runnability.py` and possibly `tests/conftest.py`; Phases 7 and 8 → nothing).
- Every action the pipeline **attempted but couldn't complete** (a phase halted by rule 7, Phase 4 unfixable ruff, Phase 7 compile or run failure).
- Every deferred item that needs human follow-up (Phase 3 `report_only`, Phase 8 real validator findings).

At the end, print the sections below in order. Section 3 is **conditional** — omit it entirely if nothing belongs in it. Sections 1, 2, and 4 always print.

### 1. Summary table

| Phase | Outcome | Notes |
|---|---|---|
| 1. Manifest | ok | generated (ownership placeholders left for user to fill in) |
| 2. Env vars | ok | 3 added, load_dotenv injected, python-dotenv added |
| 3. Align | ok | 2 fixes applied |
| 4. Lint | ok | 12 files formatted, 4 issues auto-fixed |
| 5. Recipe lock | ok | done |
| 6. Runnability test | ok | generated |
| 7. Verify (compile + run) | ok | compiles; test passes |
| 8. Validate (repo validators) | ok | manifest + structure clean except ownership placeholders |

Use plain words in the Outcome column (`ok` / `skipped` / `failed`). No emoji unless the user asked for them.

Phase 8 is `ok` when the only failures are the two ownership placeholders — that is the expected end state, not a defect. Use `failed` only for real validator findings.

### 2. Files created or modified

Short bullet list, grouped **Created** and **Modified**, one line per file. Aggregate large groups (e.g. "12 `.py` files formatted (Phase 4)") rather than listing each individually. Omit files that were checked but untouched. If a group is empty, drop its heading. If nothing changed at all, print `Nothing changed — the recipe was already fully aligned.`

Example:

> **Created**
> - `manifest.yaml` (Phase 1)
> - `tests/test_runnability.py` (Phase 6)
> - `tests/conftest.py` (Phase 6 — sys.path shim; recipe has no `[build-system]`)
> - `tests/unit/.gitkeep` (Phase 0c — required for `skills/` recipes)
> - `uv.lock` (Phase 5)
>
> **Modified**
> - `pyproject.toml` (Phases 2, 3 — added `python-dotenv`; aligned rules)
> - `<pkg>/__init__.py` (Phase 2 — added `load_dotenv()`)
> - `.env.example` (Phase 2 — 3 vars added)
> - `agent.py` (Phase 2 — replaced hardcoded model name with `os.getenv("EMBEDDING_MODEL")`)
> - 12 `.py` files formatted, 4 auto-fixed (Phase 4)

### 3. What the skill tried but couldn't complete (conditional — omit section if empty)

**Only print this section if at least one item belongs in it.** If the pipeline finished every attempted action cleanly, omit the section entirely — do NOT print a "nothing failed" placeholder. The summary table's Outcome column already conveys clean runs.

One line per item. This is for automation attempts that couldn't finish — not for deferrals-by-design (those go in Section 4).

Cases that go here:

- **Halted phase (rule 7 hard error)**: a phase's script exited non-zero and the pipeline stopped. Show the phase, the command that failed, and a one-line snippet of the stderr. Explicitly note which phases did NOT run as a result.
- **Phase 4 unfixable ruff**: `ruff check --fix` ran but couldn't auto-fix some violations. Show `<file>:<line>` — `<codes>` for each.
- **Phase 6 conftest skipped**: an existing `tests/conftest.py` blocked the path shim the generator wanted to write.
- **Phase 7 compile fail**: `py_compile` on `tests/test_runnability.py` returned an error. Show a one-line snippet of the stderr.
- **Phase 7 failure on the recipe's own module**: the test's import can't resolve. (A dependency-not-installed failure does NOT belong here — that's expected after `uv lock`.)
- **Phase 8 real validator findings**: anything `validate manifest` / `validate structure` reported other than the two ownership placeholders. One line each, verbatim.

Example:

> - **Phase 4 (ruff)** — 2 violations remain that `ruff check --fix` can't auto-fix: `app/deploy.py:276` (`C901`, `PLR0915`), `app/tools.py:52` (`C901`). Refactor or add `# noqa: <codes>` at the def line.
> - **Phase 7 (verify)** — `uv run --no-project python3 -m py_compile tests/test_runnability.py` failed: `SyntaxError: invalid syntax (line 14)`. Likely a generator bug or hand-edit; regenerate or fix before running pytest.
> - **Phase 8 (validate)** — `validate structure` reports `Required directory 'tests/unit/' is missing`. Create it with a `.gitkeep`.

### 4. What you still need to do

A single short TODO list. Keep every entry to one line. Standard items come first (always shown), then conditional items (only if the phase raised them), then a copy-pasteable command block. Do NOT invent items — only include what actually applies to this run.

**Standard — always include (skip only if genuinely N/A):**

1. **Fill in ownership** — open `<RECIPE_DIR>/manifest.yaml` and replace `ownership.team` (`"TODO: Replace with your team name"`) and `ownership.poc` (`"TODO: Replace with your GitHub user ID"`) with real values. CI validation intentionally fails until you do. If the manifest pre-existed and already had real values, this is a no-op — glance to confirm.
2. **Fill in `.env.example`** — replace each `<TODO: ...>` placeholder with the real value (or delete the line if the variable isn't used).
3. **Review the diff** — `cd <RECIPE_DIR> && git diff` — inspect every change the pipeline made before committing.

**Conditional — include ONLY if the phase raised it:**

- **Phase 3 report-only, `build-system`**: add a `[build-system]` block to `pyproject.toml` — see `.agents/skills/align-recipe-pyproject/SKILL.md` for hatchling / uv_build templates. If Phase 6 also wrote a `tests/conftest.py` shim, note that adding the build-system makes the shim redundant and it can be deleted.
- **Phase 3 report-only, `pypi-index`**: `[[tool.uv.index]]` default points somewhere other than public PyPI — verify this is intentional or fix per the align skill.
- **Phase 3 report-only, `stale-python-version-refs`**: list the files, executable ones first (a bootstrap script's interpreter allowlist is a live bug; prose is cosmetic).
- **Phase 3 report-only, `runnability-test-in-testpaths`**: add `"tests"` to `[tool.pytest.ini_options].testpaths`, or the runnability test never runs under a bare `pytest`.
- **Phase 0c / Phase 8, missing required directory**: create it with a `.gitkeep` (e.g. `mkdir -p <RECIPE_DIR>/tests/unit && touch <RECIPE_DIR>/tests/unit/.gitkeep`) if the user declined during Phase 0c.
- **Phase 8, other validator findings**: one line per finding with the fix.

**Commands — always show:**

```
cd <RECIPE_DIR>
uv sync                                         # install deps into .venv/ (Phase 5 only ran uv lock)
uv run pytest tests/test_runnability.py -v     # confirm the runnability test actually passes
# commit when you're happy
```

`uv sync` is what actually installs the recipe's dependencies into `.venv/`. The pipeline stopped at `uv lock` on purpose — installing is heavier and better done after you've reviewed the diff. It is also why Phase 7 usually cannot run the test to completion — it stops at the first missing dependency.

Then stop. Do NOT commit. End your turn.
