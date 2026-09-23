---
name: extract-python-environment-variables
description: >
  Scans a Python recipe to find every place an environment variable is
  accessed — including `os.environ.setdefault("V", "d")`, whose "d" would
  otherwise be a hidden default a user editing .env.example has no way to
  discover — then ensures all variables are declared in `.env.example`,
  that `load_dotenv()` is bootstrapped in the package `__init__.py`, and
  that `python-dotenv>=1.0.0` is listed in `pyproject.toml`. When a new
  entry is added to `.env.example`, the extracted default from source is
  written as the value (with a `# extracted-by:extract-env-vars` marker
  and provenance comment); values that look like stubs
  (`"my-project-id"`, `"changeme"`, `"<...>"`) are downgraded to the TODO
  placeholder but the source string is preserved in the marker comment.
  Also detects hardcoded model-name string literals (e.g.
  `"gemini-3.5-flash"` in `agent.py`) and rewrites them to an
  `os.getenv(...)` call. The variable name is derived from the assignment
  target when it names a model (`DEFAULT_EMBEDDING_MODEL` →
  `EMBEDDING_MODEL`), else `MODEL_NAME` (single model) or
  `MODEL_NAME_GENERATED_1` / `MODEL_NAME_GENERATED_2`, … (multiple models).
  Normally no fallback default is written into the Python source; the one
  exception is when no `load_dotenv()` bootstrap could be installed (no
  package `__init__.py`), where the original literal is kept as the
  fallback so the lookup cannot evaluate to `None` at runtime. The
  model string is written as the value in `.env.example` with a comment
  prompting a rename.   When re-run against a recipe whose `.env.example` already has entries,
  the writer classifies each entry (skill-authored vs. user-authored,
  TODO vs. real value) and only rewrites lines it can prove it authored;
  user-authored lines are always preserved. A stale TODO (either
  skill-authored or a bare v1-era `<TODO: update-this-value>` with no
  inline comment) is upgraded in place when source can supply a real
  default.
  IMPORTANT — two hard rules the skill NEVER breaks:
  (1) USER-EDIT SAFETY. Any `.env.example` line the skill cannot prove
  it authored is USER_OWNED and is never modified. The rewriter fails
  closed on any structural ambiguity (quoted values, backslash
  continuation, duplicate declarations) — a stale TODO left in place is
  cheap; a clobbered user edit is not.
  (2) ADDITIVE-ONLY FOR PYTHON FILES. The skill never writes new
  `os.environ.setdefault(...)` bootstrap lines into any Python file.
  Pre-existing `os.environ.setdefault(...)` or
  `os.getenv("VAR", "default")` calls that the recipe author wrote by
  hand are LEFT UNTOUCHED — the skill's only writes to Python files are
  (a) the `load_dotenv()` bootstrap; (b) `# noqa: E402` on trailing
  relative imports that would otherwise trip Ruff; (c) hardcoded
  model-literal replacement.
  Use when the user wants to "extract env vars", "update .env.example",
  "add load_dotenv", "surface setdefault defaults", "upgrade stale TODOs
  in .env.example", "replace hardcoded model names", or "fix environment
  variables" in a Python recipe.
metadata:
  author: Google
  license: Apache-2.0
  version: 2.3.0
---

# Extract Python Environment Variables

Use this skill to ensure a Python recipe properly declares and loads all
environment variables it uses.

---

## What This Skill Does

Runs `scripts/extract_env_vars.py` against a recipe directory. The script:

1. **Scans** all `.py` files (excluding `tests/` and common cache/venv
   directories) for environment variable accesses:
   - `os.environ["VAR"]`
   - `os.environ.get("VAR")` / `os.environ.get("VAR", "default")`
   - `os.getenv("VAR")` / `os.getenv("VAR", "default")`
   - `os.environ.setdefault("VAR", "default")` &nbsp;&nbsp;*(v2)*

   Only names matching `^[A-Z_][A-Z0-9_]*$` (UPPER_SNAKE_CASE) are captured;
   a lowercase name like `os.getenv("my_api_key")` is **skipped** and a
   `[WARN]` line lists any that were dropped. Rename such vars to uppercase
   in source, or add them to `.env.example` by hand.

2. **Updates `.env.example`** — appends any variables not already
   declared AND upgrades stale TODO entries in place. Existing
   user-authored lines are always preserved (see Rule 1 below).
   - Creates `.env.example` from scratch if it does not exist.
   - Each new/upgraded line has the shape:
     ```
     VAR=<value>  # extracted-by:extract-env-vars; <provenance>
     ```
   - `<value>` is the **resolved default** extracted from source when the
     author committed to one — with `os.environ.setdefault(...)` beating
     `os.getenv("V", "d")` / `os.environ.get("V", "d")` (setdefault is a
     stronger commitment: it mutates the process environment, while getenv
     fallback is per-read). Alphabetical file order breaks ties.
   - Values that look like **stubs** are downgraded to
     `<TODO: update-this-value>` — e.g. `my-project-id`, `your-api-key`,
     `changeme`, `<...>`, anything containing `example.com`. The source
     string is preserved in the marker comment so the maintainer sees
     what was found and can fix the source too.
   - When no source supplied a string-literal default, the value is
     `<TODO: update-this-value>` with a `no default in source` note.
   - **Stale TODO upgrade (v2.1):** if an existing entry is either a
     skill-authored TODO (has the marker) or a bare v1-era TODO (exact
     value `<TODO: update-this-value>`, no marker, no inline comment)
     AND source can now supply a real default, the line is rewritten in
     place. This closes the discoverability loop for recipes originally
     processed by v1 (which always wrote TODOs regardless of what source
     said).

3. **Injects `load_dotenv()`** into the package `__init__.py` (the first
   subdirectory inside the recipe that contains an `__init__.py`, skipping
   `tests/` and hidden directories). The snippet injected is:

   ```python
   from dotenv import load_dotenv

   # Load variables from .env if present. In production the environment is
   # already populated by the platform (Cloud Run, GKE, etc.), so a missing
   # .env is expected and not an error.
   load_dotenv()
   ```

   If `load_dotenv` is already present the injection is skipped.

   **If no package `__init__.py` exists** (common in vertical skills under
   `skills/`, where the code lives in a plain `scripts/` directory rather
   than an importable package) the injection is skipped with a `[WARN]`,
   and the step reports that no bootstrap is in place. Step 4 below depends
   on that answer.

   **Additionally — always, regardless of whether we injected** — appends
   `# noqa: E402 -- must come after load_dotenv()` to any top-level relative
   import (`from .x import y`) that sits AFTER a non-import module-level
   statement. Two cases this covers:

   - **Fresh injection.** The injected `load_dotenv()` call pushes
     pre-existing trailing relative imports below a non-import statement, so
     they'd trigger Ruff `E402` ("module-level import not at top of file")
     when Phase 4 (ruff) of `prepare-python-recipe` runs.

   - **Author-written bootstrap.** The recipe author already wrote
     `load_dotenv()` + `os.environ.setdefault(...)` calls followed by a
     trailing `from . import agent`, but never marked the trailing import.
     The skill did NOT inject anything (load_dotenv was already present) but
     still adds the noqa suffix so the file is lint-clean on the pipeline's
     next ruff pass.

   The suppression pass is precise — a relative import at the very TOP of
   the file (before any non-import statement) is fine and left untouched.
   Idempotent: a line that already carries `# noqa: E402` is skipped.

4. **Replaces hardcoded model names** in source (e.g. `model="gemini-3.5-flash"`
   in `agent.py`) with an `os.getenv(...)` call.

   **Position decides eligibility.** A model string is only promoted when it
   is a *configurable constant*. Three positions mean it is something else,
   and are left untouched:

   - **Collection-literal entries** (dict keys and values, list/set/tuple
     items) — lookup tables and enumerations of supported models:

     ```python
     IMAGE_MODELS = {
         "flash": "gemini-2.5-flash-image",
         "pro":   "gemini-2.5-pro-image",
     }
     ```

     Rewriting the values collapses the table onto one env var; rewriting the
     **keys** is worse, because `IMAGE_MODELS.get("flash")` then never
     matches anything. A dict key must stay a static literal.
   - **Subscript indices** — `IMAGE_MODELS["gemini-3.1"]` is a key *into*
     such a table, so replacing it looks up a different entry.
   - **Comparison operands** — `if "gemini-3.1" in model_id` tests a value
     rather than configuring one.

   Skipping a legitimate extraction is cheap (lift it by hand); silently
   breaking a lookup table is not, so this errs towards skipping. Every
   skipped literal is listed in an `[INFO]` block, so nothing is hidden — and
   because these strings never reach the naming step, a dict target like
   `IMAGE_MODELS` can no longer leak into `.env.example` as a variable name.

   **Variable name.** The assignment target that holds the literal is used
   when it names a model, since it carries far more meaning than a generic
   fallback — `DEFAULT_EMBEDDING_MODEL = "gemini-embedding-001"` and
   `embedding_model = cfg.get("embedding_model", "gemini-embedding-001")`
   both yield `EMBEDDING_MODEL`. This matters in recipes that already read a
   *different* model var (say `GEMINI_MODEL` for the LLM), where a second
   bare `MODEL_NAME` would be actively misleading. When a string is assigned
   to conflicting target names, the most frequent wins, ties broken
   alphabetically. Otherwise:
   - A single unnamed model → `MODEL_NAME`
   - The rest → `MODEL_NAME_GENERATED_1`, `MODEL_NAME_GENERATED_2`, … (sorted
     alphabetically for determinism)

   **Fallback argument.** Normally the emitted call is **bare** —
   `os.getenv("EMBEDDING_MODEL")`, no default — because default values are
   the maintainer's decision, not the skill's. That is only safe when the
   `load_dotenv()` bootstrap from step 3 is in place to populate the
   environment.

   **When step 3 could not install a bootstrap** (no package `__init__.py`),
   nothing reads `.env`, so a bare lookup would evaluate to `None` at runtime
   and silently break the recipe. In that case the original literal is
   preserved as the fallback — `os.getenv("EMBEDDING_MODEL",
   "gemini-embedding-001")` — which keeps behaviour identical to before the
   rewrite while still lifting the value into the environment. The step logs
   an `[INFO]` line explaining the choice.

   The actual model string is written as the value in `.env.example` (e.g.
   `EMBEDDING_MODEL=gemini-embedding-001`) with a comment prompting the
   maintainer to rename the variable if the derived name isn't right.

5. **Updates `pyproject.toml`** — adds `python-dotenv>=1.0.0` to `[project]`
   dependencies if it is not already there.

---

## Hard rules the skill NEVER breaks

**Rule 1 — User-edit safety for `.env.example`.** The writer classifies
every existing entry before touching anything, and only rewrites lines
it can prove it authored. Concretely:

| Existing entry looks like | Source now provides | Action |
|---|---|---|
| Missing | any | **append** with resolved default (or TODO) |
| A line the skill wrote (has `# extracted-by:extract-env-vars` marker) with `<TODO: update-this-value>` value | a REAL default (not placeholder-shape) | **upgrade in place** |
| Same as above | no default OR a placeholder-shape default | **skip** (would be TODO → TODO churn) |
| Bare `VAR=<TODO: update-this-value>` line with **no marker AND no inline comment** — a v1-era TODO | a REAL default | **upgrade in place** (v1 → v2 migration) |
| A line the skill wrote with a REAL value | source has any value (even different) | **skip** (never overwrite skill-authored values on drift) |
| Anything else (user typed value, user-authored comment, non-standard formatting) | any | **skip — never touched** |

Golden rule underneath all of this: **fail closed**. If the classifier
can't confidently prove a line was skill-authored or is a bare v1 TODO,
it's `USER_OWNED` and untouchable. If the rewriter finds a line whose
structure it doesn't understand (quoted value, backslash continuation,
duplicate declarations), it refuses and warns rather than guessing. And
before any write, the assembled file is re-parsed to verify every
planned upgrade re-parses as a skill-authored real value — a bug in the
rewriter cannot silently corrupt `.env.example`.

To force regeneration of a line the skill would otherwise skip, delete
the line and re-run.

*Rationale for extracting defaults (v2) and upgrading stale TODOs
(v2.1): a value hidden inside a Python file that a user must know about
but has no reason to look at is, in practice, undocumented.
`.env.example` is the documented configuration surface. Surfacing what
the author already committed to in code — and updating a stale TODO
once source can supply a real default — makes the configuration
discoverable without inventing anything new. The value in source WAS
the value; we're only lifting it into view.*

**Rule 2 — Additive-only for Python files.** The skill never writes new
`os.environ.setdefault(...)` bootstrap lines into any Python file.
Pre-existing `os.environ.setdefault(...)` or
`os.getenv("VAR", "default")` calls that a recipe author wrote by hand
are LEFT UNTOUCHED. The skill's only writes to Python files are:
- Adding the `from dotenv import load_dotenv` + `load_dotenv()` snippet
  (once, only if not already present).
- Appending `# noqa: E402` to trailing relative imports that would
  otherwise trip Ruff after the env-bootstrap block.
- Replacing hardcoded model literals with `os.getenv(...)` calls — bare
  when a `load_dotenv()` bootstrap is in place, otherwise retaining the
  original literal as the fallback (see step 4 above).

Note that scanning `os.environ.setdefault(...)` and lifting its value
into `.env.example` (v2) does NOT violate Rule 2 — the skill READS from
Python source (unchanged behaviour) and WRITES only to `.env.example`.

---

## Rules for the Agent

1. **Always use the script** — never manually edit `.env.example`, `__init__.py`,
   or `pyproject.toml` to perform these changes.
2. **Ask for the recipe directory** if the user has not provided one. Do not
   assume a path.
3. **After the script succeeds**, remind the user to:
   - Copy `.env.example` → `.env` and fill in real values before running locally.
   - Run `uv sync` to pick up the `python-dotenv` dependency if it was newly added.

---

## Input

| Field | Required | Description |
|-------|----------|-------------|
| Recipe directory | Yes | Path to the recipe root (e.g. `contrib/python/my-recipe`, `core/python/my-recipe`, or `skills/retail/store-ops`) |

If the user has not specified the recipe directory, ask for it before proceeding.

---

## Run

Run it through `uv` so it always executes on a Python 3.11+ interpreter — the
script uses the stdlib `tomllib`, which only exists from 3.11 onward. A bare
`python3` that resolves to 3.9/3.10 fails with `ModuleNotFoundError: tomllib`.
No `--with` packages are needed (the script is stdlib-only).

```bash
uv run --no-project python3 \
  .agents/skills/extract-python-environment-variables/scripts/extract_env_vars.py \
  --recipe-dir <RECIPE_DIR>
```

### Preview first (optional)

Add `--dry-run` to report exactly what *would* change without modifying any
files. Nothing is written to `.env.example`, `__init__.py`, `pyproject.toml`, or
any source file. Useful for inspecting a recipe before committing to the edits:

```bash
uv run --no-project python3 \
  .agents/skills/extract-python-environment-variables/scripts/extract_env_vars.py \
  --recipe-dir <RECIPE_DIR> --dry-run
```

In dry-run output, actions are prefixed with `[DRY-RUN]` and phrased as
"Would add" / "Would inject" / "Would replace".

---

## Respond

Do not show the script's raw stdout. Reformat its results into clear Markdown
tables (variables to add, files to update, model replacements) so they are easy
to read — this is especially important for `--dry-run` output. For each variable
or model string, include a column with the source file where it was found
(locate it in the recipe's Python source; ignore `.env` and `.env.example`).

For added variables, surface the **resolved default** and **its provenance**
so the maintainer knows which value the skill chose and where it came from:

| Variable | Value written to .env.example | Source |
|----------|-------------------------------|--------|
| `GOOGLE_CLOUD_PROJECT` | `<TODO: update-this-value>` *(downgraded — source had `"my-project-id"`)* | `agent.py` (setdefault) |
| `GOOGLE_GENAI_USE_VERTEXAI` | `TRUE` | `__init__.py` (setdefault) |
| `LOG_LEVEL` | `INFO` | `logging_setup.py` (getenv fallback) |
| `API_KEY` | `<TODO: update-this-value>` *(no default in source)* | `client.py` (getenv, no fallback) |

If the script reports **upgrades** (stale TODO entries rewritten in
place with real defaults from source), surface those in a separate table
so the maintainer can eyeball each one:

| Variable | Was | Now | Source |
|----------|-----|-----|--------|
| `USE_VERTEX` | `<TODO: update-this-value>` | `TRUE` | `__init__.py` (setdefault) |

If the script logged `[WARN] Refused to upgrade …` for any variable,
surface that too — the line's structure was ambiguous and was left
untouched deliberately.

Once the script finishes successfully, summarise what changed:

- Which variables were **added** to `.env.example` (with resolved value +
  source) or confirm it was already up to date.
- Which existing TODOs were **upgraded** in place (v2.1) with real
  defaults from source.
- Any values that were **downgraded to `<TODO>` because they looked like
  placeholders** — the maintainer should fix the source too.
- Whether `load_dotenv()` was injected or was already present.
- Whether `python-dotenv` was added to `pyproject.toml` or was already there.

Then remind the user of the next steps:

```
Next steps:
  cp <RECIPE_DIR>/.env.example <RECIPE_DIR>/.env   # then fill in real values
  cd <RECIPE_DIR> && uv sync                        # install python-dotenv if newly added
```

Do not make any further changes. End your turn.
