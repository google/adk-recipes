# Kotlin Recipes

Kotlin recipes live under `core/kotlin/` and `contrib/kotlin/`. CI runs
formatting, linting, and unit tests on Kotlin recipes. There are no Kotlin
authoring repo skills yet.

**Before contributing a new recipe:** open a
[Propose a New Recipe](https://github.com/google/adk-recipes/issues/new?template=propose-a-new-recipe.md)
issue first. Universal layout and size rules live in
[anatomy](../anatomy.md).

Every Kotlin recipe must include `manifest.yaml`, `README.md`, `.env.example`,
and `build.gradle.kts`, plus `Dockerfile` (`deployable: true`) under `contrib/`
or `AGENTS.md` under `core/`. No lockfile is required.

---

← [Docs home](../../README.md) · [Checklist](../../recipe-checklist.md) · [Handbook](../README.md) · [Anatomy](../anatomy.md)
