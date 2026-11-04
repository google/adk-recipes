# GenMedia for Commerce

Product images and videos for online shops, made with Gemini. Chat with the Studio agent ("put her in this jacket", "now make a video of it") or fill in the Studio yourself, in the web app or inside Gemini Enterprise.

## What you get

- **Photo Try-On:** a person, or a preset model, wearing one product or an outfit of up to 20, with close-up details, brand styling rules and a back view.
- **Video Try-On:** the same inputs as short fashion videos with planned camera moves.
- **360° Spin:** a looping turntable animation of any product from a few photos.
- **Quality checks:** a judge model compares each result with the product photos and scores it. Off by default in the Studio: turn on "Check images" or "Check videos" in Run options.
- **The Studio agent:** an [ADK](https://google.github.io/adk-docs/) agent that sets up the Studio from a conversation and asks "Shall I generate?" before it generates.
- **Gemini Enterprise:** the Studio as an interactive panel next to the chat (an MCP App).
- **A sample catalogue:** 788 fashion products, 8 preset models and 5 multi-view products, so you can try it without your own photos.

[AGENTS.md](./AGENTS.md) explains how each part works.

## Prerequisites

- Python 3.11 to 3.13, Node.js 20 or newer, [uv](https://github.com/astral-sh/uv) and the Google Cloud CLI (`gcloud`).
- A Google Cloud project with billing enabled.

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

`make setup-infra` creates `config.env` (every setting, see [Configuration](#configuration)) and, with Terraform, the APIs, the media bucket and the service accounts. It can run again safely; run it again after upgrading.

## Run locally

```bash
make dev
```

Open <http://localhost:3000>. Try: "Use Emma as the model and find me a black leather jacket", answer "yes" when the agent asks "Shall I generate?", then "Now make a video of it". Local runs still use your project's models and media bucket.

## Deploy and Gemini Enterprise

```bash
make deploy
make register-gemini-enterprise
```

- `make deploy` puts the app on Cloud Run (private: IAM only) and opens it at <http://localhost:8080> through a local proxy. Later, `make web` opens it again, and `make share USERS="user:alice@example.com"` lets teammates do the same.
- `make register-gemini-enterprise` creates a "GenMedia Studio" app in Gemini Enterprise. Enter answers yes to its questions (allow custom MCP connectors, start the 30-day free trial); one step is in the console: click **Reload custom actions** on the page it links, then press Enter. Use a project dedicated to this app.

Then open the app and ask "Open the GenMedia Studio". Upload images with the panel's Upload button: files attached in the chat do not reach the Studio.

## Cloud Shell: one command

In [Cloud Shell](https://shell.cloud.google.com), with your project selected (`gcloud config set project <your-project-id>`):

```bash
d=genmedia4commerce-$RANDOM && uvx google-agents-cli@1.12.0 scaffold create $d -ag -y -p -d cloud_run \
  --agent-guidance-filename AGENTS.md -a https://github.com/google/adk-recipes/tree/main/core/python/genmedia-for-commerce \
  && cd $d && make install setup-infra deploy register-gemini-enterprise
```

It runs all of the above. Open the web app with **Web Preview > Preview on port 8080**.

## Configuration

`config.env` holds the deploy settings and `.env.example` lists every variable with its default. The most useful:

| Variable | What it does |
|---|---|
| `PROJECT_ID`, `DEFAULT_REGION` | The project, and the region of Cloud Run and regional services |
| `GENMEDIA_*_MODEL` | The model of each task (text, judge, agent, image, video, embeddings) |
| `GENMEDIA_STYLE_GUIDELINES`, `GENMEDIA_SCENARIO` | Brand styling rules and the default scene of try-on images |
| `GENMEDIA_NUM_VARIATIONS` | Results per request (1 to 5, default 3) |
| `GENMEDIA_LOOK_CONFLICT` | When the look photo shows another item in an uploaded product's place (other shoes, another skirt): `upload` (default) keeps the uploaded product, `look` the look's |
| `GENMEDIA_CATALOG_PATH` | Your own catalogue, with the columns of `assets/catalog/products.parquet` |
| `MEDIA_BUCKET` | The media bucket (default `<PROJECT_ID>-gfc-media`) |
| `USE_IAP` | Browser sign-in with Identity-Aware Proxy |

## APIs

One service serves the web app, the REST API (`/api/tryon`, `/api/video-tryon`, `/api/spin`, streamed as Server-Sent Events; reference at `/docs`), the ADK API (`/run`, `/run_sse`, app `genmedia4commerce`) and the MCP App (`/mcp`).

```bash
curl -N http://localhost:8000/api/tryon -F product_images=@jacket.jpg -F person_image=@person.jpg
```

## Security and data

- **One team per deployment:** anyone who can invoke the service can open any studio whose id they know.
- **One place for data:** everything is in the media bucket and deleted 30 days after its last change; preview links are signed URLs valid for 60 minutes.
- **Cache:** what was worked out from product photos only (never the person photo) is kept under `product_cache/` and `spin_cache/` so the next run starts sooner; `GENMEDIA_USE_CACHE=false` turns it off.

## Develop

```bash
make test        # hermetic tests: no network, no credentials
make lint        # codespell and ruff
make typecheck   # mypy
```

[AGENTS.md](./AGENTS.md) is the guide to the code and its rules.

## Disclaimer

This recipe is provided for illustrative purposes only. It is a starting point for building your own agents, not a finished product. You are responsible for any further development, testing, security hardening and deployment of agents based on it. Review and test thoroughly, and add appropriate safeguards, before using any derived agent in a live or critical system.
