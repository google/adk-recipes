# Go Recipes

Go recipes live under `core/go/` and `contrib/go/`. CI runs formatting,
linting, and unit tests on Go recipes. There are no Go authoring repo skills
yet.

**Before contributing a new recipe:** open a
[Propose a New Recipe](https://github.com/google/adk-recipes/issues/new?template=propose-a-new-recipe.md)
issue first. Universal layout and size rules live in
[anatomy](../anatomy.md).

Every Go recipe must include `manifest.yaml`, `README.md`, `.env.example`, and
`go.mod` (`go.sum` is only required when the module has external dependencies),
plus `Dockerfile` (`deployable: true`) under `contrib/` or `AGENTS.md` under
`core/`.

---

← [Docs home](../../README.md) · [Checklist](../../recipe-checklist.md) · [Handbook](../README.md) · [Anatomy](../anatomy.md)
