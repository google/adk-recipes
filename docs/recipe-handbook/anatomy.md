# Anatomy of a Recipe

The shape shared by every ADK recipe in this repo, regardless
of root or language. Language-specific detail lives in
[Python](./languages/python.md), [Go](./languages/go.md),
[Java](./languages/java.md), [Kotlin](./languages/kotlin.md), and
[TypeScript](./languages/typescript.md).

## Where a recipe lives

Recipes live in one of three roots:

- `core/<lang>/<name>` — curated by the `agents-cli` team.
- `contrib/<lang>/<name>` — community contributions.
- `plugins/<vertical>/<solution>` — domain-vertical solutions
  (language is set in `manifest.language`).

Contributors submit new recipes to `contrib/`.

Every recipe must include `manifest.yaml`, `README.md`, and `.env.example` at
its root. Root-specific requirements:

- `core/` — requires `AGENTS.md` (intent, key files to study, reuse notes).
- `contrib/` — requires a root `Dockerfile` and `deployable: true` in
  `manifest.yaml`.
- `plugins/` — requires `SKILL.md`, `EVAL.yaml`, and `scripts/`.

See the full rule matrix in
[Required file or directory missing](./troubleshooting.md#required-file-or-directory-missing).

## Naming

- Max 30 characters.
- Lowercase letters and hyphens only, starts with a letter
  (`^[a-z][a-z-]*$`).

## Size limits

| Root | Default (max files / max size) | `large: true` (max files / max size) |
|---|---|---|
| `contrib/` | 70 / 2 MB | 200 / 10 MB |
| `plugins/` | 70 / 2 MB | 200 / 10 MB |
| `core/` | 500 / 50 MB | 10,000 / 10 GB |

Set `large: true` in `manifest.yaml` to opt into the relaxed tier.

**Excluded from the count:** generated files and caches. Common
exclusions:

- `uv.lock`, `__pycache__/`, `.venv/` (Python)
- `node_modules/`, lockfiles, `dist/` (TypeScript)
- `target/`, `build/`, `.gradle/` (Java, Kotlin)
- `vendor/`, `go.sum` (Go)

**Images:** a single unoptimized PNG screenshot can consume the whole
`contrib/` budget. Use WebP (`cwebp -q 85`) for anything only linked from
docs. Keep the original format when application code depends on it (an
import, build asset, or hardcoded path/MIME type).

## `manifest.yaml`

Every recipe has one. It declares the recipe's type, language,
status, description and owners. Generate it with the
`generate-manifest` repo skill. The [manifest](./manifest.md) page
lists every field and the rules CI enforces.

Example minimum:

```yaml
type: standalone
status: active
language: python
deployable: true
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

CI (`tools/validate_readme.py`) enforces the following content checks:

- No `TODO:` placeholders.
- At least 100 words (description proxy).
- A setup section — a heading containing one of (case-insensitive): `Setup`,
  `Prerequisites`, `Prerequisite`, `Installation`, `Install`, `Requirements`,
  `Requirement`, `Configuration`, `Getting Started`, `Before You Begin`,
  `Environment`.
- A run section — a heading containing one of (case-insensitive): `Run`,
  `Running`, `Usage`, `Quickstart`, `Quick Start`, `Start`, `Deploy`,
  `Deployment`, `How to Run`, `How to Use`, `Launch`, `Launching` — plus at
  least one fenced code block.

Run `uv run validate readme <recipe-path>` locally to check
before opening a PR.

## See also

- **Language-specific files** (Python's `pyproject.toml`,
  `uv.lock`, `.env.example`, `tests/test_runnability.py`) — see
  [Python](./languages/python.md).

---

← [Docs home](../README.md) · [Checklist](../recipe-checklist.md) · [Handbook](./README.md)
