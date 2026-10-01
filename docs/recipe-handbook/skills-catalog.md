# Repo Skills Catalog

**Repo skills** your AI coding assistant invokes while working in this
repo — preparing a recipe, answering questions about how the repo
works, or validating files. Source lives at
[`.agents/skills/`](../../.agents/skills/); each has a `SKILL.md`
with a full description. This catalog summarizes them and maps
them to the [checklist](../recipe-checklist.md).

> Not to be confused with **plugins** — recipes shipped to
> users under `plugins/<vertical>/<solution>/`. Repo skills build this
> repository; plugins are recipes shipped to users.

> **Fastest path:** for a PR-ready recipe, use
> [`prepare-python-recipe`](#prepare-python-recipe). It runs every
> preparation skill in order; container deployability
> (`make-python-recipe-deployable`) is a separate required step for `contrib/`.

## How to invoke a skill

Say the skill's name or its trigger phrase to your assistant:

```text
"prepare the python recipe contrib/python/my-recipe"
"align pyproject.toml for my recipe"
"generate manifest.yaml for contrib/python/my-recipe"
```

The assistant loads the skill on demand and runs it.

## Ask the repo oracle

### `repo-oracle`

Answers questions about how this repo works — CI and workflow behavior,
repository size and file limits, who reviews which paths, what the bots do, and
how a recipe is prepared and validated. It reads the repo and cites the
file it answered from.

**Read-only.** It never edits, commits, comments, or does the work
itself. Ask it to prepare your recipe and you get the steps plus the
name of the skill that does it — never the work.

- **When to use:** you have a question about the repo rather than a
  task to run — including "which skill do I need?"
- **Not for:** your failing check, your working tree, or your branch.
  It cannot see your machine.
- **Trigger:** "oracle, what's the file limit for a contrib recipe?",
  "oracle, who reviews core/go?", "ask the oracle how to add a
  language".

## Universal skills

Apply regardless of language.

### `generate-manifest`

Reads your recipe's files, infers what belongs in `manifest.yaml`,
and writes a valid manifest. The [manifest](./manifest.md) page
describes every field.

- **Input:** recipe path.
- **Writes:** `manifest.yaml`.
- **When to use:** starting a new recipe, or an existing recipe
  is missing its manifest.
- **Trigger:** "generate manifest for contrib/python/my-recipe".

## Python skills

Apply to recipes under `contrib/python/`, `core/python/`, or Python `plugins/`.

### `prepare-python-recipe`

> **Start here.** The end-to-end orchestrator — if you only run
> one preparation skill, run this one.

Runs eight phases in order:

1. `generate-manifest`
2. `extract-python-environment-variables`
3. `align-recipe-pyproject`
4. `ruff format` and `ruff check`
5. `uv lock`
6. `generate-python-runnability-test`
7. `py_compile` and `pytest` verification of the generated test file
8. `uv run validate manifest` and `uv run validate structure`

Interactive — pauses at fixed decision points, or when it needs a
decision from you.

- **Input:** recipe path (must be at final target path).
- **When to use:** fastest path to a PR-ready recipe.
- **Safe to re-run:** won't overwrite `.env.example` or hand-written
  Python code.
- **Trigger:** "prepare the python recipe contrib/python/my-recipe".

### `scaffold-python-recipe`

Your starting point for a brand-new Python recipe. Copies the
compliant directory layout from `resources/templates/` and
resolves basic placeholders so the recipe is CI-ready before
you've written a line of agent code.

- **Input:** target path (e.g. `contrib/python/my-recipe`).
- **Writes:** full recipe skeleton — `pyproject.toml`, `app/`,
  `README.md`, `tests/test_runnability.py`, `.env.example`,
  `manifest.yaml`.
- **When to use:** starting a new Python recipe from scratch.
- **Trigger:** "scaffold a new Python recipe at contrib/python/my-recipe".

### `align-recipe-pyproject`

Enforces the repo's `pyproject.toml` conventions (see the
[pyproject.toml rules, in the Python page](./languages/python.md#pyprojecttoml)).
Uses a comment-preserving TOML editor so your own comments and
formatting survive.

- **Input:** recipe path.
- **Writes:** `pyproject.toml` (and optionally `manifest.yaml`).
- **Modes:** `--dry-run` reports what needs alignment; apply mode
  rewrites.
- **When to use:** cleaning up an existing recipe before PR.
- **Trigger:** "align pyproject.toml for contrib/python/my-recipe".

### `extract-python-environment-variables`

Finds every environment variable your code reads and makes sure
`.env.example` declares it. Also swaps hardcoded model names for
`os.getenv(...)` calls so the recipe stays configurable.

- **Input:** recipe path.
- **Writes:** `.env.example`, `app/__init__.py`, and any `.py`
  file with hardcoded model literals.
- **Safety:** never overwrites user-authored `.env.example`
  lines. Never writes new `os.environ.setdefault(...)` calls
  into Python files.
- **When to use:** adding a new env var, migrating hardcoded
  model names, or preparing an existing recipe.
- **Trigger:** "extract env vars for contrib/python/my-recipe".

### `generate-python-runnability-test`

Writes the required smoke test: imports your agent and asserts
`root_agent is not None`. Auto-detects which import-time side
effects (like `vertexai.init`) need mocking so the test runs
without credentials.

- **Input:** recipe path.
- **Writes:** `tests/test_runnability.py`.
- **Modes:** dry-run (preview) and apply.
- **When to use:** adding the required runnability test.
- **Trigger:** "generate runnability test for contrib/python/my-recipe".

### `make-python-recipe-deployable`

Turns a working recipe into a **deployable** one — packageable into
a container and runnable as a service. Generates the serving files
(`Dockerfile`, `.dockerignore`, `fast_api_app.py`,
`app_utils/{a2a,services,reasoning_engine_adapter}.py`,
`agents-cli-manifest.yaml`) and configures the recipe to match.

Not part of `prepare-python-recipe`. Required for every recipe under
`contrib/`, which must ship a `Dockerfile` and set `deployable: true`;
optional under `core/` and `plugins/`.

- **Input:** recipe path. Optional `--data-dirs`, `--region`,
  `--overwrite`, `--verify-container`.
- **Writes:** the serving files, plus `pyproject.toml` (serving
  deps, hatch wheel package), `agent.py` (the `App` object), and
  `manifest.yaml` (`deployable: true`).
- **Modes:** dry-run reports; `--apply` writes.
- **Stops rather than guessing** when the recipe needs an ADK major
  migration, or carries the old `app_utils` generation
  (`telemetry.py` / `typing.py` / `deploy.py`).
- **Verifies its own output** when docker is available: builds the
  generated Dockerfile, runs it, and probes `/list-apps` and the A2A
  agent card. A container that will not come up **blocks**
  `manifest.deployable`. The skill asks before doing this, and skips
  cleanly when there is no container runtime — the common case, and
  not a failure.
- **Six outcomes,** crossing whether the recipe needs provisioned
  infrastructure with whether a container actually proved it:
  `deployable-verified`, `deployable-unverified`,
  `containerized-verified`, `containerized-unverified`,
  `verification-failed`, `blocked`. Both `containerized-*` and
  `verification-failed` leave `manifest.deployable` unset on purpose,
  so a reader can always tell a proven result from an assumed one.
- **Does not** deploy or write terraform. It builds an image only to
  check its own work and then deletes it; publishing belongs to Cloud
  Build and Artifact Registry.
- **Trigger:** "make contrib/python/my-recipe deployable".

## Go / Java / Kotlin / TypeScript skills

None yet.

## Skills load automatically

- **Source:** `.agents/skills/<skill-name>/SKILL.md`
- **Loaded:** automatically by your AI assistant when you open
  this repo.

---

← [Docs home](../README.md) · [Checklist](../recipe-checklist.md) · [Handbook](./README.md)
