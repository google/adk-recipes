<!-- word count: 948 (target 900, cap 1200) -->

# Recipe Checklist

Everything you need to submit a `contrib/` recipe, on one page.
Deep detail lives in [`recipe-handbook/`](./recipe-handbook/README.md).

---

## Proposing a new recipe

If this is a **new** recipe, open a
[Propose a New Recipe](https://github.com/google/adk-recipes/issues/new?template=propose-a-new-recipe.md)
issue and wait for approval before adding it to `contrib/` and
opening a PR. Updating an existing recipe? Skip this and go
straight to the checklist.

---

## 0. AI skills

Requires an AI coding assistant (e.g. CloudCode). If you don't have one, skip to
[§ 3](#3-before-you-open-the-pr) for the manual commands.

The fastest path is `prepare-python-recipe` — it runs every other
skill in the right order.

| Skill | What it does | Example prompt |
|---|---|---|
| `prepare-python-recipe` | Runs every other skill below in sequence. The fastest path to a PR-ready recipe. | `prepare the python recipe contrib/python/my-recipe` |
| `generate-manifest` | Writes a valid `manifest.yaml` from your recipe files | `generate manifest for contrib/python/my-recipe` |
| `align-recipe-pyproject` | Fixes `pyproject.toml` to match repo conventions | `align pyproject.toml for contrib/python/my-recipe` |
| `extract-python-environment-variables` | Populates `.env.example` from Python source and adds `load_dotenv()` where needed | `extract env vars for contrib/python/my-recipe` |
| `generate-python-runnability-test` | Writes `tests/test_runnability.py` | `generate runnability test for contrib/python/my-recipe` |
| `make-python-recipe-deployable` | Adds the serving files so the recipe can run as a container. Opt-in — not run by `prepare-python-recipe` | `make contrib/python/my-recipe deployable` |

For deep detail on each skill, see the
[Repo Skills Catalog](./recipe-handbook/skills-catalog.md).

---

## 1. Always

- [ ] Recipe has a clear, unique purpose: a concrete problem it
      solves and something new to teach — see
      [What makes a good recipe](./recipe-handbook/README.md#what-makes-a-good-recipe)
- [ ] Recipe lives at `contrib/<lang>/<name>` —
      [anatomy](./recipe-handbook/anatomy.md)
- [ ] Recipe name (folder name) ≤ 30 chars, lowercase + hyphens
      only
- [ ] Under size limit: 70 files / 2 MB for `contrib/` — use WebP for
      doc-only screenshots/diagrams
- [ ] `manifest.yaml` valid, with real `ownership.team` and
      `ownership.poc` — AI skill: `generate-manifest`
- [ ] `README.md` has ≥ 100 words, a setup section, and a run
      section with a code block — CI enforces this;
      [details](./recipe-handbook/anatomy.md#readmemd)

## 2. Your language

> **Deprecated models:** `gemini-2.0-flash` and `gemini-2.5-flash` are no
> longer accepted. Use `gemini-3.5-flash`.

**Python** — [details](./recipe-handbook/languages/python.md)
- [ ] `pyproject.toml` aligned — AI skill:
      `align-recipe-pyproject`
- [ ] `uv.lock` in sync — run `uv lock` from the recipe root
- [ ] `.env.example` declares every env var the recipe reads —
      AI skill: `extract-python-environment-variables`
- [ ] `load_dotenv()` called in the package `__init__.py` (not
      `agent.py`)
- [ ] Model names read from env vars, not hardcoded in source
- [ ] `tests/test_runnability.py` present — AI skill:
      `generate-python-runnability-test`

**Java / Go / TypeScript / Kotlin** — language-specific guidance is
in progress; start with the relevant page before writing code:
[Go](./recipe-handbook/languages/go.md) ·
[Java](./recipe-handbook/languages/java.md) ·
[TypeScript](./recipe-handbook/languages/typescript.md) ·
[Kotlin](./recipe-handbook/languages/kotlin.md).
Structural checks in § 3 already apply — run
`uv run validate $RECIPE_PATH` and review
[anatomy.md](./recipe-handbook/anatomy.md) for layout rules.

## 3. Before you open the PR

The commands in this section run from the repo root and mirror
what CI runs. They all use `$RECIPE_PATH` — set that variable
first.

### Set your recipe path

Do this once before running any of the commands below:

```bash
export RECIPE_PATH=contrib/python/my-recipe
```

Replace `my-recipe` with your recipe's folder name.

### One-liner (paste this first)

One block, one paste. Requires `$RECIPE_PATH` from above.

```bash
# From the repo root
uv run validate $RECIPE_PATH                # Validates manifest and structure
uv run ruff format $RECIPE_PATH             # Formats the recipe code
uv run ruff check --fix $RECIPE_PATH        # Fixes lint errors

# From the recipe root
cd $RECIPE_PATH
uv lock                                     # Updates the lock file
uv run pytest                               # Runs the tests
```

### Structural checks

`uv run validate <recipe-path>` runs all structural validators
against your recipe and reports PASS / FAIL for each.

- [ ] All checks pass:
      ```
      uv run validate $RECIPE_PATH
      ```

Individual validators (useful for isolating one failure):

```bash
uv run validate manifest $RECIPE_PATH
uv run validate structure $RECIPE_PATH
uv run validate readme $RECIPE_PATH
```

- `validate manifest` — checks `manifest.yaml` against the
  schema and verifies `ownership.team` / `ownership.poc` are
  not placeholders.
- `validate structure` — checks folder name, size, required
  files, and layout.
- `validate readme` — checks README.md for a setup section,
  run section, code block, and minimum word count.

### Format and lint (Python only)

- [ ] Format and lint pass:
      ```
      uv run ruff format $RECIPE_PATH
      uv run ruff check $RECIPE_PATH
      ```

### Tests (Python only)

- [ ] Tests pass (integration excluded, same as CI):
      ```
      cd $RECIPE_PATH
      uv run pytest --ignore=tests/integration --ignore-glob="**/test_integration.py"
      ```

### Integration tests

CI excludes integration tests by default. See
[python.md — Integration tests](./recipe-handbook/languages/python.md#integration-tests)
for exclusion patterns and how to run them locally before opening
a PR.

## 4. When something fails

- CI failing on your PR? →
  [troubleshooting](./recipe-handbook/troubleshooting.md)
- Fix `validate-recipe-structure` failures first — structural
  errors can mask Python-specific checks downstream.
- Some failures cascade: a stale `uv.lock` causes both
  `python-dependency-policy` and `python-tests` to fail. Fix
  the root cause before pushing again.
- CI re-runs automatically on every push to your PR branch.
  No manual trigger is needed.
- Want the full story? →
  [handbook overview](./recipe-handbook/README.md)

## 5. Automated review

Three AI reviewers — correctness, security and maintainability — run when
you open a PR and on every push, forks included. They comment on added
lines, only for critical or high severity issues, and are advisory: a
maintainer still reviews and approves.

A maintainer can re-run them by commenting `@ai-review` on the PR,
optionally followed by what to focus on. PRs above 300 changed files are
skipped, because GitHub will not serve a diff that large.

---

↑ [Docs home](./README.md) · [Handbook](./recipe-handbook/README.md)
