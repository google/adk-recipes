# Tools

Local developer tools for validating recipes before submitting a PR.

## Setup

Run once from the **repo root** to install dependencies and register the `validate` command:

```bash
uv sync
```

## Usage

Both arguments are optional:

```bash
uv run validate [subcommand] [scope]
```

| Argument | Description |
|---|---|
| `subcommand` | Optional. Which check(s) to run: `manifest`, `structure`, `readme`, `placement`, or `all` (default: `all`). |
| `scope` | Optional. `all` (default), `core`, `contrib`, `plugins`, or a path to a single recipe (e.g. `core/python/rag-agent-search`). |

```bash
uv run validate                                         # run all checks on all recipes
uv run validate all core                                # run all checks on core/ only
uv run validate manifest core/python/rag-agent-search   # run manifest check on one recipe
```

## Subcommands

### `manifest` — validate manifest.yaml

Checks that a recipe directory has a `manifest.yaml` file (or `plugin.json` for spec-compliant plugins) and that it conforms to `.github/schemas/manifest-schema.json` / `.github/schemas/plugin-schema.json`.

### `structure` — validate folder structure and required files

Checks recipe folder naming, size and file count limits, and required files/directories per root and language (from `.github/policy.yml`).

### `readme` — validate README.md

Checks `README.md` for valid UTF-8, minimum word count, no `TODO:` placeholders, a setup section, and a run section with a fenced code block.

### `placement` — validate recipe placement

Checks that every recipe sits at a valid root and depth (`core/<lang>/<name>`, `contrib/<lang>/<name>`, `plugins/<plugin>/plugin.json`, or legacy `plugins/<vertical>/<solution>`).

### `all` — run all checks

Runs every available check in sequence and prints a combined summary.

---

> Adding a new tool? Create a new `validate_<name>.py` file in this directory with a `main(scope: str | None) -> int` function, then register it in `validate.py` under `SUBCOMMANDS`. The `scope` argument follows the same convention as existing validators: `None`/`"all"` for everything, `"core"`, `"contrib"`, or `"plugins"` for a single root, or `"core/<lang>/<recipe>"` for a single recipe.
