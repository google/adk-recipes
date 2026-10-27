# TypeScript Recipes

TypeScript recipes live under `core/typescript/` and `contrib/typescript/`. CI
runs formatting, linting, and unit tests on TypeScript recipes. There are no
TypeScript authoring repo skills yet.

**Before contributing a new recipe:** open a
[Propose a New Recipe](https://github.com/google/adk-recipes/issues/new?template=propose-a-new-recipe.md)
issue first. Universal layout and size rules live in
[anatomy](../anatomy.md).

Every TypeScript recipe must include `manifest.yaml`, `README.md`,
`.env.example`, `package.json`, and one lockfile (`package-lock.json`,
`pnpm-lock.yaml`, `yarn.lock`, `bun.lockb`, or `bun.lock`), plus `Dockerfile`
(`deployable: true`) under `contrib/` or `AGENTS.md` under `core/`.

---

← [Docs home](../../README.md) · [Checklist](../../recipe-checklist.md) · [Handbook](../README.md) · [Anatomy](../anatomy.md)
