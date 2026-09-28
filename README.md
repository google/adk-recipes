# <img src="https://raw.githubusercontent.com/google/adk-docs/main/docs/assets/agent-development-kit.png" alt="Agent Development Kit Logo" width="30"> ADK Recipes

[![License](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)


Welcome. This is a public collection of ADK recipes — small,
runnable agents that show how to solve real problems with the
[Agent Development Kit](https://adk.dev). Fork one as the
starting point for your own project, or browse the collection to
learn the patterns.

Whether you're building a customer service bot, a research
agent, or something entirely new, the recipes here give you a
working foundation instead of a blank page.

## Try a recipe

Recipes live in two places:

- **[`core/`](./core/)** — canonical patterns curated by the
  `agents-cli` team. Small, focused recipes that teach one thing
  well (OAuth flows, session memory, guardrails, RAG patterns).
- **[`contrib/`](./contrib/)** — community-contributed recipes.
  Broader in scope; each one is a self-contained example for a
  specific use case or industry workflow.

Each recipe has its own `README.md` with setup and run
instructions.

## Prerequisites

Install ADK from the
[ADK Get Started guide](https://adk.dev/get-started). Language
SDKs:

- [ADK Python](https://github.com/google/adk-python)
- [ADK TypeScript](https://github.com/google/adk-js)
- [ADK Go](https://github.com/google/adk-go)
- [ADK Java](https://github.com/google/adk-java)
- [ADK Kotlin](https://github.com/google/adk-kotlin)

## Contributing a recipe

Start with the [Recipe Checklist](./docs/recipe-checklist.md) —
one page, everything you need to ship. If you want the full
context and tooling reference, the
[Recipe Handbook](./docs/recipe-handbook/README.md) has the deep
detail.

**Repo skills** — the AI coding-assistant helpers used to build this
repo (recipe scaffolding, manifest generation, pyproject alignment,
and more) — live in [`.agents/skills/`](./.agents/skills/). Not to be
confused with **vertical skills**, which are recipes shipped to users
under `skills/<vertical>/<solution>/`.

## Getting help

Open a GitHub issue at
[github.com/google/adk-recipes/issues](https://github.com/google/adk-recipes/issues).

## License

Apache 2.0 — see [LICENSE](./LICENSE).

## Disclaimer

Not an officially supported Google product. Not eligible for the
[Google OSS Vulnerability Rewards Program](https://bughunters.google.com/open-source-security).
Recipes are for demonstration and as starting points, not
production use.
