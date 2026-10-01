# Retail Virtual Try-On

Virtual try-on agent using Gemini image generation (flash/pro tiers) for clothing, eyewear, jewelry, cosmetics, and footwear. Includes a pre-flight product-cutout classifier and configurable safety levels.

## Supported Personas & Surfaces

This plugin supports both **AI Coding Assistant / Desktop Skill** workflows and **Managed Cloud Agent** deployments:

| Persona / Surface | How it is used | Entrypoint / Tooling |
|---|---|---|
| **AI Coding Assistants** (Claude Code, Antigravity, Gemini CLI, Codex) | Conversational skill that provisions GCS buckets, validates Gemini/Veo access, and launches the local VTO sandbox or exports a web app | `SKILL.md` (`npx skills add` or `make install-skill-local`) |
| **GE Desktop App** (Spark Beta, GoGo, Sobi) | Local skill in `~/.gemini/skills/retail-virtual-tryon` or synced to `google3` (`//third_party/skills/skills/`) | `make install-skill-local` / `make sync-google3-skill` |
| **Gemini Enterprise Agent Platform (Agent Engine)** | Managed ADK Reasoning Engine runtime (`try_on_product_image`, `try_on_product_video`) on Google Cloud | `make deploy-agent-engine` (`agents-cli deploy`) |
| **Gemini Enterprise (GE) App** | Registered ADK / A2A agent accessible in the Gemini Enterprise web/Sobi assistant | `make publish-gemini-enterprise` (`agents-cli publish`) |
| **Cloud Run & Agent Garden** | Containerized FastAPI + A2A + Reasoning Engine HTTP server (`deployable: true`) or standalone exported web app | `make deploy-cloudrun`, `Dockerfile`, `agents-cli-manifest.yaml`, `scripts/export_app.py` |

## Install

### 1. Skill Install (Coding Assistants & GE Desktop / Spark Beta)

Install directly into your AI coding assistant (Claude Code, Antigravity,
Codex, ...) via `npx skills add`. The tool discovers `SKILL.md` from this
recipe and registers `/retail-virtual-tryon` as an invocable skill:

```bash
npx skills add google/adk-recipes --skill retail-virtual-tryon
```

Or install locally across `~/.gemini/skills/retail-virtual-tryon` (validated for **Spark Beta**), `~/.gemini/config/skills`, and `~/.agents/skills`:

```bash
make install-skill-local
```

To sync the skill to a `google3` CitC client (`//third_party/skills/skills/retail-virtual-tryon`) for GoGo / Sobi:

```bash
make sync-google3-skill
# Or from Mac to a remote Cloudtop over SSH:
make sync-google3-skill CLOUDTOP_HOST=<your-cloudtop>.c.googlers.com
```

### 2. Developer / Agent Runtime Install

If you're contributing to the recipe or deploying the agent directly:

```bash
git clone https://github.com/google/adk-recipes.git
cd adk-recipes/plugins/retail/virtual-tryon
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

## Run

### Persona A: Run via Skill (Claude Code / Antigravity / Spark Beta)

In a fresh workspace, launch your AI coding agent and trigger the skill.

**Claude Code:**

```
/retail-virtual-tryon
```

**Antigravity / Spark Beta / Sobi:**

```
Use the retail-virtual-tryon skill to set up a virtual try-on app on Google Cloud.
```

The agent walks Q-MODE (4-5 questions Quick / 4 questions Export), runs
`scripts/bootstrap.sh` to create the venv, then `scripts/setup.py` to
provision GCS buckets, verify Gemini Enterprise Agent Platform access,
and launch the local sandbox at [http://localhost:8080](http://localhost:8080).

### Persona B: Direct CLI (No Coding Assistant)

```bash
uv sync                                       # or: pip install -e .
uv run python scripts/setup_tryon.py --config assets/design-spec.md
# or pick a model directly:
uv run python scripts/setup_tryon.py --project-id $PROJECT --model flash
uv run python scripts/setup_tryon.py --project-id $PROJECT --model pro
```

## Deploy to Agent Engine, Gemini Enterprise & Cloud Run

### Option 1: Managed Agent Runtime (Agent Engine + Gemini Enterprise + A2A Cloud Run)

Deploy the ADK agent (`scripts/agent.py` / `scripts/fast_api_app.py`) directly to Google Cloud using `google-agents-cli`:

1. **Deploy to Gemini Enterprise Agent Platform (Agent Engine):**
   ```bash
   make deploy-agent-engine PROJECT_ID=your-gcp-project-id REGION=us-central1
   ```
2. **Register with your Gemini Enterprise (GE) App:**
   ```bash
   make publish-gemini-enterprise \
     PROJECT_ID=your-gcp-project-id \
     GEMINI_ENTERPRISE_APP_ID="projects/<project-number>/locations/global/collections/default_collection/engines/<engine-id>" \
     AGENT_ENGINE_ID="projects/<project-number>/locations/us-central1/reasoningEngines/<reasoning-engine-id>"
   ```
3. **Deploy Agent Service to Cloud Run (A2A + Reasoning Engine HTTP API):**
   ```bash
   make deploy-cloudrun PROJECT_ID=your-gcp-project-id REGION=us-central1
   ```

### Option 2: Export & Deploy Standalone Web App to Cloud Run

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
