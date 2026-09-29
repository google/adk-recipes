---
name: retail-virtual-tryon
description: >-
  Creates virtual try-on agents supporting image and video (catwalk animation) try-ons
  on Google Cloud (Gemini image models and Veo on Gemini Enterprise Agent Platform). Handles resource setup,
  user photo uploading, image/video generation pipelines, local testing, and evaluation.
metadata:
  author: Google
  license: Apache-2.0
  version: 0.2.0
  requires:
    pip:
      - google-genai>=1.0
      - google-cloud-storage>=2.0
      - pillow>=9.0
      - pyyaml>=6.0
    pip_optional:
      - google-adk>=2.2.0
    install_hint: "From inside the workspace (see Workspace Setup section), run: pip install -e \"$SKILL_DIR[adk]\" -- this installs the skill's pyproject.toml; the [adk] extra pulls in google-adk for the agent runtime"
---

# Virtual Try-On Agent

Creates virtual try-on (VTO) agents on Google Cloud supporting image and video (catwalk animation) try-on modes.

## STOP -- READ THIS BEFORE RESPONDING

**Check your operating mode first — there are two distinct modes:**

### Mode A: Deployed Try-On Agent
**If the system context tells you** that setup is complete (e.g. it says "You are a DEPLOYED try-on agent", or the conversation history shows setup has already been completed) —
**skip Q-MODE entirely.** Respond directly to the user's try-on query.
Do NOT output the Q-MODE block. Do NOT mention setup.

### Mode B: First-time Setup (default)
**If there is no such context** (fresh invocation, no prior setup) —
your VERY FIRST response MUST be the Q-MODE block below. Nothing else.

Do NOT ask about products, industry, GCP project, or anything else first.
Do NOT propose a plan. Do NOT explain what you will do.

Your first message to the user must be EXACTLY this (copy-paste, no changes):

```
[skill: retail-virtual-tryon] active.
Q-MODE: Pick a setup mode? [default: 1]
  1. Quick start -- Local testing sandbox, interactive 6-question config, ~90s. Best for demos.
  2. Export Web App & GCS Catalog Sync -- Generate standalone containerized codebase, GCS catalog sync, and Cloud Run config, ~3 min.
```

Then STOP and wait for the user's answer.

Accept: `1`, `quick`, empty/Enter (= Quick Start), `2`, `export`, `sync` or `webapp` (= Export Web App & GCS Catalog Sync).

## Execution Rules

1. **Q-MODE first, always.** No exceptions. No preamble.
   - **CRITICAL WARNING**: Do NOT automatically run setup or deployment scripts (e.g. `setup_tryon.py`, `export_app.py`, `deploy_cloudrun.sh`) upon receiving a general request like "I want to create/deploy a VTO app on GCP". You MUST first present the Q-MODE setup menu choice and wait for the user to select Mode 1, 2, or 3.
2. **One question at a time. Show the default. Accept empty input.**
   Format: `Q: <question text>? [default: <value>]`
   Pressing Enter = use the default. **NEVER ask multiple questions in one turn.**
3. **Execute steps in order.** Do NOT jump ahead or skip steps.
4. **Verify each step succeeded** before moving to the next.
5. **Save all answers to `./design-spec.md`** (in the workspace) as you collect them.
   After the interview, run `.venv/bin/python "$SKILL_DIR/scripts/setup.py" --config ./design-spec.md`
   (see the Workspace Setup section to resolve `$SKILL_DIR`).
6. **Confirm completion** of each step before proceeding.

**Before Quick Start has launched setup, the user can say "export", "webapp",
or "deploy" to switch to Mode 2 (Export Web App & GCS Catalog Sync).** Carry
over answers already given for project, mode, and region; ask only the
remaining Mode-2 questions (GCS catalog bucket, export directory). After
setup.py has already started buckets/APIs, the workflow is committed -- to run
Mode 2 instead, start a fresh workspace.

## Workspace Setup

The skill has two locations:
- **Install dir** -- where SKILL.md and scripts live (varies by host)
- **Workspace** -- the agent's cwd; design-spec.md, .venv, and per-run state live here

By the end of this section the workspace must have `.venv/` (with the skill
installed editable + `[adk]` extras), `design-spec.md`, and `SKILL_DIR`
exported in the shell.

Run this as ONE shell command -- splitting it across tool calls loses state:

```bash
SKILL_DIR=$(for d in ~/.claude/skills ~/.agents/skills ~/.gemini/skills ~/.cursor/skills; do
  [ -f "$d/retail-virtual-tryon/SKILL.md" ] && echo "$d/retail-virtual-tryon" && break
done)
bash "$SKILL_DIR/scripts/bootstrap.sh"
```

`bootstrap.sh` finds a Python 3.10+ interpreter (with absolute-path fallback
for sandboxed shells), creates `.venv`, installs the skill editable with the
`[adk]` extras, and copies `design-spec.md` into the workspace.

All scripts run from the install dir against the workspace config. **Use
`.venv/bin/python`, not bare `python`** -- bare `python` may resolve to a
Python without the skill's editable install on sys.path.

```bash
.venv/bin/python "$SKILL_DIR/scripts/setup.py" --config ./design-spec.md
```

Edit `./design-spec.md` and set `gcp_project_id` (the agent will do this based on the user's answers in Q-MODE).

## Mode 1: Quick start (4-5 questions)

| Q | Question | Default | Source |
|---|---|---|---|
| Q-A | GCP project ID? | `$GOOGLE_CLOUD_PROJECT` or `gcloud config get-value project` | env / gcloud |
| Q-B | Try-on mode? | `2` (both Image + Veo Video) | prompt |
| Q-C | GCP Region? | `us-west1` | prompt |
| Q-D | Catalog Path? | `demo` (type 'demo' to use bundled catalog, or specify local folder, or gs:// URI) | prompt |
| Q-E | Upload local catalog to GCS? *(Only asked if Q-D is a custom local folder)* | `1` (Yes) | prompt |

### Question Formats & Accepted Choices:

- **Q-B: Try-on mode?**
  Format to print:
  ```
  Q: Try-on mode? [default: 2]
    1. image_only (Faster, static images only)
    2. image_and_video (Catwalk video animations via Veo)
  ```
  Accept: `1` (= `image_only`), `2` (= `image_and_video`), `image_only`, `image_and_video`.

- **Q-D: Catalog Path?**
  Accept: `demo` (uses bundled catalog), local directory path, or GCS URI starting with `gs://`.

- **Q-E: Upload local catalog to GCS?**
  Format to print:
  ```
  Q: Upload local catalog to GCS? [default: 1]
    1. Yes (Sync and host catalog in GCS)
    2. No (Run locally using local folder assets)
  ```
  Accept: `1` (= `Yes`), `2` (= `No`), `Yes`, `No`.

After collecting these answers, do this **automatically**:

1. Write the answers to `./design-spec.md` (in the workspace), filling in:
   - `gcp_project_id`
   - `tryon_mode`
   - `tryon_model` (always use `flash`)
   - `tryon_output_bucket` (default: `{project_id}-tryon-output`)
   - `tryon_upload_bucket` (default: `{project_id}-tryon-uploads`)
   - `gcp_region`
   - `tryon_catalog_path`
   - `tryon_catalog_upload` (set to `true` by default, set to `false` only if Q-E is answered as `No`)
2. Tell the user: "Setting up local sandbox environment resources..."
3. Run: `.venv/bin/python "$SKILL_DIR/scripts/setup.py" --config ./design-spec.md`
4. Stream the output. On success, start the local sandbox server in the background:
   `.venv/bin/python "$SKILL_DIR/scripts/start_sandbox.py" --config ./design-spec.md`
5. Provide the user with the clickable localhost link to test: "VTO fitting room sandbox is running! Open http://localhost:8080 in your browser to test it."

## Mode 2: Export Web App & GCS Catalog Sync (4 questions)

| Q | Question | Default | Source |
|---|---|---|---|
| Q2-A | GCP project ID? | `$GOOGLE_CLOUD_PROJECT` or `gcloud config get-value project` | env / gcloud |
| Q2-B | Try-on mode? | `2` (both Image + Veo Video) | prompt |
| Q2-C | GCS Catalog Bucket name? | `{project_id}-tryon-catalog` | prompt |
| Q2-D | Target Directory to export code? | `./vto-retail-app` | prompt |

### Question Formats & Accepted Choices:

- **Q2-B: Try-on mode?**
  Format to print:
  ```
  Q: Try-on mode? [default: 2]
    1. image_only (Faster, static images only)
    2. image_and_video (Catwalk video animations via Veo)
  ```
  Accept: `1` (= `image_only`), `2` (= `image_and_video`), `image_only`, `image_and_video`.

After collecting these answers, do this **automatically**:

1. Write the answers to `./design-spec.md` (in the workspace), filling in:
   - `gcp_project_id`
   - `tryon_mode`
   - `gcs_catalog_bucket` (starts with `gs://...`)
   - `export_directory`
   - `gcp_region` (default: `us-west1`)
   - `tryon_model` (always use `gemini-2.5-flash-image`)
2. Tell the user: "Setting up Cloud resources, exporting containerized codebase, and deploying to Google Cloud Run..."
3. Run: `.venv/bin/python "$SKILL_DIR/scripts/export_app.py" --config ./design-spec.md --skill-dir "$SKILL_DIR"`
4. Run GCS sync verification: `.venv/bin/python "$SKILL_DIR/scripts/setup_tryon.py" --config ./design-spec.md`
5. Build and Deploy container to Google Cloud Run:
   `gcloud run deploy vto-retail-app --source ./vto-retail-app/ --region us-west1 --project {gcp_project_id} --allow-unauthenticated`
6. Get the deployed service URL:
   `gcloud run services describe vto-retail-app --region us-west1 --project {gcp_project_id} --format="value(status.url)"`
7. Output the following structured instructions to the user:
   - Clickable Cloud Run service link:
     "🚀 **VTO App is deployed and running on Cloud Run! Open [Cloud Run App URL] in your browser to test it directly.**"
   - **How to Sync Catalog Images to GCS**:
     ```bash
     gsutil -m rsync -r ./my_clothes/ gs://{gcs_catalog_bucket}/
     # Then force index refresh:
     curl -X GET "https://{cloud_run_url}/api/catalog?force=true"
     ```
   - **How to Embed in Your Website**:
     ```html
     <!-- Place this iframe widget on your product details page -->
     <iframe src="https://{cloud_run_url}" width="100%" height="800px" style="border:none; border-radius:12px; box-shadow: 0 4px 20px rgba(0,0,0,0.15);"></iframe>
     ```

## When to Use

- Building **virtual try-on fitting rooms** for e-commerce.
- Creating interactive **catwalk-style video animations** showing how clothes look when walking.
- Enabling **general retail try-on** for accessories, jewelry, eyewear, or clothes.

Do NOT use for furniture/home styling (use room placement tools), or complex 3D avatar creation.

## Resource Setup

Set the parameters in `./design-spec.md` (in the workspace), then run:

```bash
.venv/bin/python "$SKILL_DIR/scripts/setup.py" --config ./design-spec.md
```

On success, local sample catalog images will be generated under `./catalog_images/` (unless a GCS bucket or custom catalog path was specified, in which case bucket access will be verified).

## Testing & Verification

Set the required environment variables in the shell that runs the agent:

```bash
export GOOGLE_CLOUD_PROJECT="<your-project-id>"
export TRYON_OUTPUT_BUCKET="<your-project-id>-tryon-output"
export TRYON_UPLOAD_BUCKET="<your-project-id>-tryon-uploads"
export GEMINI_IMAGE_MODEL="flash"  # or pro / gemini-2.5-flash-image / gemini-2.5-pro-image
```

### Run using `adk web`

Launch the interactive web UI. **Use `.venv/bin/adk`, not bare `adk`** -- bare
`adk` may resolve to a global Python (pyenv, brew, etc.) whose ADK can't find
the skill and reports an empty app list (UI loads, but `/list-apps` returns
`[]` and queries time out).

```bash
.venv/bin/adk web .
```

You can start a chat session and test VTO by providing:
- Product ID: `shirt_001` or `sunglasses_001`
- User Photo: Upload `catalog_images/sample_user.jpg` or any photo of yourself.
- Request: "Try on this shirt for me" or "Show me a catwalk video wearing these sunglasses".

### Direct Python Smoke Test

Run a quick test script without the UI:

```bash
# Test image try-on
.venv/bin/python -c "
from scripts.tryon_agent import try_on_product_image
res = try_on_product_image('shirt_001', 'catalog_images/sample_user.jpg', 'catalog_images/shirt_001.jpg', 'clothing', 'red shirt')
print(res)
"

# Test video try-on (Veo)
.venv/bin/python -c "
from scripts.tryon_agent import try_on_product_video
res = try_on_product_video('sunglasses_001', 'catalog_images/sample_user.jpg', 'catalog_images/sunglasses_001.jpg', 'eyewear', 'sunglasses')
print(res)
"
```

## Evaluation

Verify outputs using the local evaluation YAML. Ensure image consistency, correct garment placement, and no visual distortions.

## Sandbox Visual Testing

To test the VTO skill interactively with your own catalog of product images, launch the Sandbox Dashboard:

1. Start the FastAPI local server:
   ```bash
   .venv/bin/python "$SKILL_DIR/scripts/start_sandbox.py" --config ./design-spec.md
   ```
2. Open [http://localhost:8080](http://localhost:8080) in your browser.
3. Upload your own portrait photo, select any product card from the scanned catalog, and click **Generate Try-On Image**.
4. To index a custom local folder of images, type the folder path in the search header input and click **Scan** (uses Gemini to automatically catalog and describe them).

## Gotchas

- **Veo video resolution/duration**: Video generation via Veo takes ~30-60s. Be patient.
- **Image Models**: Use `flash` (recommended) or `pro` for general try-on.
- **Privacy Compliance**: ephemerally upload user photos into the uploads bucket with a 24-hour Lifecycle auto-delete rule (configured automatically by `setup_tryon.py`).

## Troubleshooting

| Error pattern | Likely cause | Fix |
|---|---|---|
| `BILLING_DISABLED` | GCP project has no billing | Link billing account in Cloud Console |
| `API has not been used` / `disabled` | Required API disabled | Run: `gcloud services enable aiplatform.googleapis.com storage.googleapis.com` |
| `PermissionDenied` on GCS | Service Account lack rights | Grant `roles/storage.admin` |
| `MethodNotImplemented: 501` / `Model not found` | Selected model is unavailable in region | Check your GCP project region availability |

## Completion Checklist

- [ ] GCP project ID and try-on mode configured.
- [ ] GCP resources provisioned and verified (Buckets, Gemini Enterprise Agent Platform APIs).
- [ ] Sample catalog generated.
- [ ] Smoke tests for image try-on succeed.
- [ ] Catwalk video generation using Veo verified.
