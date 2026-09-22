<!-- word count: 603 (target 500, cap 800) -->

# Recipe Handbook

You're here to contribute a recipe to `contrib/`. Welcome.

This handbook is the reference — standards, tooling, and the
*why* behind each. If you already know your way around, the
[checklist](../recipe-checklist.md) is your day-to-day tool.
You don't have to read every handbook page; the checklist
links back here whenever a step needs more explanation.

## What makes a good recipe

**Recipes must earn their place.** Have a clear intent, a
concrete problem the recipe solves for the ADK community, and
something new to teach. If you can't state it in one sentence,
revisit the idea before writing code. Recipes that duplicate
existing examples without new insight may not be accepted.

Every accepted recipe:

- Lives under `contrib/` with a valid `manifest.yaml` and a
  `README.md`.
- Passes the runnability test (agent code loads without
  crashing).
- Has real owners in `manifest.ownership`.

## Handbook pages

**New here?** Start with the [checklist](../recipe-checklist.md) —
it covers everything on one page. Come back here for deeper context:

- [Anatomy of a recipe](./anatomy.md) — file layout rules for all
  recipes, regardless of language
- [Python language rules](./languages/python.md) — starts with the
  fast path; specific requirements and end-to-end scenarios
- [Repo skills catalog](./skills-catalog.md) — the assistant
  helpers that build this repo

**Updating an existing recipe?** Run `prepare-python-recipe`
against your recipe path — it's safe to re-run and applies
any new requirements automatically. Then check the
[checklist](../recipe-checklist.md) for any manual steps.

**Reference:**

- [Repo skills catalog](./skills-catalog.md) — the assistant
  helpers that do the work for you
- [Repo oracle](./skills-catalog.md#repo-oracle) — ask how the repo
  itself works: policy, CI, ownership, process
- [Troubleshooting](./troubleshooting.md) — errors mapped
  directly to fixes
- Other languages *(coming soon)*: Java · Go · TypeScript ·
  Kotlin

## Glossary

- **Recipe** — a runnable agent example (or importable agent
  module) under `contrib/`, consumed by ADK developers and coding
  agents alike.
- **Repo skill** — a pre-loaded instruction set that your AI coding
  assistant follows when you ask it to perform a task (e.g.
  `prepare-python-recipe`). Files live in `.agents/skills/` and load
  automatically when you open this repo. Repo skills *build* the
  repo; they are never shipped to users.
- **Vertical skill** — a recipe under
  `skills/<vertical>/<solution>/` (e.g. `skills/retail/store-ops/`),
  where the vertical names the business domain that owns it.
  Shipped to users like any other recipe. Unrelated to repo skills,
  despite the shared word.
- **Manifest** — `manifest.yaml`. Declares recipe metadata:
  type, language, ownership, description.
- **Runnability test** — a smoke test that imports the agent module
  and asserts `root_agent is not None`. Required at
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

`contrib/` PRs are reviewed by the repository maintainers. If
your PR has had no activity for more than a week, leave a comment
on the PR to request a review.

A weekly sweep labels a quiet PR `stale`, then closes it if it
stays quiet. Any comment or push resets the clock. A close is
housekeeping, not rejection — your commits survive, the branch
outlives the close, and reopening takes one click. Ask a
maintainer for `keep-open` if a PR must stay open.

Issues follow the same pattern on a much longer clock, and close
as `not planned` — a record that nobody got to it, not that it
was fixed.

---

← [Checklist](../recipe-checklist.md) · [Handbook](./README.md)
