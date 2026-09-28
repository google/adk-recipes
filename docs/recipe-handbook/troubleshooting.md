<!-- word count: 3305 (target 500+, no cap) -->

# Troubleshooting

A check failed on your recipe and you need to get it green. Find your error
on this page, follow the steps, and run the command at the end of the
section to confirm the fix worked before you push again.

Each command below says which directory to run it from. Replace
`<recipe-path>` with the path to your recipe — `core/python/my-recipe`,
`contrib/python/my-recipe`, or `skills/retail/my-skill`.

## Contents

### General

**manifest.yaml**
- [manifest.yaml is missing, or fails the schema](#manifestyaml-missing-or-invalid)
- [ownership.team or ownership.poc still holds scaffold text](#ownershipteam-or-poc-is-a-placeholder)

**README.md**
- [README.md is absent or empty](#readmemd-is-missing-or-empty)
- [README.md still contains TODO placeholders](#readmemd-contains-todo-placeholders)
- [README.md is under the 100-word minimum](#readmemd-is-too-short)
- [README.md has no setup heading](#readmemd-is-missing-a-setup-section)
- [README.md has no run heading, or no command to copy](#readmemd-is-missing-a-run-section-or-code-block)

**Files and folders**
- [The recipe folder name breaks the naming rule](#directory-name-too-long-or-invalid)
- [The recipe is too big, or has too many files](#recipe-exceeds-size-or-file-limit)
- [A file or directory the recipe must have is absent](#required-file-or-directory-missing)
- [The recipe sits at the wrong path](#recipe-is-in-the-wrong-folder)
- [The recipe lives in a folder that no longer accepts edits](#changes-inside-a-retired-folder)
- [Only repository admins may modify files under .github/](#only-repository-admins-may-modify-files-under-github)
- [The recipe contains a recipe-local lint or style configuration file](#standalone-lint-or-style-config-file)

**Containers (Dockerfile)**
- [Dockerfile failed to build](#dockerfile-build-failed)
- [Recipe container does not serve](#recipe-container-does-not-serve)

### Python

**pyproject.toml**
- [pyproject.toml configures Ruff locally](#pyprojecttoml-has-a-local-ruff-configuration)
- [The recipe has its own ruff.toml file](#standalone-ruff-config-file)
- [The project name is not the one the path requires](#project-name-doesnt-match-the-required-name)
- [The project description disagrees with the manifest](#project-description-doesnt-match-manifest)
- [requires-python admits Python older than 3.11](#requires-python-below-311)
- [pyproject.toml has no uv.lock beside it](#pyprojecttoml-has-no-sibling-uvlock)
- [pyproject.toml does not declare PyPI as its index](#missing-tooluvindex-block)

**Environment variables**
- [Your code reads a variable .env.example never declares](#env-var-missing-from-envexample)

**Dependencies (uv.lock)**
- [uv.lock no longer matches pyproject.toml](#uvlock-out-of-sync)
- [uv.lock points at a registry other than PyPI](#lockfile-references-a-non-pypi-url)
- [uv.lock installs a package straight from git](#vcs-dependency-in-uvlock)
- [uv.lock depends on a path that exists only on your machine](#local-path-dependency-in-uvlock)
- [uv.lock is missing sha256 hashes](#missing-package-hash-in-uvlock)

**Tests and code style**
- [The recipe has no runnability test](#runnability-test-missing)
- [Formatting or lint rules failed](#ruff-format-or-check-failed)

### Warnings and unknown failures

**Warnings that do not block your PR**
- [The recipe is flagged as unhealthy](#recipe-is-marked-inactive)
- [A core recipe is on an old ADK major](#core-recipe-is-behind-the-current-adk-major)
- [Every warning, and its one-line fix](#non-blocking-notices)

**Nothing here matches**
- [The failure is ours, not yours](#ci-infrastructure-failure)
- [The AI reviewer keeps finding new things](#the-ai-reviewer-keeps-finding-new-things)
- [Something else](#something-else)

---

## manifest.yaml missing or invalid

**Symptom** — `[manifest-missing] manifest.yaml is missing.`,
`[manifest-empty] manifest.yaml has no content — it is either empty or
contains only comments.`, or a schema error naming the failing field.

**Cause** — every recipe needs a `manifest.yaml` matching the
[schema](../../.github/schemas/manifest-schema.json).

**Fix**

- File absent, empty, or only comments: run `generate-manifest` (AI skill),
  or copy the [minimum example](./anatomy.md#manifestyaml) and edit it.
- File present but rejected: the error names the failing field. Every
  manifest needs `type`, `status`, `language`, `description`,
  `ownership.team` and `ownership.poc`.

**Confirm**, from the repo root — `uv run validate manifest <recipe-path>`

## ownership.team or poc is a placeholder

**Symptom** — `[ownership-placeholder] ownership.team is still the scaffold
placeholder`, or the same for `ownership.poc`.

**Cause** — the scaffold's placeholder text is still in `manifest.yaml`.

**Fix** — set `ownership.team` to a real team name and `ownership.poc` to a
real GitHub user ID.

**Confirm**, from the repo root — `uv run validate manifest <recipe-path>`

## Directory name too long or invalid

**Symptom** — `[folder-name] Folder name`

**Cause** — the folder name breaks the pattern `^[a-z][a-z-]*$`, or runs past
30 characters.

**Fix** — rename the folder: lowercase letters and hyphens only, starting
with a letter, 30 characters at most.

**Confirm**, from the repo root — `uv run validate structure <recipe-path>`

## Recipe exceeds size or file limit

**Symptom** — `Recipe folder is 3.4 MB; the limit is 2 MB.`, or
`Recipe folder contains 91 counted files; the limit is 70.`

**Cause** — the recipe passes its budget. Under `contrib/` that is 70 files
and 2 MB.

**Fix**

1. Read the counted paths in the error output.
2. Delete anything that should never have been committed — `.venv/`, IDE
   configuration, build output.
3. Move data files over 1 MB to a storage bucket and link them from
   `README.md`.
4. Convert screenshots to WebP:
   `cwebp -q 85 <recipe-path>/shot.png -o <recipe-path>/shot.webp`.

**Confirm**, from the repo root — `uv run validate structure <recipe-path>`

## Required file or directory missing

**Symptom** — `Required file '<name>' is missing` or
`Required directory '<name>/' is missing`

**Cause** — the required set is the union of every rule that applies to your
recipe. Language rules key off `manifest.language`, not the folder path: a
vertical skill at `skills/retail/product-search` picks up the Python list
because its manifest says `language: python`.

| Rule | Applies to | Entries |
| --- | --- | --- |
| `always` | every recipe | `README.md` |
| `by_root.core` | anything under `core/` | `AGENTS.md` |
| `by_root.skills` | anything under `skills/` | `SKILL.md`, `EVAL.yaml`, `scripts/` |
| `by_language.python` | `manifest.language: python` | `pyproject.toml`, `uv.lock`, `.env.example`, `tests/test_runnability.py` |

**Fix** — most missing entries have a generator:

| Missing | Fix |
| --- | --- |
| `manifest.yaml` | `generate-manifest` (AI skill) |
| `.env.example` | `extract-python-environment-variables` (AI skill) |
| `tests/test_runnability.py` | `generate-python-runnability-test` (AI skill) |
| `uv.lock` | `uv lock --project <recipe-path>` |
| `pyproject.toml` | `align-recipe-pyproject` (AI skill) |
| everything at once | `scaffold-python-recipe` (AI skill) |

Directory looks present but still reported missing? Git cannot commit an
empty directory. Commit a placeholder:

```bash
# from the repo root
touch <recipe-path>/scripts/.gitkeep
git add <recipe-path>/scripts/.gitkeep
```

**Confirm**, from the repo root — `uv run validate structure <recipe-path>`

## Recipe is in the wrong folder

**Symptom** — `sits directly under` or `is nested too deeply`

**Cause** — every recipe under `skills/` must sit at
`skills/<vertical>/<solution>/`. The vertical (`retail/`, `hr/`, `finance/`)
is mandatory.

```
skills/retail/product-search/manifest.yaml    valid
skills/product-search/manifest.yaml           too shallow — no vertical
skills/retail/product-search/x/manifest.yaml  too deep
```

**Fix**

1. Move the directory to the path named in the error.
2. Update `[project].name` in `pyproject.toml` — a vertical skill needs
   `<vertical>-<solution>`, not the folder basename. See
   [Project name doesn't match the required name](#project-name-doesnt-match-the-required-name).

**Confirm**, from the repo root — `uv run validate placement`

## Changes inside a retired folder

**Symptom** — `is in a retired folder, which no longer accepts changes.`

**Cause** — recipes used to live at `<language>/agents/<recipe>` in the repo
root. Those roots are closed.

**Fix**

1. Move the whole recipe to `contrib/<language>/<recipe>`. The error names
   the destination it expects.
2. Make your change there.
3. Bring the recipe up to current requirements, which the retired copy
   predates — see [the checklist](../recipe-checklist.md) and
   [Required file or directory missing](#required-file-or-directory-missing).

Deleting or renaming inside a retired folder is allowed, so moving a recipe
out is never blocked.

**Confirm**, from the repo root —
`uv run validate structure contrib/<language>/<recipe>`

## Only repository admins may modify files under .github/

**Symptom** — `[github-dir-admin-only] ... is under .github/, which can only be modified by repository administrators.`

**Cause** — files in the `.github/` directory (CI workflows, issue templates, automation scripts, and repository policy configuration) control repository-wide infrastructure and security. Only repository administrators are permitted to create, modify, or delete files under `.github/`.

**Fix** — restore `.github/` to exactly what is on `main`. The `git rm` line is
needed as well as the checkout: `git checkout` restores files that exist on
`main` but leaves behind any file your branch *added* under `.github/`, which
would keep the check failing.

    git rm -r --quiet --ignore-unmatch .github/
    git checkout origin/main -- .github/
    git commit -m "Revert changes under .github/"

If CI workflow or repository configuration changes are needed, please open an issue describing the requested changes or reach out to a repository administrator.

**Confirm**, from the repo root — this lists exactly the files CI would flag.
It deliberately passes `--is-admin false`, because the permission lookup needs
a token the check has in CI and you generally do not have locally:

    git -c core.quotePath=false diff --no-renames --name-only origin/main...HEAD \
      | uv run --no-project python tools/check_github_dir_changes.py \
          --author "$(git config user.name)" --is-admin false

## Standalone lint or style config file

**Symptom** — `Recipe contains a recipe-local Biome/golangci-lint/.editorconfig configuration file`

**Cause** — the recipe contains a recipe-local configuration file (`biome.json`, `biome.jsonc`, `.biomerc*`, `.golangci.yml`, `.golangci.yaml`, `.golangci.toml`, `.golangci.json`, or an `.editorconfig` that configures Kotlin style: a Kotlin section such as `[*.{kt,kts}]`, a `ktlint_*` or `ij_kotlin_*` property, or — in a Kotlin recipe — a property such as `max_line_length` in `[*]`). Style and lint configurations are centralized at the repository root. The check is advisory for now: it reports a warning and does not fail the PR.

**Fix** — delete the file from the recipe, or remove the section or property the warning names from `.editorconfig`. If repository-wide style or lint rules need updating, update the root configuration file.

**Confirm**, from the repo root —
`python3 .github/scripts/check_recipe_lint_config.py <recipe-path>`

## README.md is missing or empty

**Symptom** — `README.md is missing.`, `README.md is empty.`,
`README.md is not valid UTF-8`, or `README.md could not be read`.

**Cause** — every recipe needs a `README.md`, and CI reads it as UTF-8.

**Fix** — create the file, covering what the recipe does, how to set it up,
and how to run it. If the error names an encoding, re-save it as UTF-8.

**Confirm**, from the repo root — `uv run validate readme <recipe-path>`

## README.md contains TODO placeholders

**Symptom** — `README.md still contains 3 TODO: placeholder(s).`

**Cause** — scaffold text or draft notes survived into the PR.

**Fix** — replace every `TODO:` line with real content.

**Confirm**, from the repo root — `uv run validate readme <recipe-path>`

## README.md is too short

**Symptom** — `README.md is 47 words; the minimum is 100.`

**Cause** — the README is under 100 words.

**Fix** — add a real description, setup instructions and run steps. Aim for
200–300 words.

**Confirm**, from the repo root — `uv run validate readme <recipe-path>`

## README.md is missing a setup section

**Symptom** — `README.md has no setup section.`

**Cause** — no heading matches the accepted set.

**Fix** — add a heading whose text contains one of: `Setup`,
`Prerequisites`, `Installation`, `Requirements`, `Configuration`,
`Getting Started`, `Before You Begin`, `Environment`.

**Confirm**, from the repo root — `uv run validate readme <recipe-path>`

## README.md is missing a run section or code block

**Symptom** — `README.md has no run section.` or
`README.md has no fenced code block.`

**Cause** — no run heading, or a run heading with no command to copy.

**Fix**

1. Add a heading whose text contains one of: `Run`, `Running`, `Usage`,
   `Quickstart`, `Start`, `Deploy`, `How to Run`.
2. Under it, add a fenced code block with the exact command that starts the
   agent.

**Confirm**, from the repo root — `uv run validate readme <recipe-path>`

## pyproject.toml has a local ruff configuration

**Symptom** — `pyproject.toml contains a [tool.ruff*] block`

**Cause** — Ruff configuration lives in the repo root `pyproject.toml`, and
a recipe-level block would override it.

**Fix** — delete every `[tool.ruff]` and `[tool.ruff.*]` table from the
recipe's `pyproject.toml`. `align-recipe-pyproject` (AI skill) does this for
you.

**Confirm**, from the repo root —
`grep -n "tool.ruff" <recipe-path>/pyproject.toml` prints
nothing.

## Standalone Ruff config file

**Symptom** — `Standalone Ruff config found`

**Cause** — the recipe contains a `ruff.toml` or `.ruff.toml`.

**Fix** — delete the file. Ruff configuration lives in the repo root
`pyproject.toml`.

**Confirm**, from the repo root —
`ls <recipe-path>/ruff.toml <recipe-path>/.ruff.toml` reports
no such file.

## Project name doesn't match the required name

**Symptom** — `[project].name` together with
`does not match the required name`

**Cause** — `[project].name` is derived from where the recipe lives, and
yours does not match.

- `core/` and `contrib/` — the recipe folder basename.
- `skills/` — `<vertical>-<solution>`, because `skills/` interposes a
  mandatory vertical.

**Fix** — set the name the error reports as required:

```toml
# contrib/python/my-recipe
name = "my-recipe"

# skills/retail/product-search
name = "retail-product-search"
```

**Confirm**, from the repo root —
`uv run python .github/scripts/check_recipe_pyproject.py <recipe-path>`

## Project description doesn't match manifest

**Symptom** — `[project].description does not match manifest.description`

**Cause** — two descriptions for one recipe have drifted apart.

**Fix** — copy `manifest.description` verbatim into `[project].description`,
or delete `[project].description`, which is optional.

**Confirm**, from the repo root —
`uv run python .github/scripts/check_recipe_pyproject.py <recipe-path>`

## requires-python below 3.11

**Symptom** — `[project].requires-python` together with
`permits Python versions below 3.11`

**Cause** — the recipe admits a Python older than the repo minimum.

**Fix**

```toml
requires-python = ">=3.11"
```

**Confirm**, from the repo root —
`uv run python .github/scripts/check_recipe_pyproject.py <recipe-path>`

## pyproject.toml has no sibling uv.lock

**Symptom** — `has no sibling uv.lock`

**Cause** — every `pyproject.toml` with a `[project]` table or a `[tool.uv]`
section needs a `uv.lock` next to it.

**Fix**

```bash
# from the repo root
uv lock --project <recipe-path>
```

**Confirm**, from the repo root — `ls <recipe-path>/uv.lock`

## Missing [[tool.uv.index]] block

**Symptom** — ``pyproject.toml has no `[[tool.uv.index]]` block.``, or
`has default=true but url=`

**Cause** — the recipe does not declare public PyPI as its default index.

**Fix** — add this to the recipe's `pyproject.toml`, or run
`align-recipe-pyproject` (AI skill):

```toml
[[tool.uv.index]]
url = "https://pypi.org/simple/"
default = true
```

Use **double** brackets. `[tool.uv.index]` with single brackets is a
different TOML construct, and uv rejects it.

**Confirm**, from the repo root —
`uv run python .github/scripts/check_recipe_pyproject.py <recipe-path>`

## Env var missing from .env.example

**Symptom** — `Environment variable '<VAR>' is read by Python source but not
declared in .env.example`

**Cause** — your code reads a variable that anyone cloning the recipe has no
way to discover.

**Fix** — run `extract-python-environment-variables` (AI skill). It parses
the source and adds every variable it finds. Without an AI assistant, add
the names the error lists to `.env.example` by hand.

A false positive such as `os.getenv("HOME")` should already be suppressed.
If one slips through, file an issue against
[`check_env_vars.py`](../../.github/scripts/check_env_vars.py).

**Confirm**, from the repo root —
`python3 .github/scripts/check_env_vars.py <recipe-path>`

## uv.lock out of sync

**Symptom** — `is out of date — run: uv lock`

**Cause** — `uv.lock` no longer matches its sibling `pyproject.toml`. This
one failure turns several checks red at once, so fix it before anything
else.

**Fix**

```bash
# from the repo root
uv lock --project <recipe-path>
```

**Confirm**, from the repo root — `uv lock --check --project <recipe-path>`

## Lockfile references a non-PyPI URL

**Symptom** — `contains non-PyPI registry URLs`

**Cause** — an entry resolves somewhere other than `pypi.org` or
`files.pythonhosted.org`. Internal registries, GitHub Packages and
Artifactory are not allowed.

**Fix** — replace the dependency with a package published on PyPI, then
`uv lock --project <recipe-path>`.

**Confirm**, from the repo root —
`grep -n "url = " <recipe-path>/uv.lock` shows only
`pypi.org` and `files.pythonhosted.org`.

## VCS dependency in uv.lock

**Symptom** — `contains git/VCS dependencies`

**Cause** — a `source = { git = "..." }` entry. Git sources are not
reproducible and skip the registry's security verification.

**Fix** — depend on a PyPI-published package instead, then
`uv lock --project <recipe-path>`.

**Confirm**, from the repo root —
`grep -n "git = " <recipe-path>/uv.lock` prints nothing.

## Local path dependency in uv.lock

**Symptom** — `contains local path dependencies`

**Cause** — a `path`, `editable` or `directory` source, which resolves only
on the machine that committed it. uv's own `editable = "."` entry for the
workspace root is the one exception.

**Fix** — remove the local source, depend on the published package, then
`uv lock --project <recipe-path>`.

**Confirm**, from the repo root —
`grep -n "editable\|directory = " <recipe-path>/uv.lock` shows
nothing beyond `editable = "."`.

## Missing package hash in uv.lock

**Symptom** — `Every distribution needs a sha256 hash for supply-chain`

**Cause** — the lockfile was hand-edited, or generated by another tool.

**Fix**

```bash
# from the repo root
rm <recipe-path>/uv.lock
uv lock --project <recipe-path>
```

**Confirm**, from the repo root — `uv lock --check --project <recipe-path>`

## Runnability test missing

**Symptom** — `No test_runnability.py under <recipe>/tests/.`

**Cause** — every Python recipe needs a test proving its agent imports.

**Fix** — run `generate-python-runnability-test` (AI skill). Without an AI
assistant, copy the template from
[python.md — Copy-paste starters](./languages/python.md#copy-paste-starters)
and point it at your agent.

**Confirm**, from the recipe folder —
`uv run pytest tests/test_runnability.py`

## Ruff format or check failed

**Symptom** — `Would reformat` for a formatting failure, or a rule ID such
as `E501`, `F401` or `I001` for a lint failure.

**Cause** — the code does not match the repo's Ruff rules: line length 80,
double quotes.

**Fix**

```bash
# from the repo root
uv run ruff format <recipe-path>
uv run ruff check --fix <recipe-path>
```

Ruff cannot auto-fix everything. What is left is reported with its rule ID.

**Confirm**, from the repo root —
`uv run ruff format --check <recipe-path> && uv run ruff check <recipe-path>`

---

## Recipe is marked inactive

**Symptom** — `recipe-inactive`, or `is marked` followed by
`status: inactive`. A warning; your PR is not blocked.

**Cause** — the recipe's `manifest.yaml` says `status: inactive`, which
means a problem was found in it and went unresolved. A recipe left inactive
keeps sliding toward removal.

**Fix**

1. Check the recipe still installs and passes:
   ```bash
   # from the recipe folder
   uv sync --dev --frozen
   uv run --frozen pytest \
     --ignore=tests/integration \
     --ignore-glob='**/test_integration.py'
   ```
2. Update the package versions and re-lock if either step fails: `uv lock`.
3. Set `status: active` in `manifest.yaml` in the same PR. Nothing sets it
   back for you.

Editing docs or fixing a typo in an inactive recipe? Ignore this warning.

**Confirm**, from the recipe folder — `grep -n "status:" manifest.yaml`

## Core recipe is behind the current ADK major

**Symptom** — `adk-major-current`, alongside either
`cannot resolve to google-adk` or `pins google-adk`. A warning; your PR is
not blocked.

**Cause** — a recipe under `core/` resolves to a `google-adk` major older
than the current one. The message comes in three forms, and they need
different fixes:

| Message | Meaning |
| --- | --- |
| `cannot resolve to google-adk N.x` | `pyproject.toml` caps the dependency below the current major (`<2.0.0`, or a pin like `==1.31.0`). Re-locking alone cannot help. |
| `uv.lock pins google-adk X, but ... already permits N.x` | The declaration is fine; only the lock is behind. |
| a prerelease lock, such as `2.0.0a3` | On the current major, but anyone cloning inherits a dependency that can change without a deprecation path. |

**Fix**

1. Widen or remove the cap in `pyproject.toml` if the first message applies.
2. Port the recipe's code to the current major. Re-locking without porting
   produces a recipe that fails at runtime instead of in CI.
3. Re-lock:
   ```bash
   # from the repo root
   uv lock --upgrade-package google-adk --project <recipe-path>
   ```

**Confirm**, from the repo root —
`grep -n "google-adk" <recipe-path>/pyproject.toml <recipe-path>/uv.lock`

## Dockerfile build failed

**Symptom** — `[docker-build]` or `Docker image failed to build`

**Cause** — a `Dockerfile` at the root of the recipe directory failed to build. Every recipe that provides a root Dockerfile must build cleanly.

**Fix**

1. Build the image locally to reproduce the failure:
   ```bash
   docker build -f <recipe-path>/Dockerfile <recipe-path>
   ```
2. If files are missing in a `COPY` instruction, ensure all referenced files are committed or created conditionally during build.
3. If dependency synchronization fails during `uv sync`, ensure `uv.lock` is up to date and compatible with the container's Python version.

## Recipe container does not serve

**Symptom** — `[docker-serves]` or `Container exited unexpectedly` or `Service inside container did not become accessible`

**Cause** — the built container image exited prematurely on startup or did not respond to HTTP requests on port 8080.

**Fix**

1. Run the container locally with test environment variables:
   ```bash
   docker run -p 8080:8080 -e USE_IN_MEMORY_SESSION=true -e INTEGRATION_TEST=1 -e MODEL_NAME=gemini-3.5-flash <image-tag>
   ```
2. Inspect the container logs (`docker logs <container-id>`) for startup exceptions.
3. Ensure required configuration variables have defaults in code or `.env.example`, and that import-time GCP calls handle missing credentials gracefully when running offline or in tests.

## Non-blocking notices

**Symptom** — a `[NOTICE]` header. None of these block your PR.

| Notice | Fix |
| --- | --- |
| Hardcoded model name | Replace the literal with `os.getenv("MODEL_NAME")`, then run `extract-python-environment-variables` (AI skill). |
| `GOOGLE_CLOUD_PROJECT`, `GOOGLE_CLOUD_LOCATION` or `MODEL_NAME` absent from `.env.example` | Add them if the recipe reads them — see [Env var missing from .env.example](#env-var-missing-from-envexample). |
| Core recipe behind the current ADK major | See [the section above](#core-recipe-is-behind-the-current-adk-major). |
| Recipe marked `status: inactive` | See [the section above](#recipe-is-marked-inactive). |

## The AI reviewer keeps finding new things

**Symptom** — you fix everything the automated review said, push, and get a
fresh batch of comments. It feels like it will never end.

**Cause** — it used to work that way. Every push re-reviewed the whole pull
request, and anything already said was filtered out, so each round was forced
to surface findings you had not seen yet. On a big PR that could go on for a
long time.

**What happens now**

- A round reads only what changed **since the last review**, so a push that
  just fixes comments has almost nothing new to look at.
- Each round is allowed fewer comments than the last one actually produced.
- There is a hard ceiling on how many comments one PR can ever receive.
- After the second round only the Correctness and Security lanes run, so the
  smaller stuff stops.

The last line of every automated review tells you where you are:

> _Round 3 · 18 of this PR's 25 automated comments used · this round is capped
> at 4 · from here only Correctness and Security run._

**Fix** — nothing to fix; keep going and it will go quiet. Two things worth
knowing:

- The **House Rules** lane is exempt from all of the above. It is a script,
  not a model, and it reports repository rules that mostly fail CI — so it
  keeps commenting until you fix them. Those are the ones to act on.
- A comment you disagree with: resolve the thread or react 👎 to it, and the
  reviewer will not raise anything like it again on that PR.

The numbers live in [`.github/policy.yml`](../../.github/policy.yml) under
`pr_review_budget`.

## CI infrastructure failure

**Symptom** — `[ci-fault]` or `CI tooling failure in`

**Cause** — one of the repo's own checker scripts crashed, or the CI
environment failed: a network drop, a missing dependency, an unhandled file
encoding. Your changes did not cause it, which is why the annotation lands
on the checker and not on your files.

**Fix**

1. Re-run the failed job. Network and registry timeouts clear on a retry.
2. Still failing? Open an issue with the failing check, the `Detail:` line
   from the output, and a link to the run. Do not reshape your recipe to
   work around it.
3. Mention anything unusual in your recipe that could have triggered the
   crash — an unexpected file encoding, a hand-edited `uv.lock`.

## Something else

**Symptom** — an error this page does not list.

**Fix**

1. Search this page for keywords from the error message.
2. Run `uv run validate all <recipe-path>` from the repo root — a second
   failure is often the cause of the first.
3. Open an issue at
   [github.com/google/adk-recipes/issues](https://github.com/google/adk-recipes/issues):

   ```
   **Recipe path:** contrib/python/my-recipe
   **Failing check:** (name of the red check on your PR)
   **Error:** (paste the relevant log lines here)
   **Failed run:** (link to the run)
   ```

---

_Last updated: August 2026_

← [Checklist](../recipe-checklist.md) · [Handbook](./README.md)
