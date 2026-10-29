# GenMedia for Commerce: guide for coding agents

How the code works and the rules for changing it. `README.md` is the only user document (setup, run, deploy, configuration); `CLAUDE.md` and `GEMINI.md` only point here.

## What the app does

A full-stack ADK app that makes product media for online shops with Gemini: Photo Try-On (a person wearing one product or a whole outfit), Video Try-On (the try-on animated into short videos), 360° Spin (a looping animated WebP of any product) and catalogue search. Each generation makes several variations (`GENMEDIA_NUM_VARIATIONS`, default 3, in parallel by default), checks each one against the product photos (and the person's face) with a judge model, ranks them and streams them as they finish.

One FastAPI app, deployed as one Cloud Run service, serves every entry point:

- the web app (`frontend/`), one page: the Studio, with the agent's chat beside it;
- the Studio MCP App at `/mcp`, an interactive panel inside Gemini Enterprise;
- the Studio agent, the app's one ADK agent, through those two and through the ADK API (`/run`, `/run_sse`); `make playground` runs it in the ADK web UI;
- REST routes that stream one generation as Server-Sent Events (`POST /api/tryon`, `/api/video-tryon`, `/api/spin`).

The agent never handles image bytes: it fills a "studio" (person, products, spin photos, settings) with image references and starts background jobs. The server owns the studio state; it, the agent's ADK sessions, the jobs, the uploads and the results all live in one Cloud Storage bucket, so any instance can answer any call.

## Module map

All the app's Python is under `genmedia4commerce/`. Read first: `core/gen.py`, `core/runner.py`, `requests.py`, `workflows/tryon/pipeline.py`, `studio/agent.py` and `api/app.py`.

| Path | What it holds |
|---|---|
| `__init__.py` | Loads `config.env` from the repository root when it exists (environment values win); exposes `agent` lazily |
| `agent/` | Re-exports `root_agent` and `app` from `studio/agent.py`, where ADK looks for them. Keep it a package: agent-starter-pack writes its own sample `agent.py` into the agent directory (`agent_directory` in `pyproject.toml`), and the package wins the import |
| `fast_api_app.py` | The uvicorn entry point (`genmedia4commerce.fast_api_app:app`) |
| `core/` | The shared layer, with no import from the rest of the package: `settings.py` and `models.py` (environment), `clients.py` (the one `genai.Client` factory and HTTP retry policy), `gen.py` (every model call), `retry.py` (deadlines, stalls, transport retries, `ContextPool`), `runner.py` (N variations, evaluate, rank), `events.py` (the event types), `runs.py` (run folders and output delivery), `_parts.py` and `_json.py` (request parts, lenient JSON for `gen`), `gcs.py` (Cloud Storage, `resolve_image`, signed URLs), `media.py`, `_fit.py`, `video.py` (image and frame helpers, Cloud Vision face boxes), `catalog.py` and `catalog_search.py`, `cache.py`, `_codec.py` and `degraded.py` (the preprocessing cache), `trace.py` (local debug trace), `errors.py`, `types.py`, `testing.py` (test helpers) |
| `eval/` | The checks, depending only on `core/`: `outfit.py` (the one-call outfit judge used by both try-ons), `identity.py` (same person in a video), `unseen.py` (video frames showing what no reference shows), `orientation.py` (view of each frame), `motion.py` (spin direction by optical flow, glitch call), `video_fidelity.py` (each reference view against its best frame, shoe or product judge). `garment.py`, `garment_detector.py` and `garment_prompts.py` (per-reference detector, verifier, aggregator) are reachable only as `video_fidelity`'s `judge="garment"`, which no workflow uses |
| `workflows/tryon/` | Photo Try-On (below) |
| `workflows/video_tryon/` | Video Try-On (below) |
| `workflows/spin/` | 360° Spin (below) |
| `workflows/details.py` | Close-ups of a product's small details (lettering, logos, hardware): one judge call finds them, each is cropped from the full-resolution photo and can be upscaled by the image model. Used by all three workflows |
| `requests.py` | Builds and validates every workflow request and starts runs (`start_run`), for both `tools.py` and the REST routes |
| `tools.py` | The four tool functions (`try_on`, `video_try_on`, `spin_360`, `catalog_search`): a generation tool folds its run's events into one JSON result; `stream` returns the events instead (the Studio jobs use it) |
| `describe.py` | One-sentence description of an uploaded image (the agent gets text and a `gs://` reference, never pixels) |
| `api/` | FastAPI: `app.py` (builds the app), one router per capability (`tryon.py`, `video_tryon.py`, `spin.py`, sharing `sse.py`), `uploads.py`, `catalog.py`, `media.py`, `status.py`, `studio.py` (the Studio for the web app), and the middlewares `message_limit.py` and `security_headers.py`. `adk_home/` is the near-empty agents folder ADK puts on `sys.path` |
| `studio/` | The Studio, with no transport: the agent (`agent.py`, `prompt.py`, `agent_tools.py`, `agent_settings.py`, `agent_spin.py`, `agent_spec.py`, `tool_order.py`, `turn.py`, `headless.py`), its sessions (`sessions.py`, `session_cache.py`, `session_limits.py`, `history.py`, `events.py`), the state (`store.py`, `state.py`, `patches.py`), jobs (`jobs.py`, `job_call.py`, `quota.py`), images (`media.py`, `picks.py`) and `service.py`, the operations both clients share |
| `mcp/` | `studio.py`, the MCP App adapter over `studio/service.py`; `static/studio.html`, the panel (plain HTML and JavaScript, no build step) |
| `app_utils/telemetry.py` | OpenTelemetry setup; by default prompts and replies are kept out of ADK's spans |

Outside the package:

- `frontend/`: React, Vite and Tailwind. `src/App.tsx` routes every path to the Studio (`src/pages/Studio/`): `useStudio.ts` and `store.ts` (the state the server owns, actions, the agent chat, jobs), `model.ts` (the server's state as the page shows it, job specs), `items.ts` (picks and uploads), `jobs.ts` (follows a job), `shrink.ts` (an upload over the server's limit or of a type it does not take becomes a JPEG in the browser). `src/api/client.ts` is the only module that calls `fetch`; `src/api/studio.ts` holds the `/api/studio` calls. `src/test/mockServer.ts` and `studioMock.ts` are the fixture backend of the tests and of `npm run dev:mock`.
- `infra/terraform/`: APIs, the media bucket and its lifecycle rules, Artifact Registry, the service accounts `genmedia-cloudrun` (runtime), `genmedia-build` (Cloud Build) and `genmedia-web` (identity of the `make web` proxy), and their roles. `infra/terraform.sh` runs Terraform, installing a pinned release when it is missing. `infra/web_proxy.sh` is `make web`.
- `infra/gemini_enterprise.py` with `gemini_enterprise_api.py`, `gemini_enterprise_app.py`, `gemini_enterprise_steps.py` and `gemini_enterprise_policy.py` (the organisation policy on custom MCP connectors): `make register-gemini-enterprise` and `make allow-mcp-connectors`. The Discovery Engine API's quirks are explained in the module docstrings.
- `assets/catalog/products.parquet`: the sample catalogue (products, preset models, spin sets, with WebP images and embeddings), copied into the image.
- `services.yaml`: registers the `studio://` session service for `adk web`.
- `tests/`: one folder per package (below).

## Data flow

### Every generation

```
caller: a Studio job (studio/jobs.py, through tools.stream) or a REST route (api/sse.py)
  -> requests.py: build and check the request; start_run resolves image refs,
     keeps the inputs (input mode gcs) and starts the run
  -> workflows/<capability>/pipeline.py: classify, references, prompt
     -> core.runner.run_variations: make N variations, evaluate each (eval/), rank
  -> core.runs.deliver: first event progress("run"); media inline as base64,
     or saved to the run's outputs/ (output mode gcs)
  -> events: progress, classified, variation (one per result), then one complete or one error
```

A workflow never raises to its caller: it ends with one `ErrorEvent` whose message is meant for the user (`core.errors.user_message(exc)` for an exception). The REST routes stream the events, the tool functions fold them into a dict, a Studio job folds them into its job document.

### Photo Try-On (`workflows/tryon/`)

`pipeline.run_tryon`:

1. `inputs.trimmed` crops the product photos to their content. With the cache on (`GENMEDIA_USE_CACHE`), `cache.TryOnCache` may return steps 2 and 3 (and the styling plan) from an earlier run with the same photos and options.
2. `classify.classify_products`: one judge-model call groups the photos into products (category, view, worn or flat, detail photos, the look photo of a whole outfit) and picks the framing. Then `worn.normalise` turns every photo it saw worn (product photos, the look photo, the reference model) into the same photo on a grey mannequin, all at once: one image-model edit (`mannequin.py`, a standalone module that imports nothing from the package) pasted back onto the photo, so every product pixel stays the original's. Every later step gets these photos and the prompt says "on a mannequin" (`ProductRef.mannequin`); a photo whose edit fails twice stays as it is (a fallback). Each converted photo is cached on its own, so a cache hit gets them too. The person photo is never touched.
3. At once: `closeups.search` (one `details.find` per product; close-ups of one photo that would show nearly the same area become one, `closeup_merge.py`) and `reference.complete`: one box call per photo to crop from (`cut.py`), the look photo's top and bottom halves as worn examples, each product's hero image (`hero.py`) and, with `complete_look`, the products the look photo adds.
4. `_prepare`: the wearer (`inputs.wearer`: the photo as it is plus its Cloud Vision face crop; `GENMEDIA_UPSCALE_FACE` upscales the prompt's face) and, for an outfit, the styling plan (`outfit.analyze_outfit`).
5. The run record (`record.py`) for a later video of these images, then the front variations: `prompt.build_prompt` for one product, `outfit_prompt.build_prompt` for several (one card per product, `cards.py`; how each is worn, `styling.py`), `gen.generate_image`, and `evaluate.evaluator` (the `eval/outfit.py` judge, which also sees the wearer and the face on the front view). With `GENMEDIA_TRYON_FIX` (off by default) each rejected image, front or back, gets one fix attempt (`fix.py`, prompt in `fix_prompt.py`) and is judged again; a fix that passes replaces it, marked `fixed`. The prompts never leave a bare chest or underwear on show (a plain top or bottom in a neutral colour instead), and the judge checks it on the front view.
6. A single product with back photos then gets back views drawn from the best front (`back_view`). One `complete` ranks the fronts.

### Video Try-On (`workflows/video_tryon/`)

`pipeline.run_video_tryon` runs `run_tryon` first (its events tagged `extra.stage="image"`) and animates each front that passes at once, in its own branch (`branch.py`), while the other images are still being made; with none passing, the best-scoring one. A branch first draws the back view of a single product with a back photo, then builds the references (`refs.py`: the try-on image, its back, the face, photos of accessories and shoes, close-ups), plans 1 to 3 shots of 8 to 10 seconds in total (`plan.py`), writes the video model's prompt (`prompt.py`) and makes `GENMEDIA_VIDEOS_PER_IMAGE` videos (default 2). Each video is checked by `evaluate.evaluate_clip`: identity (`eval/identity.py`), blind spots (`eval/unseen.py`) and the products on still frames (`checks.py`, the outfit judge in video-frame mode). One `complete` ranks every video. With a `tryon_image` only that image is animated; its products come from the try-on's run record (`source_run`) or are classified again.

### 360° Spin (`workflows/spin/`)

`pipeline.run_spin`: `classify.py` (one judge call: product type, the view of each photo, a description). Shoes: `shoe_refs.py` builds the reference set (labels, pairs cut apart by `shoe_split.py`, a mirrored twin by the image model in `shoe_twin.py`), one video with the tuned shoe prompt, then `shoe_check.py` (frame labels give the direction, turn-backs, jumps and the loop cut; the shoe judge compares frames and references). Other products: `product_refs.py` (the best photo per angle, reduced to the product alone), close-ups (`details.find`, `GENMEDIA_SPIN_CLOSEUPS`), two half turns of one stored video interaction (`prompt.orbit_turns`), then `product_check.py` (optical flow, frame labels and a glitch call; an anticlockwise spin is reversed, a turn-back rejected, the clip cut where it closes) and the product judge. Every spin is delivered as a looping animated WebP (`core.video.to_webp`).

### Catalogue

`core/catalog.py` reads the parquet file once, on first use. Its images are `catalog:<id>` or `catalog:<id>/<n>` references, which `gcs.resolve_image` reads, so every image input takes them. `catalog_search.search` embeds the query with the catalogue's embedding model, ranks by cosine similarity plus a keyword boost and falls back to keywords alone when the embedding call fails; `find` lists the featured products for an empty query.

### A Studio turn

```
Gemini Enterprise chat -> studio_agent MCP tool (mcp/studio.py)   or   web app -> POST /api/studio/{id}/agent
  -> studio/service.py agent_turn -> studio/agent.py run_agent_turn: a turn scope (studio/turn.py)
     holding a working copy of the saved state
  -> ADK Runner on the studio's session (studio/sessions.py, the studio:// service)
  -> _StudioPlugin rewrites the user's message before it is stored: attachments become [studio] lines;
     the current studio and up to six of its images (gs:// file parts) are added when they changed
  -> the Studio agent (LlmAgent, instruction in studio/prompt.py, eleven tools in agent_tools.py,
     agent_settings.py and agent_spin.py) edits the working copy and records patches
  -> state.save_patched applies the turn's patches to the saved state.json; the reply and
     the state go back to the panel or the web app
```

- The panel and the web app send each user change as an action (`studio_event`, `POST /api/studio/{id}/actions`): `state.apply_action` checks it with the rules in `patches.py`, applies it to `state.json` and notes it in the session as a `[studio]` line (`history.py`, `events.py`).
- Jobs: the agent's `generate` and `remake_video` and the clients' job routes all start through `quota.start` (saved studio, one running job per studio, `GENMEDIA_MAX_JOBS_PER_HOUR` per studio, `GENMEDIA_MAX_JOBS_PER_INSTANCE_HOUR` per instance). `jobs.py` runs `tools.stream` on that instance, with outputs in the bucket, and rewrites the job document as events arrive; clients poll it. When the job ends, `state.finish_job` records the results and a note goes to the session.
- `generate` refuses with `needs_confirmation` when the agent changed the inputs, the page or a cost setting in the same turn (`tool_order.py`), so the agent asks "Shall I generate?" first.
- Without a panel (`adk web`, the ADK API) `_before_agent` opens a `Headless` scope for the studio named by the ADK session id; `headless.py` turns attached images into uploads and saves finished results as ADK artifacts.

## Prompts and model ids

- Model ids live only in `core/models.py`: one environment variable per role (`GENMEDIA_TEXT_MODEL`, `GENMEDIA_TEXT_LITE_MODEL`, `GENMEDIA_JUDGE_MODEL`, `GENMEDIA_STUDIO_AGENT_MODEL`, `GENMEDIA_IMAGE_MODEL`, `GENMEDIA_VIDEO_MODEL` with `GENMEDIA_VIDEO_API_REVISION`, `GENMEDIA_EMBED_CATALOG_MODEL`) and a default. Code reads them with `get_models()`. The judge model also does the classifying, boxing and planning calls.
- Prompts live next to the code that sends them, as module constants: `workflows/tryon/` (`prompt.py`, `outfit_prompt.py`, `styling.py`, `skin.py`, `classify.py`, `cut.py`, `outfit.py`, `fix_prompt.py`, `mannequin.py`), `workflows/video_tryon/` (`plan.py`, `prompt.py`, `checks.py`), `workflows/spin/` (`prompt.py`, `shoe_prompts.py`, `classify.py`, `product_check.py`), `workflows/details.py`, `eval/` (the long ones in `eval/prompts/*.md`, the others in `motion.py`, `orientation.py`, `unseen.py`, `video_fidelity.py`), `studio/prompt.py` and the tool docstrings in `studio/agent_tools.py` (the agent), `describe.py`, and the upscale prompts in `core/media.py`.
- The try-on prompts are pinned by golden files in `tests/workflows/tryon/golden/`.
- Any change to a source or prompt file starts a new preprocessing cache (`core.cache.code_fingerprint`).

## Settings

- `core/settings.py` reads the deployment settings once (`get_settings()`, cached); `PROJECT_ID` is checked only when first used, so the package imports without it. A few variables are read where they are used: `ALLOW_ORIGINS`, `SESSION_SERVICE_URI`, `LOGS_BUCKET_NAME` and `K_SERVICE` in `api/app.py`, `GENMEDIA_MAX_JOBS_PER_*` in `studio/quota.py`, the telemetry ones in `app_utils/telemetry.py`.
- `.env.example` declares every variable the code reads, with its default: add a new one there (adk-recipes' env-var check fails on an `os.getenv` or `os.environ` read it does not declare). Do not repeat the list here.
- `config.env` (made from `config.env.example` by any target that needs it) holds the values for a project; the Makefile also reads the deploy sizing, region, service name and `USE_IAP` from it. A deployment passes the same values as environment variables.

## Storage

Everything is in the media bucket (`MEDIA_BUCKET`, default `<PROJECT_ID>-genmedia-for-commerce-media-payloads`). Terraform deletes objects under `runs/`, `sessions/` and `product_cache/` 30 days after they were written (`media_retention_days`).

- `runs/<date>/<session_id>/<run_id>/`: one folder per run (`core/runs.py`): `inputs/` with `GENMEDIA_INPUT=gcs` (or `input=gcs` on a call) and for `POST /api/uploads`, `outputs/` with `GENMEDIA_OUTPUT=gcs` (or `output=gcs`; events then carry `media_uri` and `media_url`, served by `GET /api/media`), and, for a try-on with gcs output, its run record `manifest.json` with `record/`. Workflows never see the modes.
- `runs/studio_catalog/`: the Studio's copies of catalogue images; the Studio's own runs use the session `studio-<id>`.
- `sessions/studio-<id>/`: `adk/session.json` and one object per event under `adk/events/` (the ADK session), `state.json` (the studio) and `results.json` (the latest results). `sessions/studio-jobs/<job_id>.json`: one job. Writers on different instances go through `store.update` (a generation precondition, retried on conflict); events are written create-only.
- `product_cache/<key>/`: the try-on preprocessing cache (`core/cache.py`, `workflows/tryon/cache.py`, and each worn photo on a mannequin, `workflows/tryon/worn.py`), from product photos only; nothing from a person photo is cached. No image reference rule reaches it.
- Elsewhere: with `LOGS_BUCKET_NAME` set, ADK artifacts go to that bucket (`api/app.py`) and GenAI telemetry can be uploaded there (`app_utils/telemetry.py`); the debug trace (`GENMEDIA_DEBUG_DIR`, `core/trace.py`) is on local disk.

## Rules the server enforces (keep them)

- Image references: a client or the agent may use only catalogue images, the catalogue copies and the image and video files of that studio's runs (`studio.media.allowed_ref`); a job spec is checked by `studio.job_call.check_refs` before it starts; `GET /api/media` serves run inputs and outputs (`core.runs.is_media_file`) and the catalogue copies, never a record or a manifest.
- Studio and job ids are refused, never rewritten, when invalid (`store.check_id`, `store.check_job_id`).
- A user message is at most 4000 characters (`studio/session_limits.py`; `api/message_limit.py` on ADK's `/run` and `/run_sse`).
- What a client or the agent's model reads of a failure is `core.errors.user_message(exc)`: the message of `InputError`, `GenerationError`, `EvalError` and `BusyError`, a generic one for anything else (log the details).
- Browser side: an enforced CSP and security headers outside `/mcp` (`api/security_headers.py`); CORS same-origin unless `ALLOW_ORIGINS` lists origins (`*` is refused at startup); ADK's session list and delete routes answer 404; no ADK web UI is served.
- Everyone who can invoke the service is one trusted team: there are no per-user accounts.

## Commands

| Command | Purpose |
|---|---|
| `make install` | Python dependencies with uv |
| `make dev` | Backend on 127.0.0.1:8000 and the Vite dev server on :3000 (`make run-backend` / `make run-frontend` for one) |
| `make playground` | Optional, best effort: the ADK web UI on :8501, the agent without a panel |
| `make test` | The hermetic test suite |
| `make test-live` | `tests/live` against the real APIs (needs credentials, costs money) |
| `make lint` | codespell, ruff check and ruff format check with the adk-recipes rules; fails when `uv.lock` is out of date |
| `make format [FILES=...]` | ruff fix and format with the same rules |
| `make typecheck` | mypy on `genmedia4commerce/`, `tests/` and `infra/` with the flags of adk-recipes' CI (no mypy config: its CI would not read ours) |
| `make secrets` | detect-secrets on the tracked files when a `.secrets.baseline` is present, skipped otherwise; mark a false positive with `# pragma: allowlist secret` |
| `make check-panel` | Syntax check of the script in `mcp/static/studio.html` |
| `make tf-check` | `terraform fmt -check` and `validate`, no Google Cloud access needed |
| `make setup-infra` | Terraform apply (APIs, bucket, Artifact Registry, service accounts, IAM); `make tf-plan` / `make tf-destroy` |
| `make deploy` | Build from source and deploy the Cloud Run service; `make deploy-agent-engine` is a legacy alias |
| `make web`, `make share USERS=...` | Open the deployed web app through a local proxy; let teammates do it |
| `make register-gemini-enterprise [DRY_RUN=1]` | Register the Studio in a Gemini Enterprise app |
| `make allow-mcp-connectors` | Allow custom MCP connectors in the project (the per-project override of the organisation policy) |
| `cd frontend && npm run lint && npm run build && npm test` | Biome (the adk-recipes rules), `tsc` and the Vite build, Vitest; `npm run format` fixes formatting |

Run `make deploy` only after the user confirms. `make register-gemini-enterprise` changes IAM and a Gemini Enterprise app: run it with `DRY_RUN=1` first, and without it only after the user confirms; without a terminal it prints each console-only step and exits. `make allow-mcp-connectors` changes an organisation policy: run it only after the user confirms.

## Code rules

- Delete before you build: standard library first, then the SDK, then one shared helper. One of each: one client factory, one retry policy, one runner. No dead code.
- Keep modules to at most 400 lines and functions to at most 60: split before you add. Line length is 80 (ruff, with the adk-recipes rules passed as flags by the Makefile: there is no ruff config file, and adk-recipes forbids one, as it does a recipe-local Biome config).
- Imports: `from genmedia4commerce.<pkg> import ...`; never edit `sys.path`.
- Model calls go through the module so tests can fake them: `from genmedia4commerce.core import gen`, then `gen.generate_image(...)`. A model id anywhere but `core/models.py` is a bug.
- Model parameters (`TextParams`, `ImageParams`, `VideoParams`) travel from the request to the `gen.*` call; workflows set the defaults, helpers never hard-code a size, ratio, thinking level or resolution.
- No `fastapi`, `starlette` or request models under `core/`, `eval/` or `workflows/`. `core/` imports nothing else of the package and `eval/` only `core/`.
- `gen`, `media` and `eval` functions are synchronous; workflows call them with `asyncio.to_thread`.
- Raise the types in `core/errors.py`; workflows yield an `ErrorEvent` instead of raising.
- Log with `logging.getLogger(__name__)`, never `print`.
- Every new code file starts with the Apache 2.0 header of the existing files (`tests/test_license_headers.py` checks it).
- `README.md` is the only user document; this file is the guide to the code. Write plain, direct language, one paragraph per line.

## How to test

- `make test` (`pytest -m "not live"`, skipping `tests/integration`, `tests/unit` and `tests/load_test`, which agent-starter-pack adds to the projects it creates). Tests are hermetic: no network, no credentials, no Cloud Storage. The autouse fixture in `tests/conftest.py` sets `PROJECT_ID=test-project`, a catalogue path with no file, inline input and output, no trace and no cache, and makes any real `genai` client fail.
- Fixtures: `fake_gen` (from `core/testing.py`) fakes every `core.gen` call and records them; `storage` is an in-memory bucket; `catalog_file` writes a tiny catalogue.
- Tests mirror the package: `tests/core`, `tests/eval`, `tests/workflows/<capability>`, `tests/studio`, `tests/api`, `tests/mcp`, `tests/infra`, `tests/app_utils`. `tests/test_runnability.py` imports the package, the agent and the app without credentials (adk-recipes requires it).
- Goldens: after an intended prompt change, regenerate them with `UPDATE_GOLDEN=1 uv run pytest tests/workflows/tryon` and review the diff.
- Live tests are in `tests/live/`, marked `@pytest.mark.live`, and run only with `GENMEDIA_LIVE=1` (`make test-live`).
- `tests/benchmark/rotation/run_benchmark.py` measures `eval.motion.check_rotation` on labelled clips.
- Before a commit: `make lint`, `make typecheck`, `make secrets` and `make test`; frontend changes add the npm commands above; `studio.html` changes `make check-panel`; Terraform changes `make tf-check`.

## Reuse

- `core/` imports nothing else of the package: copy it for any Gemini project that needs typed model parameters, one retry policy and a fan-out runner. `eval/` needs only `core/`.
- `studio/store.py`: JSON documents in Cloud Storage that several instances update safely (generation preconditions, retry on conflict).
- `studio/sessions.py`: an ADK session service on Cloud Storage (one object per event, create-only writes), registered under the `studio://` scheme for `get_fast_api_app` (`register()`) and `adk web` (`services.yaml`).
- `mcp/studio.py` with `studio/`: an ADK agent behind an MCP App, with a server that owns the panel's state, one chat tool, app-only tools for the panel and background jobs the panel polls.
- `infra/gemini_enterprise.py` registers an MCP server on Cloud Run (`<URL>/mcp`) in a Gemini Enterprise app; change `DISPLAY_NAME` and `STUDIO_ACTIONS` in `gemini_enterprise_steps.py`.

## Gotchas

- Run ADK from the repository root, as the Makefile does (`adk web .`). Pointing it at `genmedia4commerce/agent` puts `genmedia4commerce/` on `sys.path`, where `requests.py`, `tools.py` and `mcp/` shadow the libraries of the same names; the same reason gives the backend its own agents folder, `api/adk_home/`.
- The agent always needs the media bucket, also in `make playground`: its sessions, the studio state, uploads and jobs live there.
- Keep the system instruction identical from call to call and the stored conversation append-only, so Gemini's prefix cache hits (`Studio model call: <n> prompt tokens, <m> cached` in the logs).
- Studio jobs run as background tasks on the instance that started them: the service needs CPU always allocated (`--no-cpu-throttling` in `make deploy`), and a job whose instance stops is reported as interrupted, not resumed.
- Preview URLs are V4 signed URLs: the runtime account needs Token Creator on itself (Terraform grants it). With user credentials signing fails and previews fall back to inline images.
- `make run-backend` sets `ALLOW_ORIGINS` to the Vite dev server: Vite's proxy rewrites the Host but forwards the browser's Origin, which ADK's origin check would refuse.
- agent-starter-pack (Agent Garden) renders the `Makefile` with Jinja2: never write `{{`, `{%` or `{#` in it (`tests/infra/test_deploy_files.py` checks it). Agent Garden's command ends with `make deploy-agent-engine` and fills `config.env` by replacing `your-gcp-project-id` in `config.env.example`: keep that alias, that placeholder and `agent_engine` in `[tool.agent-starter-pack.settings]` until it uses `make deploy`.
