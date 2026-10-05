---
name: generate-python-runnability-test
description: >
  Generates a lightweight `tests/test_runnability.py` for a Python recipe.
  The test just imports the recipe's agent module and asserts that
  `root_agent is not None` (and `app is not None` if the module defines
  one). The skill parses agent.py with `ast` to figure out which import-time
  side effects need mocking (`vertexai.init`, `google.auth.default`) and
  which env vars need setting (`GOOGLE_CLOUD_PROJECT`, `INTEGRATION_TEST`),
  and only emits the boilerplate the recipe actually needs. Runs in
  dry-run (report + preview) and apply (write to disk) modes. Use when the
  user wants to "add a runnability test", "generate test_runnability.py",
  "create a smoke test for the recipe", or fix the missing-required-file
  failure from `python-validate-recipe.yml`.
metadata:
  author: Google
  license: Apache-2.0
  version: 1.1.0
---

# Generate Python Runnability Test

Use this skill to create the `tests/test_runnability.py` file that every Python recipe under `core/python/`, `contrib/python/`, or `plugins/<vertical>/<solution>/` must ship (see `python-validate-recipe.yml` Check 4). The generated test is deliberately minimal — it just verifies the agent module imports and defines the expected globals. Business-logic testing lives elsewhere.

---

## What This Skill Does

Runs `scripts/generate_runnability_test.py` against a recipe directory. Steps:

1. **Locate `agent.py`.** Walks the recipe safely (excludes `.venv`, `venv`, `env`, `build`, `dist`, `__pycache__`, `node_modules`, `tests`, `*.egg-info`, and dot-directories) and picks the shallowest `agent.py` match. If none is found, errors out and suggests `--agent-file`. Only one file is generated per invocation.

2. **Parse `agent.py` AND every ancestor package `__init__.py` with `ast`** to detect:
   - **Top-level assignments** (agent.py only, per convention) — is `root_agent = ...` present? Is `app = ...` present?
   - **Import-time calls** (agent.py + every ancestor `__init__.py`) — does `vertexai.init(...)` fire at module load? Does `google.auth.default()`? Every ancestor `__init__.py` is checked because Python runs each of them in order when the test does `import a.b.agent` (`a/__init__.py`, then `a/b/__init__.py`, then the module), so a side effect in any of them matters as much as one in `agent.py`. Historical bug closed by this: cross-session-memory has `_, project_id = google.auth.default()` in `__init__.py`; before, the scanner missed it and the generated test crashed in CI without ADC. Detection uses `ast.walk` (any depth), so it is intentionally broad — a call nested in a function body still flags the recipe; the resulting patch is a harmless no-op if it never fires at import time, whereas a missed import-time call would crash the generated test.
   - **Env-var access** (agent.py + every ancestor `__init__.py`) — does the code *read* `GOOGLE_CLOUD_PROJECT` via `os.getenv` / `os.environ.get` / `os.environ["…"]`? Only reads count: an `os.environ["…"] = value` write means the recipe sets its own value and doesn't depend on the test providing one, so it's ignored.

3. **Scan all source `.py` files** in the recipe directory tree (same safe walker) for `INTEGRATION_TEST` env-var reads. This is a per-package convention: `agent.py` often calls a helper (e.g. `retrievers.create_search_tool`) at module load, and THAT helper — which may live anywhere in the recipe tree, not necessarily beside `agent.py` — is what reads `INTEGRATION_TEST`. Restricting the scan to `agent.py` would miss it.

4. **Emit the test.** Two shapes:
   - **Minimal** (no side effects detected) — module-level `import <module>` + `assert root_agent is not None` (and `app is not None` if present).
   - **Guarded** (any side effect detected) — env-var `setdefault` calls at the top of the test function; a `with patch(...):` block around the import listing every patch needed (`patch("vertexai.init")` when vertexai is used, `patch("google.auth.default", return_value=(MagicMock(), "test-project"))` when the recipe touches `google.auth`); and assertions **outside** the `with` block (the patches are only needed during import; keeping them active around assertions would be misleading).

   The `google.auth.default` patch is what makes the test survive a recipe that calls `google.auth.default()` unconditionally at import time (like cross-session-memory's `__init__.py`). Just setting `GOOGLE_CLOUD_PROJECT` isn't enough for that pattern — the call still fires and still needs valid ADC — hence the patch.

   Emission is post-processed through `ruff format` when available, so multi-patch `with (...):` blocks come out already wrapped per the repo's ruff config.

5. **Check that the import can actually resolve.** The generated test does `import <module_name>`, which only works if the RECIPE ROOT is on `sys.path`. That is not automatic — under pytest's default `prepend` import mode only the test file's own directory (`<recipe>/tests`) is inserted. The recipe root gets there in one of these ways, reported as `import_support`:

   | `import_support` | Meaning |
   |---|---|
   | `installable` | `pyproject.toml` declares a `[build-system]`, so `uv sync` installs the project and the package is importable. |
   | `pythonpath-ini` | `[tool.pytest.ini_options].pythonpath` includes `"."`. |
   | `existing-conftest` | A `conftest.py` sits at the recipe root (sufficient whatever it contains — pytest puts each conftest's own directory on `sys.path`, and for that one it IS the recipe root), or a `tests/conftest.py` that demonstrably extends `sys.path`. |
   | `generated-conftest` | None of the above held, so the skill wrote `tests/conftest.py` with a path shim. |

   A `tests/conftest.py` is judged by AST, not text search: it counts only if it really touches `sys.path`, so a comment or docstring that merely *mentions* `sys.path` cannot wrongly certify it.

   **Historical bug closed by this:** the recipe root being importable was assumed rather than checked. A recipe with no `[build-system]` — common for vertical plugins under `plugins/`, where code lives in a plain `scripts/` directory rather than an installed package — got a test that always died with `ModuleNotFoundError`, while `prepare-python-recipe`'s `py_compile` verification still reported success.

   Adding a `[build-system]` is the better fix; the generated conftest says so and tells the maintainer to delete it once they do.

6. **Write it** to `<recipe-dir>/tests/test_runnability.py` (creating `tests/` if needed), plus `tests/conftest.py` when step 5 called for it. Refuses to clobber an existing file unless `--overwrite` is passed.

### Edit safety

- No files outside the target recipe directory are read (beyond the recipe's own `.py` files) or written.
- Existing `tests/test_runnability.py` is never silently overwritten. The user must explicitly opt in with `--overwrite`.
- An existing `tests/conftest.py` is **never** overwritten, not even with `--overwrite` — the skill reports `conftest_action: skipped` and warns instead. A conftest it did not write may do something clobbering would break.
- `tests/` directory is created if missing (`mkdir -p` equivalent). No other directory or file is added.
- Ruff-clean by construction — the generated file passes `ruff check` and `ruff format --check` under the root config.

---

## Rules for the Agent

1. **Always use the script — never hand-write `tests/test_runnability.py` yourself.** The skill exists to keep the boilerplate consistent across recipes.

2. **Ask for the recipe directory** if the user hasn't given one. Recipe roots live under `core/python/<name>/`, `contrib/python/<name>/`, or `plugins/<vertical>/<solution>/`.

3. **Always start with `--dry-run`** unless the user has explicitly said "apply", "generate it", "just do it", or equivalent. Show them what would land before writing.

4. **Report only what matters.** Render a compact 3-column Markdown table (Rule / Status / Details) summarising the action plus the detections. Do NOT dump the raw JSON or the raw generated Python. Include the generated file's content as a fenced code block below the table so the user can review before deciding.

5. **If action is `refused_overwrite`**, tell the user the file already exists and offer to re-run with `--overwrite`. Don't do it silently.

6. **If action is `error`**, surface the message verbatim and stop. Common cases: no `agent.py` found (suggest `--agent-file`), parse error in `agent.py`.

7. **Offer to apply** after a dry-run. Do NOT paste the raw command as a copy-and-paste snippet for the user; ask something like "Want me to write this file?" and if they agree, run apply yourself.

8. **After apply mode succeeds**, remind the user to run the test locally to confirm it passes:

   ```
   cd <RECIPE_DIR> && uv run pytest tests/test_runnability.py -v
   ```

9. **Do not commit any changes.** Show the diff or file contents; let the user commit.

---

## Input

| Field | Required | Description |
|---|---|---|
| `--recipe-dir` | Yes | Path to the recipe root (e.g. `core/python/cross-session-memory`, `contrib/python/my-recipe`, `plugins/retail/store-ops`). |
| `--dry-run` | No | Print the JSON report (with the generated content in `test_content`) without writing any file. |
| `--overwrite` | No | Overwrite an existing `tests/test_runnability.py`. Default: refuse and exit 1. |
| `--agent-file` | No | Override auto-detection of the entry-point file. Path is relative to `--recipe-dir` (or absolute). Use when the recipe uses a non-standard layout (rare — <2% of recipes). |

---

## Run

### Dry-run (start here)

```bash
uv run --no-project python3 .agents/skills/generate-python-runnability-test/scripts/generate_runnability_test.py \
  --recipe-dir <RECIPE_DIR> --dry-run
```

Output on stdout: JSON with `agent_file`, `module_name`, `detections`, `import_support`, `conftest_path`, `conftest_action` (`would_write` / `wrote` / `skipped` / `null`), `test_content`, `action` (`would_write` / `refused_overwrite` / `error`), `message`, and `warnings` (a list — surface every entry to the user). Exit code `0`.

Note: no `--with` flags are needed — the script only uses Python's stdlib (`ast`, `argparse`, `json`, `pathlib`, `dataclasses`, `os`, `sys`, `subprocess`, `textwrap`). `uv run --no-project python3` is used (rather than a bare `python3`) to guarantee a modern managed interpreter, consistent with the other Python recipe skills; the system `python3` on macOS can still be an old version. Dry-runs remain cheap and side-effect-free.

### Apply

```bash
uv run --no-project python3 .agents/skills/generate-python-runnability-test/scripts/generate_runnability_test.py \
  --recipe-dir <RECIPE_DIR>
```

Writes `<RECIPE_DIR>/tests/test_runnability.py`. Refuses if the file exists (exit `1`).

### Apply with overwrite

```bash
uv run --no-project python3 .agents/skills/generate-python-runnability-test/scripts/generate_runnability_test.py \
  --recipe-dir <RECIPE_DIR> --overwrite
```

### Override the entry-point file (rare)

```bash
uv run --no-project python3 .agents/skills/generate-python-runnability-test/scripts/generate_runnability_test.py \
  --recipe-dir <RECIPE_DIR> --agent-file some/other/entry.py --dry-run
```

Path is relative to `--recipe-dir` or absolute. The generated import uses the module path derived from the relative location (e.g. `some/other/entry.py` → `import some.other.entry`).

---

## Respond

Do not dump raw JSON. Render a compact table summarising the action and the detections, then include the generated file's content as a fenced code block below.

### Table shape

| Rule | Status | Details |
|---|---|---|

- **Rule** — a short label for what's being reported: `agent-file`, `module`, `detections`, `write`. Use backticks for clarity.
- **Status** — `ok` / `would_write` / `wrote` / `refused_overwrite` / `error`. No emoji unless the user has asked.
- **Details** — compact prose. For `detections`, list only what was found (e.g. "root_agent, app, needs vertexai patch + GCP project env + INTEGRATION_TEST env"). Don't list what was NOT found.

### After the table

- Show the generated `test_content` as a fenced Python code block so the user can review it before deciding.

### Closing action

- **`would_write`** (dry-run) — **offer to apply** yourself. Do NOT paste the raw command. Ask "Want me to write this file?" If the user agrees, run the apply command yourself and render the resulting report as another compact confirmation. If they decline, stop.
- **`refused_overwrite`** — tell the user the file already exists and offer to re-run with `--overwrite`. Do NOT overwrite silently. If they agree, run with `--overwrite`.
- **`error`** — surface the message verbatim and stop. Do not attempt to work around it.
- **`wrote`** (apply) — end with:

  ```
  Next steps:
    cd <RECIPE_DIR> && uv run pytest tests/test_runnability.py -v
    git diff                                                          # review before committing
  ```

Then stop. Do not commit. Do not run any further tools. End your turn.
