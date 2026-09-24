---
name: generate-manifest
description: Scan an ADK recipe directory and generate a manifest.yaml for it based on the schema at .github/schemas/manifest-schema.json. Use when the user wants to create or generate a manifest.yaml for a recipe under core/, contrib/, or plugins/.
---

# Generate Manifest

Scan a recipe directory and produce a valid `manifest.yaml` for it, then validate it against the schema.

## Process

### 1. Identify the target recipe

The user will provide a path to a recipe directory (e.g. `core/python/rag-agent-search`, `contrib/python/my-recipe`, `plugins/retail/store-ops`). If they don't, ask for it before proceeding.

### 2. Read the schema

Read `.github/schemas/manifest-schema.json` from the repo root to understand the current required and optional fields, allowed enum values, and constraints. Do not rely on memory — always read the live schema file so changes to it are automatically picked up.

### 3. Scan the recipe directory and infer only what the code proves

Read the following files if they exist:

- `pyproject.toml` / `requirements.txt` — primary language (for `language` field)
- `app/agent.py` or equivalent entry point — single vs multi-agent, agent count
- `Makefile` — deploy targets (determines `deployable`)
- `app/` source files — stateful writes, datasource patterns
- `AGENTS.md` — architecture notes if present

**Field-by-field rules — read carefully before filling any field:**

| Field | Rule |
|---|---|
| `type` | Infer from code. `standalone` = has its own runnable entry point: look for an `if __name__ == "__main__"` block, a `make playground`/run target, an `adk web`/`adk run` invocation, or a CLI. `module` = only importable (it just exports a `root_agent`/`Agent` for another workflow to orchestrate, with no way to run on its own). When ambiguous, re-read the schema's `type` description and prefer `module` only when there is genuinely no entry point. |
| `deployable` | OPTIONAL. `true` only if a single `make` target or script deploys everything with no manual steps. Omit the field entirely if false (schema default is `false`). |
| `large` | OPTIONAL. Whether to opt into the relaxed size tier. Exact limits depend on whether the recipe lives under `core/` or `contrib/` or `plugins/` and are defined in `.github/policy.yml` (`recipe_size_limits`) — as of writing: `contrib/` and `plugins/` default is 70 files / 2 MB and large is 200 files / 10 MB; `core/` default is 500 files / 50 MB. Omit the field unless the recipe would otherwise exceed the default tier's limits (schema default is `false`). |
| `status` | Always `"active"` unless there is explicit evidence of abandonment. |
| `language` | Read from file extensions or `pyproject.toml`. Never guess. |
| `description` | Read `README.md` and `AGENTS.md` only (author-written intent, not code). Write a draft of at most 15 words summarising what the recipe does. The schema requires at least 10 characters, so keep the draft comfortably above that. Append the comment `# TODO: review and expand this draft description`. If neither file exists or the intent is unclear, fall back to `"DESCRIPTION"` with the same TODO comment. |
| `architecture.agent` | Infer from code only: count `Agent(` or equivalent constructor calls. `single` or `multi`. Omit the whole `architecture` block if uncertain. |
| `architecture.stateful` | Infer from code: `true` only if the recipe writes persistently to an external system (DB, vector store, GCS) during normal operation — not just one-time setup. |
| `architecture.datasources` | Infer from code: `hardcoded` = data literals in source; `local` = files bundled in repo; `external` = live systems queried at runtime. Can be multiple. |
| `dependencies` | Include the block but comment it out entirely. Do NOT infer library or service names — GCP product names change and guesses will be wrong. Use the commented-out sample shown in the template. |
| `ownership.team` | Always use the exact placeholder `"TODO: Replace with your team name"` — this is the literal string the validator checks for, so it correctly fails validation until a human fills it in. Never invent or infer a team name. |
| `ownership.poc` | Always use the exact placeholder `"TODO: Replace with your GitHub user ID"` — this is the literal string the validator checks for. Never invent or infer a GitHub ID from email addresses, file authors, or any other source. |
| `ownership.contributors` | OPTIONAL. Omit the field entirely. Leave a commented-out sample line so the author knows it exists. Never infer contributor IDs. |
| `tags` | Include but comment out entirely, with a sample entry showing the expected style. Do NOT generate real tags — tag choices are the author's call. |
| `license` | OPTIONAL. If the recipe's source tree contains a `LICENSE` file or the author has explicitly stated a license, record the SPDX identifier (e.g. `"Apache-2.0"`, `"MIT"`, `"BSD-3-Clause"`). Otherwise comment it out. Never infer or guess — only set it if it is explicitly declared. |

### 4. Make judgment calls explicitly

Before writing the manifest, briefly state what you found for each inferred field (`type`, `deployable`, `architecture.*`) and cite the specific file/line that supports your conclusion. This lets the user catch errors before they land in the file.

### 5. Write the manifest

Write `manifest.yaml` to the root of the recipe directory. Match the concise inline-comment style used by the existing manifests in `core/` and `contrib/` — allowed values shown as `# Options: [...]` after each field:

```yaml
type: "..."          # Options: [standalone | module]
status: "active"     # Options: [active | inactive]
language: "..."      # Options: [python | java | go | kotlin | typescript]
description: "..."   # TODO: review and expand this draft description

# deployable: true   # (optional) one-command deploy, no manual steps; omit if false (default)
# large: true        # (optional) opt into the relaxed size tier; see .github/policy.yml (recipe_size_limits) for the exact numbers; omit if false (default)

architecture:          # (optional) omit the whole block if nothing below can be inferred from code
  agent: "..."          # Options: [single | multi]
  stateful: false       # Options: [true | false]
  datasources:          # Options: [hardcoded | local | external]
    - "..."

# dependencies: (optional) uncomment and fill in with canonical names
#   libraries:
#     - "ADK"
#     - "pandas"            # example — replace with actual libraries used
#   services:
#     - "GCP Project"
#     - "Cloud Run"         # example — replace with actual GCP/external services used

ownership:
  team: "TODO: Replace with your team name"
  poc: "TODO: Replace with your GitHub user ID"
  # contributors: (optional) uncomment and add GitHub IDs of additional contributors
  #   - "github-id-1"

# tags: (optional) uncomment and replace with meaningful labels
#   - "rag"               # example — use technology names, patterns, and use-case keywords
#   - "gemini"

# license: "Apache-2.0"  # (optional) SPDX identifier; only set if explicitly declared — see https://spdx.org/licenses/
```

Omit `architecture` entirely if none of its sub-fields can be determined from the code.

### 6. Validate

After writing the manifest, run the validator **from the repo root** (do not `cd` into `tools/`, and do not pass `--active`):

```bash
uv run validate manifest <recipe-path>
```

`<recipe-path>` must be **relative to the repo root** (e.g. `core/python/my-recipe`, `contrib/python/my-recipe`, `plugins/retail/store-ops`), not an absolute path.

Interpret the result carefully — there are two kinds of failure:

- **Real schema errors** (missing required field, invalid enum value, description under 10 characters, malformed YAML): fix these in the manifest and re-validate until they are gone.
- **`ownership.team` / `ownership.poc` placeholder errors**: these are EXPECTED and must be left as-is. The validator deliberately fails while the placeholders are in place, to force a human to supply real values before merge. Do NOT invent a team name or GitHub ID to make validation pass.

So a correctly generated manifest will still report `[FAIL]` — but only on the two ownership placeholders, and nothing else. That is the intended outcome; report it to the user as "valid except for the ownership placeholders you need to fill in". Do not change or modify any other files in the repository. Also do not commit the changes.

### 7. Report back

Tell the user:
1. What was inferred from the code and which file/line supports each inference.
2. Which fields need human input before the manifest will pass validation: `ownership.team` and `ownership.poc` (placeholders — validation intentionally fails until these are replaced with real values). Also flag `description` (draft generated — must be reviewed and expanded) and `dependencies`/`tags` (commented out, ready to uncomment and fill in).
