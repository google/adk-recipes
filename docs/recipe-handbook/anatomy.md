<!-- word count: 443 (target 800, cap 1200) -->

# Anatomy of a Recipe

The shape shared by every ADK recipe in this repo, regardless
of root or language. Language-specific detail lives in
[languages/](./languages/).

## Where a recipe lives

Every recipe lives at `<root>/<lang>/<name>`, where `<root>` is
`core/` (curated by the `agents-cli` team) or `contrib/`
(community). Nested by language.

Contributors submit new recipes to `contrib/`. The rest of this
page covers what all recipes share. `core/` recipes have one additional file — `AGENTS.md` — written
for coding agents: intent, key files to study, and reuse notes.
`contrib/` recipes don't need `AGENTS.md`, but every `contrib/`
recipe must be deployable, which means it must have a `Dockerfile`
at the recipe root and set `deployable: true` in `manifest.yaml`.

## Naming

- Max 30 characters.
- Lowercase letters and hyphens only, starts with a letter
  (`^[a-z][a-z-]*$`).

## Size limits

| Root | Max files | Max size |
|---|---|---|
| `contrib/` | 70 | 2 MB |

**Excluded from the count:** generated files and caches. Common
exclusions:

- `uv.lock`, `__pycache__/`, `.venv/` (Python)
- `node_modules/`, lockfiles, `dist/` (TypeScript)
- `target/`, `build/`, `.gradle/` (Java, Kotlin)
- `vendor/`, `go.sum` (Go)

**Images:** a single unoptimized PNG screenshot can consume the whole
`contrib/` budget. Use WebP (`cwebp -q 85`) for anything only linked from
docs. Leave the original format alone if application code depends on it —
an import, a build asset, a hardcoded path or MIME type — not just a doc
reference.

## `manifest.yaml`

Every recipe has one. It declares the recipe's type, language,
status, description and owners. Generate it with the
`generate-manifest` AI skill. The [manifest](./manifest.md) page
lists every field and the rules CI enforces.

Example minimum:

```yaml
type: standalone
status: active
language: python
description: A retrieval-augmented search agent over public docs.
ownership:
  team: your-team-name
  poc: your-github-username
```

## `README.md`

Every recipe has one. Cover:

1. What the recipe does (one paragraph).
2. Setup — prerequisites, credentials, environment variables.
3. Run — the exact command to start the agent.
4. Optional: architecture diagram, example prompts, screenshots (WebP —
   see [Size limits](#size-limits)).

CI enforces the following content checks:

- No `TODO:` placeholders.
- At least 100 words (description proxy).
- A setup section — a heading containing one of: `Setup`,
  `Prerequisites`, `Installation`, `Requirements`, `Configuration`,
  `Getting Started`, `Before You Begin`, `Environment`.
- A run section — a heading containing one of: `Run`, `Running`,
  `Usage`, `Quickstart`, `Start`, `Deploy`, `Launch`,
  `How to Run` — plus at least one fenced code block.

Run `uv run validate readme <recipe-path>` locally to check
before opening a PR.

## See also

- **Language-specific files** (Python's `pyproject.toml`,
  `uv.lock`, `.env.example`, `tests/test_runnability.py`) — see
  [languages/](./languages/).

---

← [Checklist](../recipe-checklist.md) · [Handbook](./README.md)
