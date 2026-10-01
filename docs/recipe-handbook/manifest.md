# The Manifest

Every recipe has a `manifest.yaml` at its root. It declares what the
recipe is, who owns it, and what it depends on. CI validates it on
every PR that changes the recipe.

## Generate it with the `generate-manifest` skill

Don't write the file by hand. The
[`generate-manifest`](./skills-catalog.md#generate-manifest) repo
skill reads your recipe and writes a valid `manifest.yaml`. Ask your
AI coding assistant:

```text
"generate manifest.yaml for contrib/python/my-recipe"
```

The skill fills in what it can infer from your code. It leaves
`ownership.team` and `ownership.poc` as `TODO` placeholders on
purpose, because only you can supply them — see
[Ownership is a commitment](#ownership-is-a-commitment). It also marks
its draft `description` for review. Edit those fields, then check the
file:

```bash
uv run validate manifest <recipe-path>
```

## Required fields

| Field | Values | Meaning |
|---|---|---|
| `type` | `standalone`, `module` | See [Recipe type](#recipe-type). |
| `status` | `active`, `inactive` | Health of the recipe. New recipes are `active`. |
| `language` | `python`, `java`, `go`, `kotlin`, `typescript` | Primary language. Decides which language-specific files CI requires. |
| `description` | Text, at least 10 characters | What the recipe does and the value it provides. |
| `ownership.team` | Text | The real team that owns the recipe. See [Ownership is a commitment](#ownership-is-a-commitment). |
| `ownership.poc` | GitHub user ID | The one person accountable for the recipe's upkeep. See [Ownership is a commitment](#ownership-is-a-commitment). |
| `deployable` | `true`, `false` (default) | Required (`true`) for every `contrib/` recipe, along with a root `Dockerfile`. Optional under `core/` and `plugins/`. |

### Recipe type

- **`standalone`** — a complete recipe with its own entry point,
  runnable or deployable on its own. Example: an IT helpdesk agent.
- **`module`** — an importable sub-agent built to be orchestrated by a
  larger workflow, with no entry point of its own. Example: a BigQuery
  query-generator sub-agent.

Choose `module` only when the recipe cannot run by itself.

### Ownership is a commitment

`ownership.poc` and `ownership.team` are how maintainers find the
people responsible for a recipe. Get them right before you open a PR.

**Being the POC is a real commitment.** The point of contact is the
person maintainers go to when the recipe breaks, falls behind a new
ADK release, or needs a security or dependency fix. Name yourself only
if you will watch for those requests and act on them. A recipe whose
problems go unanswered is set to `inactive`, and one left inactive is
deprecated and removed from the repository. See
[Recipe is marked inactive](./troubleshooting.md#recipe-is-marked-inactive).

- **`ownership.poc`** — one GitHub user ID: the accountable person,
  not a team alias.
- **`ownership.team`** — the name of the actual team that owns the
  recipe. It must identify a team, so reviewers reject:
  - a company name, such as `Google`
  - the POC's GitHub user ID, or any other single person's ID
  - a placeholder, such as `N/A`, `none`, `TBD`, `-` or `TODO`

## Optional fields

| Field | Values | Meaning |
|---|---|---|
| `large` | `true`, `false` (default) | Opts into the relaxed size tier (`200 files / 10 MB` in `contrib/` and `plugins/`; see [anatomy](./anatomy.md#size-limits)). |
| `license` | SPDX identifier, e.g. `Apache-2.0` | Set only when the recipe declares a license. |
| `tags` | List of text | Classification tags. |
| `ownership.contributors` | List of GitHub user IDs | Additional contributors. |
| `architecture.agent` | `single`, `multi` | One agent or several. |
| `architecture.stateful` | `true`, `false` | `true` if the agent keeps state or memory across sessions, or writes transactionally to external systems. |
| `architecture.datasources` | List of `hardcoded`, `local`, `external` | Where data comes from: the source code, files bundled with the recipe, or a live system at runtime. Each value at most once. |
| `dependencies.libraries` | List of text | Libraries used, e.g. `ADK`, `LangGraph`. |
| `dependencies.services` | List of text | GCP or external services used, e.g. `vertex-ai`, `bigquery`. |

## CI rejects more than wrong types

A manifest fails validation when it:

- **Contains a field the schema does not define**, at any level. A
  typo such as `onwership` fails instead of being ignored.
- **Is missing, empty, or only comments.**
- **Still has the scaffold placeholders** in `ownership.team` or
  `ownership.poc`. See
  [ownership.team or poc is a placeholder](./troubleshooting.md#ownershipteam-or-poc-is-a-placeholder).
  CI matches only those exact strings; the other
  [ownership rules](#ownership-is-a-commitment) are checked in review.
- **Has a `description` starting with `TODO`**, even when it is long
  enough.

Every other failure names the field. See
[manifest.yaml missing or invalid](./troubleshooting.md#manifestyaml-missing-or-invalid).

## Complete example

```yaml
type: standalone
status: active
language: python
description: >-
  A support agent that answers billing questions from a product FAQ
  and hands account changes to a human.
deployable: true
license: Apache-2.0
tags: [customer-support, escalation]
architecture:
  agent: multi
  stateful: true
  datasources: [local, external]
dependencies:
  libraries: [ADK]
  services: [vertex-ai, firestore]
ownership:
  team: support-agents
  poc: your-github-username
  contributors: [teammate-username]
```

The smallest valid manifest is in the
[manifest section](./anatomy.md#manifestyaml) of anatomy.

---

← [Docs home](../README.md) · [Checklist](../recipe-checklist.md) · [Handbook](./README.md)
