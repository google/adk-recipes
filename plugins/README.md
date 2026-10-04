# `plugins/`

Installable industry and domain plugins following the
[Google Agent Plugins v1.0.0 specification](https://agent-plugins.org/specification).

## Purpose

Each folder under `plugins/<plugin-name>/` is a spec-compliant plugin bundle
(`plugin.json` + `skills/<skill-name>/SKILL.md`) that teaches AI coding
assistants (Gemini CLI, Claude Code, Antigravity, Codex, …) how to provision
Google Cloud infrastructure, scaffold an ADK agent, evaluate it, and export a
runnable application inside any developer workspace.

If you are looking for standalone, container-ready ADK + FastAPI + A2A agents
that deploy directly to **Vertex AI Agent Engine**, **Gemini Enterprise**,
**Agent Garden**, or **Cloud Run**, see the companion recipes in
[`contrib/`](../contrib/).

## Available Plugins

| Plugin | Included Skills | Companion Deployable Agents (`contrib/`) |
|---|---|---|
| [`retail/`](./retail/) | [`retail-product-search`](./retail/skills/product-search/) (Vector Search + BigQuery semantic catalog RAG)<br>[`retail-virtual-tryon`](./retail/skills/virtual-tryon/) (Gemini image + Veo catwalk video virtual try-on) | [`contrib/python/retail-product-search`](../contrib/python/retail-product-search/)<br>[`contrib/python/retail-virtual-tryon`](../contrib/python/retail-virtual-tryon/) |

## Installing a Skill

Install any skill directly from GitHub via `npx skills add`:

```bash
npx skills add google/adk-recipes --skill retail-product-search
npx skills add google/adk-recipes --skill retail-virtual-tryon
```

Or from a local clone of a plugin directory:

```bash
cd plugins/retail
make surface
```

## Contributing a Plugin

See [Plugin Layout and Specification](../docs/recipe-handbook/plugins.md) and
the [recipe checklist](../docs/recipe-checklist.md) for structure, schema, and
validation rules.
