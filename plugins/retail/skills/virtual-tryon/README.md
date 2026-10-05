# Retail Virtual Try-On (Skill Plugin)

Virtual try-on skill plugin using Gemini image generation (flash/pro tiers) and Veo on Gemini Enterprise Agent Platform for clothing, eyewear, jewelry, cosmetics, and footwear. Includes a pre-flight product-cutout classifier and configurable safety levels.

> **Looking for the standalone deployable cloud agent (Agent Engine, Gemini Enterprise App, Cloud Run, & Agent Garden)?**
> See [`contrib/python/retail-virtual-tryon`](../../../../contrib/python/retail-virtual-tryon/README.md).

## Skill Surfacing Personas & Boolean Config

This plugin provides boolean configuration flags (`SURFACE_GEMINI_SKILL`, `SURFACE_AGENTS_SKILL`) in [`assets/design-spec.md`](assets/design-spec.md), [`.env.example`](.env.example), and [`Makefile`](Makefile) so you can choose which skill surfaces to enable:

| Persona / Surface | Boolean Flag | Default | What It Does |
|---|---|---|---|
| **Gemini Enterprise Desktop & Gemini CLI** | `SURFACE_GEMINI_SKILL` (`surface_gemini_skill`) | `true` | Installs a clean skill folder to `~/.gemini/skills/retail-virtual-tryon` and symlinks `~/.gemini/config/skills/retail-virtual-tryon` |
| **AI Coding Assistants (`.agents/skills`)** (ADK, Claude Code, Antigravity) | `SURFACE_AGENTS_SKILL` (`surface_agents_skill`) | `true` | Symlinks the skill into `~/.agents/skills/retail-virtual-tryon` |

## Install

### Option 1: Install via Boolean Config (`make surface`)

Toggle whichever skill personas you want to surface:

```bash
# Default: surface to both ~/.gemini/skills and ~/.agents/skills
make surface

# Surface only to Gemini Enterprise Desktop / Gemini CLI (~/.gemini/skills):
make surface SURFACE_GEMINI_SKILL=true SURFACE_AGENTS_SKILL=false

# Surface only to ~/.agents/skills:
make surface SURFACE_GEMINI_SKILL=false SURFACE_AGENTS_SKILL=true
```

### Option 2: Install via `npx skills add`

Install directly into your AI coding assistant (Claude Code, Antigravity,
Codex, ...) via `npx skills add`:

```bash
npx skills add google/adk-recipes --skill retail-virtual-tryon
```

### Option 3: Developer Install

```bash
git clone https://github.com/google/adk-recipes.git
cd adk-recipes/plugins/retail/skills/virtual-tryon
uv sync
```

## Prerequisites

- Python 3.11+
- A Google Cloud project with billing enabled
- [`gcloud` CLI](https://cloud.google.com/sdk/docs/install) with ADC configured
  (`gcloud auth application-default login`)
- Gemini Enterprise Agent Platform and Cloud Storage APIs enabled —
  `scripts/setup_tryon.py` enables these for you
- Model access in your project: `gemini-2.5-flash-image` (try-on) and
  `gemini-3.5-flash` (agent and catalog classifier)
- For catwalk video only: access to Veo (`veo-3.1-generate-001`)

## Run Each Persona

### Persona 1: Gemini Enterprise Desktop App / Gemini CLI

1. Run `make surface SURFACE_GEMINI_SKILL=true`.
2. In your Gemini Enterprise Desktop app, open **Settings → General → Skills Folders**, add `~/.gemini/skills`, and check the **Skills** tab (`retail-virtual-tryon` appears automatically).
3. In chat, prompt:
   ```
   Use the retail-virtual-tryon skill to set up a virtual try-on app on Google Cloud.
   ```

### Persona 2: AI Coding Assistants (Claude Code / Antigravity / Gemini CLI)

In a fresh workspace, launch your AI coding agent and trigger the skill.

**Claude Code:**

```
/retail-virtual-tryon
```

**Antigravity / Gemini CLI:**

```
Use the retail-virtual-tryon skill to set up a virtual try-on app on Google Cloud.
```

The agent walks Q-MODE (4-5 questions Quick / 4 questions Export), runs
`scripts/bootstrap.sh` to create the venv, then `scripts/setup.py` to
provision GCS buckets, verify Gemini Enterprise Agent Platform access,
and launch the local sandbox at [http://localhost:8080](http://localhost:8080).

### Persona 3: Direct CLI (No Coding Assistant)

```bash
uv sync                                       # or: pip install -e .
uv run python scripts/setup_tryon.py --config assets/design-spec.md
# or pick a model directly:
uv run python scripts/setup_tryon.py --project-id $PROJECT --model flash
uv run python scripts/setup_tryon.py --project-id $PROJECT --model pro
```

### Persona 4: Managed Cloud Agent (Agent Engine, Gemini Enterprise, Cloud Run, Agent Garden)

To deploy and surface the standalone ADK agent to **Gemini Enterprise Agent Platform (Agent Engine)**, **Gemini Enterprise (GE) App**, **Cloud Run**, or **Agent Garden**, use the companion agent recipe in [`contrib/python/retail-virtual-tryon`](../../../../contrib/python/retail-virtual-tryon/README.md):

```bash
cd ../../../../contrib/python/retail-virtual-tryon
make surface DEPLOY_AGENT_ENGINE=true PUBLISH_GEMINI_ENTERPRISE=true PUBLISH_AGENT_GARDEN=true DEPLOY_CLOUD_RUN=false
```

## Export Standalone Sandbox Web App to Cloud Run

The interactive VTO sandbox is a FastAPI app that can also be exported as a standalone web application.
`scripts/export_app.py` generates a standalone codebase — it copies the
backend modules and UI assets, rewrites the `scripts.*` imports for a flat
layout, renders `Dockerfile`, `cloudbuild.yaml` and `deploy_cloudrun.sh`
with your project values baked in, and generates the container's
`requirements.txt` from the `serving` extra in `pyproject.toml`.

Through the agent, this is Q-MODE option 2 ("Export Web App & GCS Catalog
Sync"). Directly:

```bash
# 1. Set gcp_project_id (and optionally export_directory) in the design spec
uv run python scripts/export_app.py \
  --config assets/design-spec.md \
  --skill-dir .

# 2. Deploy the generated app (defaults to ./vto-retail-app)
cd vto-retail-app
./deploy_cloudrun.sh
```

`deploy_cloudrun.sh` enables the Cloud Run and Cloud Build APIs, then runs
`gcloud run deploy vto-retail-app --source .` — Cloud Build builds the
Dockerfile and Cloud Run serves it on port 8080 with
`uvicorn server:app`. Use the generated `cloudbuild.yaml` instead if you want
a CI pipeline that tags images by commit SHA.

Notes:

- The container runs as a non-root user and reads its configuration from
  `ENV` values written into the Dockerfile at export time, so it does not
  need a `.env` file.
- `catalog_images/` is excluded by `.dockerignore` in the exported app; the app recreates the
  directory at startup and serves catalog images from your GCS bucket. Set
  `tryon_catalog_upload: true` in the design spec so the catalog is synced.
- The Cloud Run service account needs `roles/aiplatform.user` and
  `roles/storage.objectAdmin`, the same roles `setup_tryon.py` grants for
  local runs.
- `deploy_cloudrun.sh` passes `--allow-unauthenticated`. Remove that flag if
  the service should not be publicly reachable.

## Model tiers

| Label | Model ID | Best for |
|-------|----------|----------|
| `flash` (default) | `gemini-2.5-flash-image` | High-volume, cost-sensitive |
| `pro` | `gemini-2.5-pro-image` | Luxury, editorial |

## Skill

See [SKILL.md](SKILL.md) for the conversational agent guide.

## Try it

Open [http://localhost:8080](http://localhost:8080) once the sandbox says
`Application startup complete`. The demo catalog ships with two products
(`shirt_001`, `sunglasses_001`) and one sample user photo.

**Image try-on flow:**

1. Click **Upload Photo** and pick a portrait-orientation photo of yourself,
   or use `catalog_images/sample_user.jpg`
2. Click a product card (e.g. `shirt_001`) to select it
3. Click **Try On Image**
4. First call takes 15-30s (Gemini Enterprise Agent Platform cold start);
   subsequent calls under 10s

**Video (catwalk) try-on flow:**

1. Run the image try-on above first — video needs the composite image
2. Once the composite renders, click **Generate Catwalk**
3. Veo takes 30-60s to generate a 5-second video; be patient

**Test your own catalog:**

Type a local folder path or `gs://` URI into the catalog search header and
click **Scan**. Gemini classifies each image (`Clothing` / `Eyewear` /
`Jewelry` / `Footwear` / `Other`) and writes a `catalog.json` manifest. On
a rescan, cached results are used unless you tick **Force reindex**.

**Failure modes to watch for:**

- `404 NOT_FOUND: Publisher model .../gemini-<X>-flash-image was not found` →
  the model name isn't real in Gemini Enterprise Agent Platform. Only
  `gemini-2.5-flash-image` and `gemini-2.5-pro-image` exist today. Check
  `GEMINI_IMAGE_MODEL` in your environment.
- Image renders but face looks distorted → the Veo reference-image cropping
  in `tryon_processor.py` didn't get a clean face crop. Retry with a
  higher-resolution portrait photo.
- Video hangs at "Generating catwalk..." for over 90s → Veo 3.1 quota
  exhausted or the model is under load. Check the sandbox log for a `503`
  or `429` from `veo-3.1-generate-001`.

## License

Apache 2.0
