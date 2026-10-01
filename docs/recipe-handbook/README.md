# Recipe Handbook

You're here to contribute a recipe to `contrib/`. Welcome.

This handbook is the reference — standards, tooling, and the
*why* behind each. If you already know your way around, the
[checklist](../recipe-checklist.md) is your day-to-day tool.
You don't have to read every handbook page; the checklist
links back here whenever a step needs more explanation.

## What makes a good recipe

Have a clear intent, a concrete problem the recipe solves for the ADK
community, and something new to teach. If you can't state it in one sentence,
revisit the idea before writing code. Recipes that duplicate existing examples
without new insight are rejected during review.

Every accepted recipe:

- Lives under `contrib/` with a valid `manifest.yaml`, `README.md`,
  `.env.example`, and `Dockerfile`.
- Passes the runnability and container checks.
- Has real owners in `manifest.ownership`.

## Handbook pages

**New here?** Start with the [checklist](../recipe-checklist.md) —
it covers everything on one page. Come back here for deeper context:

- [Anatomy of a recipe](./anatomy.md) — file layout rules for all
  recipes, regardless of language
- [The manifest](./manifest.md) — every `manifest.yaml` field and
  the rules CI enforces
- [Python language rules](./languages/python.md) — starts with the
  fast path; specific requirements and end-to-end scenarios
- [Repo skills catalog](./skills-catalog.md) — the repo skills
  that prepare and validate recipes

**Updating an existing recipe?** Run `prepare-python-recipe`
against your recipe path — it's safe to re-run and applies
any new requirements automatically. Then check the
[checklist](../recipe-checklist.md) for any manual steps.

**Reference:**

- [Repo oracle](./skills-catalog.md#repo-oracle) — ask how the repo
  itself works: policy, CI, ownership, process
- [Troubleshooting](./troubleshooting.md) — errors mapped
  directly to fixes
- Other languages: [Go](./languages/go.md) ·
  [Java](./languages/java.md) ·
  [Kotlin](./languages/kotlin.md) ·
  [TypeScript](./languages/typescript.md)

## Glossary

- **Recipe** — a runnable agent example (or importable agent
  module) under `core/`, `contrib/`, or `plugins/`, consumed by ADK
  developers and coding agents alike.
- **Repo skill** — a pre-loaded instruction set that your AI coding
  assistant follows when you ask it to perform a task (e.g.
  `prepare-python-recipe`). Files live in `.agents/skills/` and load
  automatically when you open this repo. Repo skills *build* the
  repo; they are never shipped to users.
- **Plugin** — a recipe under
  `plugins/<vertical>/<solution>/` (e.g. `plugins/retail/store-ops/`),
  where the vertical names the business domain that owns it.
  Shipped to users like any other recipe. Unrelated to repo skills.
- **Manifest** — `manifest.yaml`. Declares recipe metadata:
  type, language, ownership, description.
- **Runnability test** — a smoke test that imports the agent module
  and asserts `root_agent is not None`. Required for Python recipes at
  `tests/test_runnability.py`.
- **poc** — Point of Contact. A GitHub user ID; the person
  accountable for the recipe. Set in `manifest.yaml` as
  `ownership.poc`.
- **Structural check** — a CI validation that checks folder name,
  size limits, required files, and layout. Runs regardless of
  programming language.

## Contact

Open a GitHub issue at
[github.com/google/adk-recipes/issues](https://github.com/google/adk-recipes/issues).
Include the recipe path and the CI check name if you're
reporting a failure.

Pull requests are routed via [`.github/CODEOWNERS`](../../.github/CODEOWNERS)
once all CI checks pass and automated review comments are resolved.

---

← [Docs home](../README.md) · [Checklist](../recipe-checklist.md)
