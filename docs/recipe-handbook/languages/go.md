<!-- word count: 102 (target 100, cap 200) -->

# Go Recipes

Not currently active. No Go recipes have landed yet and no
language-specific tooling (skills, CI, `pyproject`-equivalent
checks) is in place.

**If you want to contribute one:** open a GitHub issue at
[github.com/google/adk-recipes/issues](https://github.com/google/adk-recipes/issues)
first so we can align on module layout, test runner, and CI
expectations before you invest the work. Once accepted, this page
will mirror the shape of the [Python page](./python.md).

Structural checks (folder name, size limits, `manifest.yaml`,
agent layout) apply to Go recipes today — you can submit a working
`contrib/go/` recipe against those alone.

**Layout** (enforced for `core/` and `contrib/`): agent code in
`app/`, with the root agent `RootAgent` defined in `app/agent.go`.

---

← [Checklist](../../recipe-checklist.md) · [Handbook](../README.md)
