# Java Recipes

`contrib/java/financial-advisor` and `contrib/java/time-series-forecasting`
are Java recipes in this repository. CI runs format/lint (`java-format.yml`)
and unit tests (`java-tests.yml`). There are no Java authoring repo skills yet.

**Before contributing a new recipe:** open a
[Propose a New Recipe](https://github.com/google/adk-recipes/issues/new?template=propose-a-new-recipe.md)
issue first. Universal layout and size rules live in
[anatomy](../anatomy.md).

Every Java recipe must include `manifest.yaml`, `README.md`, `.env.example`,
and one build configuration file (`pom.xml`, `build.gradle`, or
`build.gradle.kts`), plus `Dockerfile` (`deployable: true`) under `contrib/`
or `AGENTS.md` under `core/`. No lockfile is required.

---

← [Docs home](../../README.md) · [Checklist](../../recipe-checklist.md) · [Handbook](../README.md) · [Anatomy](../anatomy.md)
