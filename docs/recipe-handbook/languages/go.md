<!-- word count: 114 (target 100, cap 200) -->

# Go Recipes

Not currently active. No Go recipes have landed yet and no
language-specific tooling (skills, CI, `pyproject`-equivalent
checks) is in place.

**If you want to contribute one:** open a GitHub issue at
[github.com/google/adk-recipes/issues](https://github.com/google/adk-recipes/issues)
first so we can align on module layout, test runner, and CI
expectations before you invest the work. Once accepted, this page
will mirror the shape of the [Python page](./python.md).

Structural checks apply to Go recipes today: every recipe must include
`manifest.yaml`, `README.md`, `.env.example`, and `go.mod` (every
recipe is its own module; `go.sum` is not required if there are no
external dependencies). You can submit a working `contrib/go/`
recipe against those alone.

---

← [Checklist](../../recipe-checklist.md) · [Handbook](../README.md)
