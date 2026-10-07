# Copilot instructions for `google/adk-recipes`

## Repository shape

This repository is a collection of runnable Agent Development Kit (ADK)
recipes, not one application. Recipes are organized by lifecycle and
audience:

- `core/` contains canonical, focused patterns curated by the `agents-cli`
  team.
- `contrib/` contains community recipes and is the normal destination for new
  contributions.
- `plugins/<vertical>/<solution>/` contains vertical solutions shipped to users.
  The vertical directory is required; do not flatten this path.

The old language roots (`python/agents`, `java/agents`, `go/agents`,
`kotlin/agents`, and `typescript/agents`) are retired/frozen. New or moved
recipes belong under `core/`, `contrib/`, or `plugins/`. Every active recipe
has a `manifest.yaml` and `README.md`; the manifest declares its language,
status, type, description, and real ownership team/point of contact.

Repo skills in `.agents/skills/` are tooling for building and maintaining the
repository. They are distinct from vertical solutions under `plugins/`; do not
mix changes to repo skills with recipe changes in one change set.

## Local setup and validation

The repository's own tooling is a Python project. Use Python 3.11+ and `uv`,
not `pip`:

```bash
uv sync --dev
uv run pytest
```

The root test suite covers `tools/`, `.agents/skills/`, and
`.github/scripts/`. Run one tooling test with a pytest node selector, for
example:

```bash
uv run pytest tools/tests/test_validate_manifest.py::test_validate_valid_manifest
```

Recipe structure, manifest, README, and placement checks are exposed through
the installed `validate` command:

```bash
uv run validate <recipe-path>
uv run validate manifest <recipe-path>
uv run validate structure <recipe-path>
uv run validate readme <recipe-path>
uv run validate placement
```

For a Python recipe, run commands from the recipe root after installing its
dependencies:

```bash
cd contrib/python/<recipe>
uv sync --dev
uv run pytest --ignore=tests/integration \
  --ignore-glob="**/test_integration.py"
```

Run an individual test with `uv run pytest path/to/test.py::test_name`.
Integration tests are intentionally excluded from CI; run them explicitly
when the recipe README documents the required credentials and services.

## Formatting and linting

Python style is centralized in the root `pyproject.toml`: Ruff uses an
80-character line length, double quotes, and the configured E/F/I/C/PL/B/UP/RUF
rules. Do not add a recipe-local `ruff.toml`, `.ruff.toml`, or `[tool.ruff]`
configuration.

```bash
uv run ruff format <path>
uv run ruff check <path>
uv run ruff check --fix <path>
```

Go formatting and linting use the root `.golangci.yml`; run from the Go module
directory:

```bash
gofmt -w <changed-files>
golangci-lint fmt
golangci-lint run ./...
```

Java formatting uses the pinned `google-java-format` workflow tool. Kotlin
formatting uses pinned `ktlint` with its official style unless a root
`.editorconfig` changes it. TypeScript/JavaScript formatting and linting use
the root `biome.json`/`biome.jsonc` configuration:

```bash
npx @biomejs/biome format --write <changed-files>
npx @biomejs/biome lint --write <changed-files>
```

## Language-specific test commands

CI detects affected recipes from `manifest.yaml` and runs each recipe in its
own directory. Use the same commands locally:

- Go: `go test -v ./...`; one test: `go test -run TestName ./...`.
- Java: `mvn test -B` or `./gradlew test`; one test:
  `mvn -Dtest=TestName test` or `./gradlew test --tests '*TestName'`.
- Kotlin: `./gradlew test` or `mvn test -B`, depending on the recipe build
  files.
- TypeScript: install and test with the lockfile's package manager
  (`npm`, `pnpm`, `yarn`, or `bun`) and run the recipe's `test` script.
  There is no repository-wide TypeScript test-runner contract yet; use the
  script declared by that recipe's `package.json`.

The CI workflows copy `.env.example` to `.env` when needed. Do not commit
real credentials or rely on an untracked `.env` being present for tests.

## Recipe conventions

- Use the term **recipe**, not sample, in new documentation, comments, and
  messages.
- New recipes use lowercase hyphenated names (maximum 30 characters), include
  setup and run instructions in `README.md`, and stay within the `contrib/`
  size limits (70 files and 2 MB).
- Python recipes normally use `app/` as the package, with
  `app/__init__.py` calling `load_dotenv()` before importing `agent.py`.
  Include `pyproject.toml`, `uv.lock`, `.env.example`, `manifest.yaml`, and
  `tests/test_runnability.py`; the smoke test must import the agent and
  assert `root_agent` is not `None`.
- Python model names and other configuration values come from environment
  variables, not hardcoded model literals. Keep every environment variable
  read by the recipe in `.env.example`.
- `manifest.yaml` is the source of truth for recipe metadata and is validated
  against `.github/schemas/manifest-schema.json`. Use the repository
  validators rather than ad hoc manifest checks.
- Root Ruff/isort configuration treats `app` and
  `__AGENT_PACKAGE__` as first-party. Preserve that convention in serving
  templates and rendered Python recipes.
- Recipe tests are isolated per recipe by pytest's importlib mode; avoid
  changing the root test paths or collecting scaffold template tests under
  `.agents/skills/**/resources/templates/`.
- Read the target recipe's README and any local `AGENTS.md` before editing.
  `core/` recipes may have an `AGENTS.md` with reuse and architecture notes;
  those instructions apply to that recipe.

## Pull-request scope

Keep changes scoped to the requested recipe or repository subsystem. If a
repository-wide validator, formatter, or skill touches unrelated recipes,
restore those unrelated changes rather than bundling them. For a new
contributor recipe, follow `docs/recipe-checklist.md` and the deeper
`docs/recipe-handbook/` guidance, and run the affected language workflow's
local equivalent before opening a pull request.
