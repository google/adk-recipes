<div align="center">
  <img src="assets/long-horizon-banner.webp" alt="An observatory on a hillside at sunset, an agent graph glowing on a table, a long valley beyond" width="820">
  <h1>Long Horizon</h1>
  <h3>A reference implementation of an agent harness on ADK and Google's Agent Platform.</h3>
</div>

This recipe shows how to build a long-horizon harness on ADK with capabilities like cross-session memory, a per-user sandbox, tool guardrails, sub-agents, and a self-improvement loop. Read it, then lift the patterns into your own agent.

> **Not an officially supported Google product** — recipe code for demonstration only.

- **Study the components** — [`AGENTS.md`](AGENTS.md), where each row links to the function to start from
- **Run it yourself** — [Quickstart](#quickstart)
- **Build your own** — [hand it to a coding agent](#build-your-own-with-a-coding-agent)
- **Understand the design** — [`docs/architecture.md`](docs/architecture.md)
- **Review the security model** — [`docs/security-model.md`](docs/security-model.md), before pointing it at anything real

<div align="center">
  <img src="https://raw.githubusercontent.com/google/adk-recipes/assets/long-horizon-harness/horizon-demo.gif" alt="The Horizon web UI running two tasks: an AI research digest and a BigQuery analysis, each streaming tool calls and ending in a rendered HTML artifact" width="820">
</div>

**Features:**

*Memory & self-improvement*

- **Cross-session memory** — recalls facts and preferences across conversations, prefetched every turn from Memory Bank via `PreloadMemoryTool`.
- **Background self-improvement** — the agent gets better the more you use it: it saves what's worth remembering to memory, and occasionally learns a new technique, without slowing down the reply to the user.
- **It dreams** — nightly, a cross-session pass surfaces what no single conversation can (mention it Monday, it's surfaced by Friday) and consolidates scattered memories into one profile. See [`docs/memory.md`](docs/memory.md).

*Sandbox & secrets*

- **Per-user sandbox** — a JWT-routed BYOC [Sandbox](https://docs.cloud.google.com/gemini-enterprise-agent-platform/scale/sandbox), warm between turns and reattached on the next message (snapshot survival opt-in). See [`docs/sandbox-lifecycle.md`](docs/sandbox-lifecycle.md).
- **Bring-your-own secrets** — API keys live per-user in Secret Manager and inject into sandbox commands; the model sees the name, never the value. Bulk-import via `.env` upload.
- **Connect Google** — one-click OAuth wires `gcloud`/`bq` and the `gws` Workspace CLI into your sandbox; tokens are stored as per-user secrets, never shown to the model. Bring your own OAuth client — [setup](docs/quickstart.md#6-connect-google-optional-oauth).

*Integration & extensibility*

- **A2A-native** — any agent or script drives Horizon over A2A (agent-to-agent), and it streams structured UI parts back over the same channel. No custom frontend per feature.
- **Sub-agents** — one `subagent` tool, blocking by default or fire-and-forget with `background=True`, each with its own context window and toolset.
- **Skills & custom scripts** — drop a `SKILL.md` or a `scripts/<name>.py` in the workspace and `/reload` picks it up mid-session. No fork, no redeploy.
- **Scheduled chats** — a recurring routine fires as a real, persisted chat in a "Scheduled" folder, viewable and replayable in the web UI.

*Reliability & safety*

- **Resumability + compaction** — sessions resume cleanly; `HorizonSummarizer` compresses old turns so context stays focused on what's current.
- **Guardrails** — iteration-budget, no-progress, and repeated-failure halts share one `halt_reason` and reset together at the turn boundary.
- **Feedback capture** — users send structured feedback from the UI, persisted through `POST /feedback`.

## The stack

| Layer | What |
|---|---|
| **Runner** | ADK 2.5.x — plugins, callback graph, resumability, events compaction |
| **Model** | Gemini 3.6 Flash (root, default) · Flash Latest (web research) |
| **Memory** | [Memory Bank](https://docs.cloud.google.com/gemini-enterprise-agent-platform/scale/memory-bank) (managed store) **+** a custom self-improvement loop (throttled judge fork, pre-compaction flush, nightly dream-review consolidation) — see [`docs/memory.md`](docs/memory.md) |
| **Sessions** | [Agent Platform Sessions](https://docs.cloud.google.com/gemini-enterprise-agent-platform/scale/sessions) |
| **Sandbox** | [Sandboxes](https://docs.cloud.google.com/gemini-enterprise-agent-platform/scale/sandbox) (BYOC container) — one per user, JWT-routed; reattached between turns (snapshot survival opt-in) |
| **Frontend** | Vite 8 + TanStack Router/Query + React 18 |
| **Surface** | Cloud Run + Cloud SQL + Cloud Scheduler — nothing scales to zero: both Cloud Run services hold `min_instance_count = 1` (the every-minute tick can't pay a cold start), and Cloud SQL bills continuously |

On top of those managed primitives, Horizon adds a handful of **interfaces** — the rows in [`AGENTS.md`](AGENTS.md):

- the tool-guardrail chain
- the `Environment` ContextVar sandbox layer
- the per-user secret store
- the dynamic delegate + HITL resurfacing
- the three-tier system-prompt assembler
- the self-improvement loop over managed Memory Bank — throttled judge fork, pre-compaction flush, nightly dream-review (see [`docs/memory.md`](docs/memory.md))

Compaction, resumability, and prefix caching are ADK/Agent Platform knobs Horizon only configures; routines and the scheduler are applications composed from those interfaces.

Full subsystem walkthrough: [`docs/architecture.md`](docs/architecture.md).

---

## Quickstart

**Run it locally.** For the step-by-step, see [`docs/quickstart.md`](docs/quickstart.md).

### Prerequisites

[uv](https://docs.astral.sh/uv/getting-started/installation/), [google-agents-cli](https://pypi.org/project/google-agents-cli/) (`uvx google-agents-cli setup` — provides the `agents-cli` command), [Google Cloud SDK](https://cloud.google.com/sdk/docs/install), `make`, and Node.js 20.19+ or 22.12+ (Vite 8 — web UI only). On Windows, use WSL.

Plus a **GCP project with billing enabled**. These are one-time setup; skip any you've already done:

```bash
gcloud auth application-default login             # local credentials for inference
gcloud config set project YOUR_PROJECT_ID         # skip if your gcloud default is already the right project
gcloud services enable aiplatform.googleapis.com  # the API Agent Platform serves from
```

### Setup and run

```bash
# 1. Clone just this recipe
git clone --depth 1 --filter=blob:none --sparse https://github.com/google/adk-recipes.git
cd adk-recipes && git sparse-checkout set core/python/long-horizon-harness
cd core/python/long-horizon-harness

# 2. Run — the first run installs deps and seeds .env from .env.example
make dev-local
```

## Build your own with a coding agent

Run `uvx google-agents-cli setup`, then ask your coding agent:

> Using `agents-cli` and this reference —
> https://github.com/google/adk-recipes/tree/main/core/python/long-horizon-harness
> — help me build an agent that **&lt;does xyz&gt;**.

### Testing

Deterministic tests need no GCP; evals hit Agent Platform and are billed like any other call.

```bash
uv run pytest tests/unit tests/integration   # deterministic — no GCP needed
agents-cli eval run --dataset tests/eval/datasets/smoke-dataset.json \
  --config tests/eval/eval_config.yaml       # grades behavior, one suite at a time
uv run python scripts/eval_gate.py           # applies the 0.8 pass threshold
```

Three replay suites still run on `adk eval`; see
[`tests/eval/datasets/README.md`](tests/eval/datasets/README.md) for the split.

Full config reference: [`docs/configuration.md`](docs/configuration.md).


---

## Learn & adapt

Horizon is a recipe — **configure it with environment variables and adapt it by editing the code** (there's no wrapper API). Where to go next:

- **The custom interfaces** → [`AGENTS.md`](AGENTS.md) — each row links to the function that implements it.
- **Architecture** → [`docs/architecture.md`](docs/architecture.md) — the map + per-subsystem start-here files (and the runtime diagram).
- **Adapt / extend** → [`docs/extending.md`](docs/extending.md) — swap the model, add tools/routes, plug a custom sandbox, teach it skills.
- **Configuration** → [`docs/configuration.md`](docs/configuration.md) — every environment variable + dependency extra.
- **Security** → [`docs/security-model.md`](docs/security-model.md) — Horizon trusts the LLM within tool- and sandbox-level guardrails (identity, the guard chain, what each route exposes). Review it before pointing it at anything real.

## Deploy

`make deploy` is **one command** that provisions infrastructure *and* ships code, in three steps:

1. **Terraform** (`terraform/`) provisions the foundation (Cloud SQL, Secret Manager, IAM service accounts + roles, Cloud Scheduler jobs, IAP, GCS buckets, required-API enablement), plus the two Cloud Run **skeletons** (`lha` backend, `lha-web` IAP proxy) with placeholder images.
2. **`agents-cli deploy`** builds + rolls the real backend image onto `lha`.
3. **Cloud Build + `gcloud run deploy`** builds + rolls the web image onto `lha-web`.

Both services `ignore_changes` on their image, so steps 2–3 never fight Terraform. Prereqs: `terraform`, `agents-cli`, `gcloud`, and a project (`gcloud config set project <id>` or `PROJECT_ID=<id> make deploy`).

> **IAP access is empty by default.** Set `TF_VAR_iap_users='["user:you@example.com"]'` before `make deploy` (or re-run after) or you'll be locked out of the web UI.

**Billable, always-on resources** (Cloud SQL especially, plus one always-warm instance of each Cloud Run service) — tear everything down when you're done. Container images pushed to Artifact Registry (`cloud-run-source-deploy`) are not Terraform-managed and survive `make destroy`; delete them separately if you care about the storage:

```bash
make destroy   # flips the delete guards off, then terraform destroy of all the above
```

## Disclaimer

This repository is for demonstrative purposes only and is **not an officially supported Google product**. It is reference/recipe code provided **without warranty or support of any kind**; review, test, and secure it before any real use. Licensed under Apache 2.0.
