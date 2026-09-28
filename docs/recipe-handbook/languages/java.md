<!-- word count: 102 (target 100, cap 200) -->

# Java Recipes

Not currently active. No Java recipes have landed yet and no
language-specific tooling (skills, CI, `pyproject`-equivalent
checks) is in place.

**If you want to contribute one:** open a GitHub issue at
[github.com/google/adk-recipes/issues](https://github.com/google/adk-recipes/issues)
first so we can align on package manager, test runner, and file
layout before you invest the work. Once accepted, this page will
mirror the shape of the [Python page](./python.md).

Structural checks (folder name, size limits, `manifest.yaml`,
agent layout) apply to Java recipes today — you can submit a
working `contrib/java/` recipe against those alone.

**Layout** (enforced for `core/` and `contrib/`): agent code in
`src/main/java/com/google/adk/recipes/<name>/`, with the root
agent `ROOT_AGENT` defined in `Agent.java` there (`<name>` is the
folder name without hyphens).

---

← [Checklist](../../recipe-checklist.md) · [Handbook](../README.md)
