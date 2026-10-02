# Retail Plugin

Retail AI plugin bundling two installable skills for Google Cloud:
semantic **product search** and generative image/video **virtual try-on**.
Follows the [Google Agent Plugins v1.0.0 spec][spec] (`plugin.json` +
nested `skills/`).

Owner: `FDE/Blackbelt` · POC: `tanvisinghal-0105` · License: Apache-2.0

## Skills

| Skill | What it builds | Google Cloud services |
|---|---|---|
| [`retail-product-search`](./skills/product-search/) | Semantic product catalog search / RAG agent for e-commerce, shopping assistants, and catalog discovery. | Vector Search (Gemini Enterprise Agent Platform), BigQuery, embeddings |
| [`retail-virtual-tryon`](./skills/virtual-tryon/) | Virtual try-on agent (image + catwalk video) for clothing, eyewear, jewelry, cosmetics, footwear. Ships a pre-flight product-cutout classifier and configurable safety levels. | Gemini image models, Veo |

Each skill folder contains a `SKILL.md` (the conversational installer
prompt read by the host assistant) and a `README.md` (human-oriented
install and surfacing instructions).

## Install

Install directly into an AI coding assistant (Claude Code, Antigravity,
Codex, Gemini CLI, …):

```bash
npx skills add google/adk-recipes --skill retail-product-search
npx skills add google/adk-recipes --skill retail-virtual-tryon
```

Or, from a cloned copy of this repo, use the per-skill `make surface`
target to install into `~/.gemini/skills` and/or `~/.agents/skills`.
See each skill's `README.md` for the full surfacing matrix.

## Deployable companions

The skills bootstrap a working agent locally. If you want the
production-shaped standalone agent — ready for Agent Engine, Cloud Run,
Gemini Enterprise App, and Agent Garden — use the companion `contrib/`
recipes:

- [`contrib/python/retail-product-search`](../../contrib/python/retail-product-search/)
- [`contrib/python/retail-virtual-tryon`](../../contrib/python/retail-virtual-tryon/)

## Layout

```
plugins/retail/
├── plugin.json                     # plugin manifest (v1.0.0 spec)
└── skills/
    ├── product-search/
    │   ├── SKILL.md                # installer prompt (Q-MODE)
    │   ├── README.md               # human install docs
    │   ├── EVAL.yaml               # LLM-as-judge + deterministic checks
    │   └── …                       # scripts, references, assets
    └── virtual-tryon/
        └── …                       # same shape
```

## Related

- [Plugin layout and specification](../../docs/recipe-handbook/plugins.md)
- [Agent Plugins v1.0.0 spec][spec]

[spec]: https://agent-plugins.org/specification
