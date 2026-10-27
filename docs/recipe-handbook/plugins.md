# Plugin Layout and Specification

This page covers the structure and layout requirements for plugins under `plugins/`.

> Not to be confused with **repo skills** under `.agents/skills/`. Repo skills are
> AI coding assistant helpers used to build this repository; plugins are vertical
> solutions shipped to users.

## Layouts During Migration

During the migration window, the repository accepts two plugin layouts:

### 1. Spec-Compliant Layout (Google Agent Plugins v1.0.0)

The target end state follows the [Agent Plugins v1.0.0 Specification](https://agent-plugins.org/specification)
and its schema at [https://agent-plugins.org/schemas/1.0.0/plugin.schema.json](https://agent-plugins.org/schemas/1.0.0/plugin.schema.json):

```
plugins/<plugin-name>/
├── plugin.json                                    ← plugin manifest
├── skills/
│   └── <skill-name>/
│       ├── SKILL.md                               ← skill documentation and prompt
│       ├── scripts/                               (optional, executable scripts)
│       └── references/                            (optional, reference docs)
├── mcp.json                                       (optional, MCP server definitions)
└── com.<reverse-domain>/                          (optional, per-client extensions)
```

**Requirements:**
- `plugin.json` sits at `plugins/<plugin-name>/plugin.json` and conforms to `.github/schemas/plugin-schema.json`.
- Required manifest fields: `$schema`, `name`, `ownership` (`team` and `poc`).
- One or more skills nested under `skills/<skill-name>/SKILL.md`.
- `SKILL.md` must start with YAML front-matter containing `name` and `description` (plus optional `metadata`).
- Size limits apply to the plugin container root (`plugins/<plugin-name>`).

### 2. Legacy Layout (Single-Skill Vertical)

The legacy layout inherited from earlier conventions:

```
plugins/<vertical>/<solution>/
├── manifest.yaml                                  ← recipe manifest
├── SKILL.md                                       ← conversational installer
├── EVAL.yaml                                      ← eval rubrics
└── scripts/                                       ← runnable scripts
```

**Requirements:**
- `manifest.yaml` sits at `plugins/<vertical>/<solution>/manifest.yaml`.
- The vertical namespace (`retail/`, `hr/`, `finance/`) is mandatory.

## Migration Rules

- A directory under `plugins/` must use either the legacy or the spec-compliant layout.
- **Mixing layouts is an error:** placing both `manifest.yaml` and `plugin.json` in the same plugin directory fails validation.
- Once existing plugins migrate, legacy layout support will be deprecated and eventually removed.

---

← [Anatomy](./anatomy.md) · [Handbook](./README.md) · [Skills Catalog](./skills-catalog.md)
