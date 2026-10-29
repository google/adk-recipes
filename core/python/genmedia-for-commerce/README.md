# GenMedia for Commerce

Product images and videos for online shops, made with Gemini and checked before they are shown. Chat with the Studio agent ("put her in this jacket", "now make a video of it") or fill in the Studio yourself, in the web app or inside Gemini Enterprise.

## What you get

- **Photo Try-On:** state-of-the-art virtual try-on. Show a person, or a preset model, wearing one product or a full outfit of up to 20, with close-up details kept, your brand styling rules, and a back view when there are back photos of a single product.
- **Video Try-On:** the same inputs, turned into short fashion videos with planned camera moves.
- **360° Spin:** a turntable animation (a looping animated WebP) of any product from a few photos, with a tuned path for shoes.
- **Quality checks:** a judge model compares every result with the product photos (colours, logos, missing or invented details) and the face when a person is involved. Results are scored, ranked and streamed as they finish.
- **The Studio agent:** an ADK agent that sets up the Studio from a conversation, finds catalogue products and preset models, and always asks "Shall I generate?" before it generates.
- **Gemini Enterprise:** the Studio runs as an interactive panel next to the Gemini Enterprise chat (an MCP App), registered with one command.
- **A sample catalogue:** 788 fashion products, 8 preset models and 5 products photographed from several sides, so you can try everything without your own photos.
- **One Cloud Run service:** the web app, the Gemini Enterprise panel, the ADK API and a REST API for each capability, set up with Terraform.

[AGENTS.md](./AGENTS.md) explains how each part works.

## Built with ADK

- **The agent:** the Studio agent is an ADK `App` (`root_agent` in `genmedia4commerce/agent/`), with its tools as ADK function tools, a plugin and callbacks around each turn and tool call, results saved as artifacts, and the "ask before generating" rule enforced in the `generate` tool's code.
- **Sessions:** a custom ADK session service on Cloud Storage (`studio://`, `genmedia4commerce/studio/sessions.py`), so any Cloud Run instance can continue any conversation, from the web app, Gemini Enterprise or the ADK API.
- **Serving:** the backend is ADK's `get_fast_api_app`, so the ADK API (`/run`, `/run_sse`) comes with the service:

```bash
curl -X POST http://localhost:8000/apps/genmedia4commerce/users/demo/sessions \
  -H "Content-Type: application/json" -d '{"session_id": "demo-chat-1"}'
curl -X POST http://localhost:8000/run -H "Content-Type: application/json" \
  -d '{"app_name": "genmedia4commerce", "user_id": "demo", "session_id": "demo-chat-1",
       "new_message": {"role": "user", "parts": [{"text": "Find me a black t-shirt."}]}}'
```

## Prerequisites

- Python 3.11 to 3.13, Node.js 20 or newer, [uv](https://github.com/astral-sh/uv) and the Google Cloud CLI (`gcloud`).
- A Google Cloud project with billing enabled.
- For Gemini Enterprise: a subscription or the free trial in that project (see [Gemini Enterprise](#gemini-enterprise)).

## Setup

```bash
git clone https://github.com/google/adk-recipes.git
cd adk-recipes/core/python/genmedia-for-commerce
make install

gcloud auth login
gcloud auth application-default login
gcloud config set project <your-project-id>

make setup-infra
```

`make setup-infra` creates `config.env` from `config.env.example` (with your gcloud project), then uses Terraform (installed for you if missing) to enable the APIs and create the media bucket, the image repository and the service accounts. Run it once per project, and again after upgrading, before you deploy. Every setting is in `config.env` ([Configuration](#configuration)).

## Run locally

```bash
make dev
```

Open <http://localhost:3000>: the Studio, with the agent's chat on the side and pages for Photo Try-On, Video Try-On and 360° Spin. The backend runs on port 8000.

Optional and best effort: `make playground` opens the ADK web UI on <http://localhost:8501>, to chat with the agent without the Studio panel.

Try: "Use Emma as the model and find me a black leather jacket", answer "yes" when the agent asks "Shall I generate?", then "Now make a video of it".

Running locally still uses Google Cloud: the models, and the media bucket where the Studio keeps its conversations, uploads and results.

## Deploy

```bash
make deploy
```

This builds the image and deploys it to Cloud Run with the settings in `config.env` (by default 8 CPUs, 32 GiB, 2 to 10 instances; the minimum instances are billed all the time). The service requires Google Cloud IAM authentication, so its URL answers 403 in a browser. When it finishes in a terminal, `make deploy` starts the web app in the background at <http://localhost:8080> (in Cloud Shell: **Web Preview > Preview on port 8080**); otherwise it prints how to open it. To run it in the foreground yourself:

```bash
make web
```

It runs `gcloud run services proxy` for the service in `config.env` on port 8080, which adds an identity token to every request: go to <http://localhost:8080>, or in Cloud Shell use **Web Preview > Preview on port 8080**. Ctrl+C stops it. Right after a deploy, the page can answer 403 for about a minute while `genmedia-web`'s access to the new service applies: wait and reload. The first run installs the gcloud component `cloud-run-proxy` if it is missing.

The token belongs to the service account `genmedia-web`, which can only invoke this service: `make setup-infra` creates it and lets whoever runs it act as it (Token Creator on that account only), and `make deploy` gives it Cloud Run Invoker on the service. This works the same on a laptop and in Cloud Shell, with no extra login (Cloud Shell's own credentials cannot mint a token for Cloud Run). If you cannot act as `genmedia-web`, `make web` uses your own token instead (`gcloud auth print-identity-token`, which works with `gcloud auth login` credentials on a laptop). A token lasts an hour, so the proxy restarts with a fresh one every 50 minutes.

To let teammates open the web app with `make web` (they also need the repository and `config.env`):

```bash
make share USERS="user:alice@example.com,group:team@example.com"
```

It grants each of them Token Creator on `genmedia-web` and Cloud Run Viewer on this service only (`make web` reads the service's URL). A setup made before `genmedia-web` existed needs `make setup-infra`, then `make deploy`.

After `make deploy` (so also the Agent Garden command) or `make register-gemini-enterprise` in a terminal, the proxy is already running in the background: the command prints its address and `kill <pid>` to stop it, and it stops by itself when the terminal session ends.

The backend accepts browser calls from those two origins because `ALLOW_ORIGINS` in `config.env` lists them: `http://localhost:8080` (and `127.0.0.1`) and, as a `regex:` pattern, Cloud Shell's Web Preview of port 8080 (`https://8080-<instance>.cloudshell.dev`). A `config.env` created before that pattern was added needs it copied from `config.env.example`, then `make deploy`. To use another port, add its origin to `ALLOW_ORIGINS` and deploy again. To let people in your organisation sign in from a browser, set `USE_IAP=true`. Secrets go through Secret Manager: `make deploy SECRETS="NAME=SECRET_ID:latest"`.

`make deploy-agent-engine` is a legacy name for the same deploy, kept for the Agent Garden command.

## Gemini Enterprise

After `make deploy`:

```bash
make register-gemini-enterprise
```

It connects the deployed Studio to Gemini Enterprise and creates a "GenMedia Studio" app. It can run again safely, and `DRY_RUN=1` shows what it would change. Use a project dedicated to this app: some of these settings apply to the whole project.

The command does everything it can and guides you through what only the console can do:

1. **Start the subscription or the free trial**, in the console under Gemini Enterprise, before you register.
2. **Allow custom MCP connectors:** Gemini Enterprise blocks them by default. The command asks to allow them for the project (Enter means yes; it needs the Organization Policy Administrator role) and waits until that applies. `make allow-mcp-connectors` does the same on its own; without a terminal the command prints what to run.
3. **Reload custom actions** (Gemini Enterprise > Data stores > GenMedia Studio > Actions), then enable `studio_agent`. Do it again after a deploy that changes the Studio's tools.

Then give users a licence (Gemini Enterprise > Manage users), open the app, turn on GenMedia Studio under Connectors and ask "Open the GenMedia Studio". Upload images with the panel's Upload button: files attached in the chat do not reach the Studio.

## Configuration

`config.env.example` lists the deploy settings and `.env.example` every variable the code reads, with their defaults. The ones you are most likely to change:

| Variable | What it does |
|---|---|
| `PROJECT_ID`, `DEFAULT_REGION` | The Google Cloud project, and the region of Cloud Run and regional services |
| `GENMEDIA_*_MODEL` | The model used for each task (text, judge, agent, image, video, embeddings) |
| `GENMEDIA_STYLE_GUIDELINES`, `GENMEDIA_SCENARIO` | Brand styling rules and the default scene for try-on images |
| `GENMEDIA_NUM_VARIATIONS` | Results per request (1 to 5, default 3) |
| `GENMEDIA_UPSCALE_FACE` | Upscale a small face crop of the wearer before it goes into the try-on prompt (default false) |
| `GENMEDIA_TRYON_FIX` | Give each try-on image the judge rejects one fix attempt with the judge's defects; a fixed image that passes replaces it and is marked `fixed` (default false) |
| `GENMEDIA_CATALOG_PATH` | Your own catalogue file, with the same columns as `assets/catalog/products.parquet` |
| `GENMEDIA_MAX_JOBS_PER_HOUR` | Studio generations per studio per hour (default 20) |
| `ALLOW_ORIGINS` | Browser origins allowed besides the app's own (by default the proxy's on port 8080 and Cloud Shell's Web Preview of it) |
| `USE_IAP` | Sign-in with Identity-Aware Proxy instead of IAM only |

## APIs

All on the same service; the OpenAPI reference is at `/docs`.

- **REST:** `POST /api/tryon`, `/api/video-tryon` and `/api/spin` take images (or `gs://`, `catalog:` and allowed `https://` references) and stream the results as Server-Sent Events. Also uploads, catalogue search and images, and media.
- **ADK API:** the Studio agent under the app name `genmedia4commerce` (`/run`, `/run_sse`; see [Built with ADK](#built-with-adk)).
- **MCP:** the Studio MCP App at `/mcp`, used by Gemini Enterprise and any MCP client that supports MCP Apps.

```bash
curl -N http://localhost:8000/api/tryon -F product_images=@jacket.jpg -F person_image=@person.jpg
```

## Security and data

- **One team per deployment:** everyone who can invoke the service, through IAM, IAP or the Gemini Enterprise app, can open any studio whose id they know. Grant access to that team only.
- **One place for data:** everything is stored in the media bucket, and deleted 30 days after its last change. Preview links are signed URLs, valid for 60 minutes.
- **Preprocessing cache:** so that the next photo try-on with the same product photos starts sooner, the server keeps what it worked out from the product photos only (their classification, the product crops, the detail close-ups and the styling plan) in the media bucket under `product_cache/`, in folders named by a hash of the photos and options; nothing from the person photo is kept. These are also deleted after 30 days, and are never served or accepted as image references. "Try again" after a failed generation recomputes them; `GENMEDIA_USE_CACHE=false` turns the cache off.
- **Hardening:** the server checks every id and image reference, limits generations per studio, and refuses browser calls from other websites.

## Evaluate and develop

```bash
make test        # hermetic tests: no network, no credentials
make lint        # codespell and ruff
make typecheck   # mypy
make secrets     # detect-secrets
make test-live   # tests against the real APIs (costs money)
```

When the frontend changes, also run `cd frontend && npm run lint && npm run build && npm test`. [AGENTS.md](./AGENTS.md) is the guide to the code and its rules.

## Disclaimer

This recipe is provided for illustrative purposes only. It is a starting point for building your own agents, not a finished product. You are responsible for any further development, testing, security hardening and deployment of agents based on it. Review and test thoroughly, and add appropriate safeguards, before using any derived agent in a live or critical system.
