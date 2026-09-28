<!-- word count: 552 (target 700, cap 1000) -->

# Python Recipes

Everything Python-specific in one place. Universal requirements
live in [anatomy](../anatomy.md).

## Package layout

Every Python recipe uses this shape:

```
contrib/python/my-recipe/
  pyproject.toml            # project spec + deps
  uv.lock                   # pinned deps
  .env.example              # env vars the recipe reads
  manifest.yaml             # recipe metadata (see anatomy)
  README.md                 # description, setup, run
  app/
    __init__.py             # runs load_dotenv(), then imports agent
    agent.py                # your agent code
  tests/
    test_runnability.py     # import smoke test
```

**Best practice: name the Python package `app`.** Not strictly
enforced, but the root Ruff/isort configuration assumes it
(`known-first-party = ["app"]`) — other names produce wrong
import ordering.

**Minimum Python:** 3.11.

## The fast path

**New recipe:**

```
"scaffold a new Python recipe at contrib/python/my-recipe"
# ... write your agent in app/agent.py ...
"prepare the python recipe contrib/python/my-recipe"
```

**Updating a recipe:**

```
"prepare the python recipe contrib/python/my-recipe"
```

`prepare-python-recipe` is safe to re-run and won't overwrite
your `.env.example` or hand-written Python code.

Full skill reference:
[skills catalog](../skills-catalog.md#python-skills).

> **Deprecated models:** `gemini-2.0-flash` and `gemini-2.5-flash` are no
> longer accepted. Use `gemini-3.5-flash`.

## Copy-paste starters

The `scaffold-python-recipe` skill creates these for you. Use
them by hand if you're setting up a recipe manually.

### `pyproject.toml`

```toml
[project]
name = "my-recipe"           # see "Project name" below
version = "0.1.0"
requires-python = ">=3.11"
dependencies = [
    "google-adk",
    "python-dotenv>=1.0.0",
]

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"
```

**Project name.** `[project].name` is derived from where the
recipe lives:

| Location | `[project].name` | Example |
| --- | --- | --- |
| `core/<language>/<recipe>` | the folder name | `deep-search` |
| `contrib/<language>/<recipe>` | the folder name | `financial-advisor` |
| `plugins/<vertical>/<solution>` | `<vertical>-<solution>` | `retail-product-search` |

Plugins join the vertical in because the folder name
alone is not unique across verticals — `plugins/retail/product-search`
and `plugins/grocery/product-search` would otherwise both claim the
distribution name `product-search`. It also keeps the package name
aligned with the plugin's `SKILL.md` `name:` field.

Optional: add `description` if you want it in your `pyproject.toml`
— it must match `manifest.description` exactly. Do NOT add
`[tool.ruff]` or a local `ruff.toml` (Ruff config is
centralised at the repo root).

### `.env.example`

```
GOOGLE_CLOUD_PROJECT=your-project-id
GOOGLE_CLOUD_LOCATION=us-central1
MODEL_NAME=gemini-3.5-flash
```

Every env var your code reads must appear here. Read model
names from `MODEL_NAME` — never hardcode a model literal in
source. Deprecated: `gemini-2.0-flash`, `gemini-2.5-flash`.

### `app/__init__.py`

```python
from dotenv import load_dotenv

load_dotenv()

from . import agent  # noqa: E402
```

Call `load_dotenv()` here, not in `agent.py`. `__init__.py`
runs first when the package is imported, so `.env` loads
before any env-var reads at agent-import time. The
`# noqa: E402` tells Ruff the late import is intentional.

### `tests/test_runnability.py`

```python
"""Runnability tests for the recipe."""


def test_agent_runnability() -> None:
    """Verify agent.py imports and defines root_agent."""
    import app.agent

    assert app.agent.root_agent is not None
```

This is the bare minimum; you may need to tweak or extend it
to make it work for your recipe. The core idea is to import
`app.agent` and check whether `root_agent` is `None`.

## Integration tests

Tests that hit real external resources (Gemini, BigQuery,
third-party APIs). CI skips them for speed and credentials —
run them locally before opening a PR.

**Excluded from CI:**

| Pattern | Excluded |
|---|---|
| `tests/integration/` | Everything under this directory |
| `**/test_integration.py` | Files with this exact name, any depth |

**Adding one:** create a file matching either pattern, write
it with real API calls, document credential setup in the
recipe's `README.md`. Run locally:

```bash
cd contrib/python/my-recipe
uv run pytest tests/integration    # integration only
uv run pytest                      # full suite
```

## Local commands

```bash
# Structural checks (all validators at once)
uv run validate contrib/python/my-recipe
# Or individual: manifest / structure / readme
uv run validate readme contrib/python/my-recipe

# Format and lint
uv run ruff format contrib/python/my-recipe
uv run ruff check contrib/python/my-recipe

# Tests (mirrors CI — integration excluded)
cd contrib/python/my-recipe
uv run pytest --ignore=tests/integration \
  --ignore-glob="**/test_integration.py"
```

---

← [Checklist](../../recipe-checklist.md) · [Handbook](../README.md)
