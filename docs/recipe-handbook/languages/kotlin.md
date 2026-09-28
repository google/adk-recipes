<!-- word count: 101 (target 100, cap 200) -->

# Kotlin Recipes

`core/kotlin/llm-auditor` is the first Kotlin recipe. There are no
Kotlin authoring skills or `pyproject`-equivalent checks yet.

**If you want to contribute one:** open a GitHub issue at
[github.com/google/adk-recipes/issues](https://github.com/google/adk-recipes/issues)
first so we can align on build tool (Gradle / Maven), test
runner, and JVM target before you invest the work. Once accepted,
this page will mirror the shape of the [Python page](./python.md).

Structural checks (folder name, size limits, `manifest.yaml`,
agent layout) apply to Kotlin recipes today — you can submit a
working `contrib/kotlin/` recipe against those alone.

**Layout** (enforced for `core/` and `contrib/`): agent code in
`src/main/kotlin/com/google/adk/recipes/<name>/`, with the root
agent `rootAgent` defined in `Agent.kt` there (`<name>` is the
folder name without hyphens).

---

← [Checklist](../../recipe-checklist.md) · [Handbook](../README.md)
