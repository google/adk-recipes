---
name: make-python-recipe-deployable
description: >
  Makes an existing Python recipe deployable: generates the serving files a
  container needs (Dockerfile, .dockerignore, fast_api_app.py,
  app_utils/a2a.py, app_utils/services.py,
  app_utils/reasoning_engine_adapter.py) and configures the recipe to match
  (required serving dependencies, the App object in agent.py, the hatch wheel
  package, manifest.deployable). Interactive by design — it asks the recipe
  owner about runtime data directories and stops for a human decision when a
  recipe needs an ADK migration or carries a legacy app_utils generation.
  When docker is available it offers to PROVE the claim: it builds the
  generated Dockerfile, runs it, probes it, and refuses to flag a recipe
  deployable if the container does not come up. Does NOT deploy or write
  terraform. Use when the user wants to "make this recipe deployable", "add a
  Dockerfile to a recipe", "add the serving files", "containerize a recipe",
  "verify the container builds", or prepare a recipe for Cloud Build /
  Artifact Registry.
metadata:
  author: Google
  license: Apache-2.0
  version: 1.0.0
---

# Make a Python Recipe Deployable

A **deployable** recipe is one that can be packaged into a container and run as
a service. This skill writes the files that requires and configures the recipe
to match.

It does **not** build an image, deploy anything, or provision infrastructure.
Image builds happen later via Cloud Build → Artifact Registry; this skill's job
ends when the files are correct.

The standard it implements lives in `.github/policy.yml` under `deployability:`
— the minimum `google-adk` version, the required dependency list, the required
file list, and the legacy `app_utils` file list. **Change the standard there,
not in the script.**

---

## What "deployable" means here, and the one distinction that matters

`deployable` in `.github/schemas/manifest-schema.json` means "can be deployed
with one click". Two independent questions decide the outcome:

1. **Does it need infrastructure a human must provision?** If yes it is
   containerized, not one-click deployable.
2. **Did we PROVE the container works, or only assume it?**

| Outcome | Meaning | `manifest.deployable` |
|---|---|---|
| `deployable-verified` | No bespoke infra, and the image built and served. | set to `true`, **on evidence** |
| `deployable-unverified` | No bespoke infra, but nothing built it. | set to `true`, on static checks |
| `containerized-verified` | Built and served, but needs backing infra. | **left unset** |
| `containerized-unverified` | Needs backing infra, and unproven. | **left unset** |
| `verification-failed` | Docker was usable and the recipe failed. | **left unset**, and a pre-existing flag is retracted |
| `verification-inconclusive` | Verification was **attempted** and defeated by the environment — network, registry, or a runtime that could not exec the image here. It proves nothing, so nothing is retracted. Not the same as `-unverified`: that means nobody tried, this means you did and should retry. | set if static checks earned it |
| `blocked` | The run stopped without a usable verdict. Four causes: a gate refused it up front (nothing written); a hard ERROR disqualified the recipe; the **skill itself** faulted; or verification was **deferred** pending `uv lock` — a pause, not a judgement, so re-invoke to finish. Check `files_written` (only a gate stop guarantees a clean tree) and read `container-verify` to tell a deferral from a real stop. | untouched — EXCEPT that a disqualified recipe has a stale flag **retracted**. Disqualified means a hard ERROR *or* no usable `app` object in agent.py, and the latter records no ERROR — so never infer from the absence of an ERROR that the tree was left alone. |

Never describe a `containerized` result as "deployable" to the user. Setting
that flag on a recipe that still needs hand-written terraform puts a false
claim in the manifest, which is worse than leaving it unset.

Equally, never describe an `-unverified` result as proven. It means the files
are right by inspection and nobody built the image — which is the *normal*
result on a machine without docker, not a defect.

**Why `-unverified` still sets the flag.** Absence of evidence is not evidence
of absence. Withholding it whenever docker is missing would judge the recipe
by the checker's laptop rather than by its own quality, and this skill's
primary user has no container runtime at all. Only a verification that
actually *failed* withholds the flag — a recipe proven broken is not
deployable, whatever the static checks said.

---

## What generating `a2a.py` does and does not prove

Nothing, on its own. These templates were designed assuming the project was
scaffolded by agents-cli, and **a file named `a2a.py` does not make an agent
behave correctly over A2A**. The skill copies and configures; it does not
certify. Say so in your summary — do not tell the owner their recipe "supports
A2A" because the file exists.

---

## Rules for the agent

1. **Confirm before applying.** Always run a dry-run first, show the plan, and
   get a "yes" before `--apply`. The skill writes six files and edits three.
2. **Ask the questions in the Interview below** before applying — but only the
   ones the dry-run shows are relevant. Do not interrogate the owner about
   data directories for a recipe that has none.
3. **Never override a gate on your own.** `adk-locked-version`,
   `adk-version-floor` and `legacy-app-utils` return `needs_input` and stop the
   run. Each means a human has to change code. Report the message verbatim and
   stop; do not go hunting for a way around it.
4. **Never widen an existing version bound** to satisfy the standard. The
   script leaves version specifiers exactly as the recipe wrote them and
   reports them for confirmation. It *does* merge in missing **extras**
   (`google-adk` → `google-adk[gcp,otel-gcp]`, same version bound), because
   the generated code imports what those extras install and the recipe would
   not start otherwise. Those are different risks: an extra only adds a
   package's own optional dependencies, while a version rewrite can move the
   recipe onto code it was never tested against.
5. **Do not overwrite an existing `fast_api_app.py`** without explicit
   confirmation. An existing one is usually bespoke — `long-horizon-harness`'s
   is ~400 lines of custom routing. `--overwrite` exists but is a deliberate
   choice, not a default.
6. **Run the follow-ups yourself** after a successful apply (see Step 5); the
   script does not, so a failure is attributable to the right step.
7. **Stay inside the recipe.** If a run reveals problems in a different
   recipe, mention them and move on.

---

## Pipeline

### Step 0 — Dry run

```bash
uv run --no-project --with tomlkit --with 'ruamel.yaml' --with packaging \
  python3 .agents/skills/make-python-recipe-deployable/scripts/make_deployable.py \
  --recipe-dir <RECIPE_DIR>
```

Prints a JSON report: `outcome`, `agent_package`, `checks`, `todos`, `notes`.
Nothing is written. Exit code `0` = fine, `1` = a gate needs human input,
`2` = error.

Summarise it for the owner. Do not dump the raw JSON.

### Step 1 — Handle gates

If any check is `needs_input`, **stop**. The three that gate:

- **`adk-locked-version`** — `uv.lock` resolves `google-adk` to an older major
  than the standard requires. The declared specifier may well permit the newer
  version, which is exactly the trap — in both directions. Re-locking IN
  PLACE keeps the old major (uv is sticky), so the recipe would ship the new
  serving dependencies against an ADK that cannot support them; resolving
  FRESH crosses the major silently,
  and the agent code has only ever run against the old one. The owner must port
  the agent first. This script rewrites metadata; it cannot migrate code.
- **`adk-version-floor`** — the specifier itself excludes the required version
  (a `<2.0.0` ceiling, an `==1.31.0` pin). Same conclusion.
- **`legacy-app-utils`** — the package carries the old ASP-era generation
  (`telemetry.py`, `typing.py`, `deploy.py`, `memory_config.py`). Filenames do
  not collide with the new set, but the two wire telemetry and services
  differently and the existing `fast_api_app.py` imports the old ones.
  Generating over the top orphans them or double-wires telemetry. A human
  decides how to migrate.

Also check `already-deployable`. If it is `report_only`, the recipe already
serves and you must confirm the owner wants to migrate onto the standard
layout before applying — see the advisory at the bottom of this file.

### Step 2 — Interview

Ask only what applies. Keep it to one round.

1. **Runtime data directories.** Does the agent read anything at runtime that
   is not in the agent package — `assets/`, `sample_data/`, a config file?
   Those need `COPY` lines or the container fails *at request time*, not at
   build time, which is why a human confirms rather than the skill guessing.
   Pass them as `--data-dirs assets,sample_data`.
2. **An existing serving file was found.** The dry-run reports each as
   `report_only`. Ask whether to keep it (default) or replace it
   (`--overwrite`). Show what the existing file does first.
3. **Version bounds that sit below the standard.** The report lists any
   existing requirement it left alone (e.g. `google-adk>=2.2.0` against a
   `>=2.6.0` standard). Resolution usually lands on a satisfying version
   anyway — `a2a-sdk>=1.0` forces `google-adk>=2.5` on its own — but confirm
   the owner is happy rather than rewriting their pin.
4. **Deployment region.** Nothing in a recipe declares one, and it goes into
   `agents-cli-manifest.yaml`, so it is a real decision. Default `us-east1`
   (agents-cli's own). Pass `--region us-central1` etc.
5. **Backing infrastructure.** If the outcome is `containerized`, confirm the
   owner understands `manifest.deployable` stays unset and why.

### Step 3 — Apply

```bash
uv run --no-project --with tomlkit --with 'ruamel.yaml' --with packaging \
  python3 .agents/skills/make-python-recipe-deployable/scripts/make_deployable.py \
  --recipe-dir <RECIPE_DIR> --apply [--data-dirs a,b] [--overwrite] \
  [--region us-central1]
```

### Step 4 — Report what changed

List `files_written` and the checks that moved to `fixed`.

### Step 5 — Follow-ups (you run these)

When the script changes dependencies the lockfile goes stale, and the new
files are unformatted. In order:

```bash
cd <RECIPE_DIR> && uv lock --python 3.11
```

`--python 3.11` because CI pins it — locking with a newer local interpreter
produces a lockfile CI rejects with a misleading "out of date" error.

**Run the lock command the report's todos actually give you**, and if they
give you none, skip it. The report picks between three states rather than
always asking for a re-lock:

| Report todo | State | What to run |
|---|---|---|
| `uv lock --upgrade-package google-adk --python 3.11` | Pinned below the ADK floor | That command — a plain `uv lock` here is a **no-op** |
| `uv lock --python 3.11` | This run changed dependencies, or uv says the lockfile is out of date | A plain re-lock |
| *no lock todo* | Nothing changed and `uv lock --check` passes | Nothing — re-locking would only churn `uv.lock` |

The third row is why an idempotent re-run is quiet. The script asks
`uv lock --check` before staying silent, so an earlier run that added
dependencies and never locked still produces the todo.

If `adk-locked-version` came back `report_only`, the recipe is pinned below
the ADK floor — uv keeps any locked version that still satisfies the declared
specifier. The report will hand you this instead:

```bash
cd <RECIPE_DIR> && uv lock --upgrade-package google-adk --python 3.11
```

Then confirm the resolved pair. An ADK below 2.5 alongside `a2a-sdk` 1.x looks
fine in the lockfile and dies at import with `cannot import name 'TextPart'
from 'a2a.types'` — invisible to every static check, and one of the reasons
Step 6.5 exists.

```bash
# from the REPO ROOT, so the root ruff config wins
uv run ruff format <RECIPE_DIR>/ && uv run ruff check --fix <RECIPE_DIR>/
```

Then the repo validators:

```bash
uv run validate manifest <RECIPE_DIR>
uv run validate structure <RECIPE_DIR>
cd <RECIPE_DIR> && uv run pytest tests/ -q
```

### Step 6 — Boot check (the real proof)

Static checks cannot tell you the recipe actually serves. This can, it needs no
container runtime, and it is the closest thing to a correctness oracle the
skill has. Run it from inside the recipe after `uv sync`:

```bash
cd <RECIPE_DIR> && uv sync --python 3.11 && uv run --python 3.11 --with httpx python -c "
import warnings; warnings.filterwarnings('ignore')
from fastapi.testclient import TestClient
from <PKG>.fast_api_app import app
with TestClient(app) as c:          # entering runs the lifespan
    print('/list-apps ->', c.get('/list-apps').status_code, c.get('/list-apps').json())
    card = [r.path for r in app.routes if 'well-known' in r.path]
    print('agent card ->', c.get(card[0]).status_code if card else 'NO A2A ROUTES')
"
```

Expected: `/list-apps` returns `200` listing the agent package, and the agent
card returns `200`. Entering the `TestClient` context is what triggers the
lifespan — without it the A2A routes never attach and the check is worthless.

If the agent card 404s or no A2A routes exist, the A2A wiring did not take
effect. Report that plainly; **do not** describe the recipe as A2A-capable.

Warnings about experimental `InMemoryCredentialService` are expected and
harmless.

### Step 6.5 — Container verification (ask first)

Step 6 proves the app boots *on this machine*. This proves the **image** the
recipe will actually be deployed as. It is the only step that turns "we
generated a Dockerfile" into evidence, and it is what lets the word
"deployable" mean anything.

**Look at the `docker` check in the report.** It is present in every run,
including the dry-run, and its `details.docker_state` is one of:

| State | Meaning | What you do |
|---|---|---|
| `absent` | No `docker` on PATH. | Skip. Say nothing alarming — this is the common case, not a problem. |
| `unreachable` | Binary present, daemon not answering. | Skip, same as above. Mention the daemon is down in case they want to start it. |
| `usable` | Daemon responding. | **Ask the owner** (below). |

When and only when the state is `usable`, ask:

> Docker is available. Shall I build the generated Dockerfile and check the
> container actually serves? It takes a few minutes, and it means
> `manifest.deployable` is set on evidence rather than on inspection. If the
> container does not come up, I will not set the flag.

If they decline, carry on — the outcome ends `-unverified` and that is a
legitimate result. **Do not decide for them, and do not skip the question
because verification seems slow.**

On a "yes", re-invoke with **both** `--apply` and `--verify-container`.
`--verify-container` does *not* imply `--apply` — on its own it reports that
verification needs the files on disk, and builds nothing:

```bash
uv run --no-project --with tomlkit --with 'ruamel.yaml' --with packaging \
  python3 .agents/skills/make-python-recipe-deployable/scripts/make_deployable.py \
  --recipe-dir <RECIPE_DIR> --apply --verify-container [--data-dirs a,b]
```

**Run it after Step 5's `uv lock`, not before.** The Dockerfile runs
`uv sync --frozen`, which cannot succeed until the lockfile matches. If the
lockfile is stale the script does not build — it defers `manifest.deployable`,
returns `needs_input`, and tells you to lock first. That is by design: a build
attempted against a stale lockfile fails with an error that looks exactly like
a broken template and is not.

What it does: builds for `linux/amd64` (Cloud Run's platform), runs the
container with the recipe's own `.env.example` values plus the policy's
`container_env`, polls `/list-apps` until it answers, then probes the A2A
agent card. It removes the container and the image afterwards.

Reading the result:

- **`container-build` ERROR** — the Dockerfile does not build. The check
  carries a `details.hint` naming the likely structural cause. Report it and
  stop; the recipe is not deployable.
- **`container-serves` ERROR** — the image builds but the app does not come
  up. The log tail is in the message. `manifest.deployable` was not set.
- **`container-a2a` REPORT_ONLY** — it serves, but the agent card did not
  return 200. The A2A wiring did not take effect. Say so plainly and **do
  not** call the recipe A2A-capable.

⚠️ **Running a container is allowlisted, not automatic.** Recipes not on
`deployability.verification.run_allowlist` are **built only**, because some
create real cloud resources at import — `core/python/cross-session-memory`
calls `client.agent_engines.create()` at module scope. A build-only result is
reported as unproven, never as a pass. Add a recipe to the allowlist only
after reading its package for import-time side effects.

### Step 7 — Close out

Walk the report's `todos` with the owner — `.env.example` entries for any new
variables (the `extract-python-environment-variables` skill does this), and
terraform if the outcome was `containerized`.

If you created a `.venv` in the recipe to run Step 6 and it was not there
before, remove it.

---

## Deliberately not in scope

| Not done | Why, and what does it instead |
|---|---|
| Deploying, or pushing an image anywhere | Out of scope by decision. Cloud Build builds and Artifact Registry stores the real image. Step 6.5 builds one only to verify its own output, then deletes it — docker is an instrument here, never a deployment mechanism. |
| Running a container that is not allowlisted | Some recipes create real cloud resources at import. Unlisted recipes are built only. |
| Migrating agent code across an ADK major | A code migration, not a metadata rewrite. Same stance as `align-recipe-pyproject`. |
| Merging a legacy `app_utils` generation | Needs human judgement about telemetry and feedback wiring. |
| Writing terraform | Two deployable recipes in this repo share *zero* infrastructure resources; nothing is templatable. |
| Removing `[tool.ruff*]`, fixing `requires-python` | `align-recipe-pyproject` |
| Completing `.env.example` | `extract-python-environment-variables` |
| Running `uv lock`, ruff, or the boot check | You do, in Steps 5-6, so failures are attributable to the right step. |

---

## `agents-cli-manifest.yaml`

Written when `deployability.emit_agents_cli_manifest` is true (it is).

This file is **functional, not decorative**. `agents-cli` uses it as the
project-root marker — `find_project_root()` walks up looking for it — and
`agents-cli deploy` reads `create_params.deployment_target` to choose how to
deploy. Without it, deploy reports "No agents-cli-manifest.yaml found".

The provenance problem — these recipes were never scaffolded by agents-cli —
is handled by **omitting the fields that would be fiction**, not by skipping
the file:

| Omitted | Why omission beats a value |
|---|---|
| `acli_version` | No scaffold ran. `check_cli_version` returns early on an absent version; a *fabricated* one makes the CLI tell the owner to run `agents-cli scaffold upgrade` on a project that cannot be upgraded. |
| `generated_at` | Never read by `ProjectConfig.from_dict`, and asserts an event that did not happen. |
| `base_template` | Only used by upgrade/enhance. Already defaults to `adk`, so writing it changes nothing. |

Everything written is derived from the recipe or from what the skill just
generated. Verified against agents-cli 1.4.0's own parser: the project root
resolves, every field reads back correctly, and
`require_agent_directory` / `require_deployment_target` / `require_a2a_project`
all pass.

An existing `agents-cli-manifest.yaml` is never overwritten.

---

## Templates

`resources/templates/` holds the serving files, vendored from
`agents-cli` 1.4.0's `scaffold/base_templates/python` and
`scaffold/deployment_targets/{cloud_run,agent_runtime}/python`, rendered for
the **cloud_run** target with **in-memory** sessions, then formatted to this
repo's ruff config so generated files pass CI unmodified.

Two placeholders are substituted at copy time:

| Placeholder | Becomes |
|---|---|
| `__AGENT_PACKAGE__` | the recipe's agent package (`app`, `horizon`, ...) |
| `__PROJECT_NAME__` | `[project].name` from `pyproject.toml` |

Both are valid Python identifiers on purpose, so the templates parse and stay
lint-checked in place rather than only after substitution.
`__AGENT_PACKAGE__` is registered in the root `pyproject.toml`'s
`known-first-party` so isort orders template imports exactly as the rendered
output needs them.

**Known divergences from agents-cli, all deliberate:**

- The Dockerfile's `FROM python:X-slim` is rewritten from the recipe's own
  `requires-python` floor. The template hardcodes 3.12; recipes here target
  3.11, 3.12 and 3.13.
- `reasoning_engine_adapter.py`'s streaming route duck-types the object it is
  about to iterate instead of assuming an async generator. `streaming_methods`
  merges the operation registry's **sync** `stream` bucket with `async_stream`,
  so `async for` over a method from the former raises
  `TypeError: 'async for' requires an object with __aiter__ method, got
  generator` — at request time, only for whoever streams, and never during a
  build. The sync route already drew exactly this distinction for the `""` and
  `async` buckets via `iscoroutinefunction`; the streaming route did not.
  **This is a fix to the vendored source, so re-rendering from a newer
  agents-cli will silently revert it** — re-check the streaming route after any
  re-render.
- `reasoning_engine_adapter.py` is included even for cloud_run because the
  Recipe Deployability doc lists it unconditionally, while agents-cli ships it
  only under `agent_runtime`. **Settled by verification: in a cloud_run recipe
  it is dead code.** Nothing imports it — `fast_api_app.py` pulls in only
  `app_utils.services` and `app_utils.a2a` — and its own
  `from agentplatform... import AdkApp` would raise `ModuleNotFoundError` if
  anything did, because `agentplatform` is not a required dependency and does
  not appear in a resolved `uv.lock`. Verified containers build and serve
  without it. Whether the published doc should stop listing it is a standards
  decision, not a code one.

Since these are vendored, they drift as agents-cli moves. Re-render from a
newer agents-cli when the standard changes; do not hand-edit them to fix a
single recipe.

---

## Verified end to end

### Against real containers

Both taken through the full pipeline including Step 6.5, then reverted.
Deployable recipes live under `core/` or `contrib/`; `skills/` is out of scope.

| Recipe | Outcome | What it proves |
|---|---|---|
| `contrib/python/financial-advisor` | `deployable-verified` | Image builds, `/list-apps` → 200, agent card → 200, flag set on evidence. |
| `core/python/rag-agent-search` | `containerized-verified` | Builds and serves, **and** the flag still correctly stays unset because it needs a datastore. The two axes compose. |

Three failures were found by building that no static check saw. All three are
the reason this step exists:

1. **`financial-advisor` crashed on import with no `GOOGLE_CLOUD_PROJECT`.**
   Its `__init__.py` does
   `os.environ.setdefault("GOOGLE_CLOUD_PROJECT", project_id)` where
   `google.auth.default()` yields `None` without credentials, and `setdefault`
   only assigns when the key is absent. Not a recipe defect — Cloud Run always
   supplies a project — so `container_env` now models the platform.
2. **`rag-agent-search` crashed on `Gemini(model=None)`.** It reads
   `MODEL_NAME`, and `.dockerignore` correctly keeps `.env` out of the image.
   Fixed by `load_env_example`, which seeds the container from the recipe's
   own declared environment. Unconfigured is not the same as broken.
3. **`rag-agent-search` then failed with `ImportError: cannot import name
   'TextPart' from 'a2a.types'`** — a genuine incompatibility. It had resolved
   to `google-adk 2.3.0` against `a2a-sdk 1.1.2`, and adk ≤2.4.0 expects
   `a2a-sdk<0.4`. See the warning below; this one is a live trap.

> ⚠️ **`uv lock` does not raise an already-locked version.** uv is sticky: it
> preserves any locked version that still satisfies the declared specifier, so
> a recipe declaring `google-adk>=2.0.0` stays on whatever it was pinned at.
> `rag-agent-search` stayed on 2.3.0 through a plain `uv lock` and only moved
> to 2.7.1 under `uv lock --upgrade-package google-adk`. The coupling the
> policy leans on (`a2a-sdk>=1.0` forcing adk to 2.5+) travels with
> google-adk's **`a2a` extra**, which the required dependency list does not
> use — so it never constrains resolution.
>
> The `adk-locked-version` check now states this correctly and emits
> `uv lock --upgrade-package google-adk` as the remedy in its `details` and in
> the run's todos. **Use the command the report gives you**, not a plain
> `uv lock`, whenever that check is `report_only`.

### Against a live interpreter

`skills/retail/product-search` was taken through the pipeline before `skills/`
was ruled out of scope for deployability. Kept as the record of what the Step 6
boot check looks like when it works:

- `uv lock` resolved to `google-adk 2.6.2` + `a2a-sdk 1.1.2` — even though the
  recipe's own bound is `>=2.2.0`, because `a2a-sdk>=1.0` forces `google-adk`
  to 2.5+ on its own. That coupling is the reason for the policy floor.
- All three `app_utils` modules import.
- `fast_api_app:app` builds — 71 routes, no credentials needed.
- Booted through the lifespan: `/list-apps` → `200 ['scripts']`, and
  `/a2a/scripts/.well-known/agent-card.json` → `200`.
- Running the skill a second time wrote nothing and changed no file on disk.

---

## Current recipe landscape

Measured by running the dry-run against every Python recipe under `core/` and
`contrib/` — not predicted. Deployable recipes live in those two trees;
vertical skills under `skills/` are out of scope.

Outcomes below are the dry-run's, so they all end `-unverified`: a dry run
builds nothing. Running with `--apply --verify-container` on a machine with
docker is what upgrades them (`financial-advisor` and `rag-agent-search` have
both been taken to `deployable-verified` and `containerized-verified`).

| Recipe | Dry-run outcome | Why |
|---|---|---|
| `contrib/python/financial-advisor` | `deployable-unverified` | loose pin, but locked at 2.6.2 |
| `core/python/rag-agent-search`, `rag-vector-search` | `containerized-unverified` | need a datastore / vector index |
| `core/python/long-horizon-harness`, `ambient-expense-agent` | `containerized-unverified` + **already-deployable** flag | they already serve; see the advisory below |
| `contrib/python/market-research-agent`, `core/python/deep-search`, `core/python/safety-plugins` | `blocked` | `adk-locked-version` — locked on ADK 1.x |
| `core/python/cross-session-memory`, `genmedia-for-commerce`, `oauth-user-consent-flow` | `blocked` | `adk-version-floor` — the ceiling excludes the floor |
| `contrib/python/cross-border-data-router` | `blocked` | legacy `app_utils` |

**The `already-deployable` advisory.** When a recipe already has a `Dockerfile`
*and* a `<pkg>/fast_api_app.py`, it serves by its own arrangement.
`long-horizon-harness` is the case: a bespoke ~400-line entrypoint wiring A2A
from its own `horizon/a2a/` package. Neither file is replaced without
`--overwrite`, but `app_utils/` modules generated alongside a bespoke
entrypoint are **dead code** unless someone wires them in. Confirm the owner
wants to migrate onto the standard layout before applying.
