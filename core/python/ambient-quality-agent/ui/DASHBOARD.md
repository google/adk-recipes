# The AQA dashboard service

`app.py` — a FastAPI service that serves the dashboard's front end and the
`/api/*` routes behind it. It reuses the web stack `adk web` already ships
(FastAPI + Starlette + uvicorn) and the shared wire client, so on top of
`google-adk` it adds essentially no backend dependencies.

**This file is the service.** What the dashboard *shows* — the views, the
charts, the state machines — is the React app in `web/`, documented in
[`web/README.md`](web/README.md). What this directory is as a deployable is
[`README.md`](README.md).

```
ui/
├── app.py                  FastAPI: the /api/* routes, and the bundle at /
├── web/                    the front end: React + Vite + Tailwind + shadcn
├── agents-cli-manifest.yaml
├── Dockerfile              a Node stage builds web/ into static_v2/
├── requirements.txt
├── README.md               what this directory is, as an agents-cli project
└── DASHBOARD.md            this file

src/ambient_quality_shared/  the wire client, shared with src/ambient_quality_cli
├── agent_client.py          AgentRuntimeClient (deployed) + AdkHttpClient (local)
└── protocol.py              the command route paths, shared with the agent
```

This directory and the shared package are staged into one build context — by
`agents-cli deploy` via [`extension/acli_ui.py`](../extension/acli_ui.py), or by
[`deploy_ui_cloud_run.sh`](deploy_ui_cloud_run.sh) — and the Dockerfile fails the build if the
shared package is missing. Running by hand needs `PYTHONPATH` pointed at
`src/`, which `tools/local_ui.sh` sets.

## Serving the front end

`npm run build` writes the bundle to `ui/static_v2`, and that bundle is the
whole of what `/` serves:

- `/` returns the shell, and so does any path the server does not claim, so the
  client-side router can match it;
- `/assets/*` is mounted as real static files — Vite hashes those names, so they
  are immutable;
- `/v2/*` answers 308 to `/*`, because insight permalinks already in circulation
  were built that way;
- with **no bundle**, `/` answers **503** with the command that builds one. The
  API keeps answering underneath, which is why a missing bundle is not an import
  error. An image without one is misbuilt; a checkout has none until the build
  has run once.

Everything to do with the bundle is registered *last* in `app.py`, on purpose:
FastAPI matches in registration order and the catch-all matches every path
there is, so declared any earlier it would shadow `/api/*`. `_OURS_404` and
`_NOT_IMPLEMENTED_501` are the backstop against the failure that costs the most
to debug — a data request answered with a page of HTML, which `fetch` reads as
success and which then fails somewhere unrelated.

## How a click reaches the agent

Each action POSTs one of the agent's **command routes** over the shared client's
`post_command(path, payload)`, which calls the matching tool directly with **no LLM
roundtrip** and answers with the tool's result as JSON. The endpoints below
forward that JSON to the front end.

| action | route | tool |
| --- | --- | --- |
| list investigations | `POST /investigations/list` `{window}` | `list_investigations` |
| total them, over a period | `POST /investigations/stats` `{window}` | `get_investigation_stats` |
| per-day figures | `POST /investigations/daily` `{window}` | `get_daily_trends` |
| start one | `POST /investigations/schedule` | `schedule_investigation` |
| open one | `POST /investigations/get` `{"run_id"}` | `get_investigation` |
| list insights | `POST /insights/list` `{filters}` | `list_insights` |
| open one | `POST /insights/get` `{"insight_id", …}` | `get_insight` |
| dismiss / merge one | `POST /insights/{dismiss,merge}` | `dismiss_insight` / `merge_insight` |
| is the loop turning | `POST /health` | `get_ambient_health` |
| show configuration | `POST /config` | `show_config` |

The routes are defined in `ambient_quality_agent.core.command_routes` and
mounted on the container's app, so they are **not** env-configurable. Only the
config *read* is a route: changing a setting stays a conversation, where the LLM
can validate the field and the value.

Requests go out in parallel — react-query owns the fetching, and there is no
shared agent session for two of them to interleave on. A route can be reading
BigQuery behind a full table scan, so the front end keeps a client-side timeout
and a **Retry** on every list.

### Dashboard endpoints

| endpoint | returns |
| --- | --- |
| `GET /api/config` | `{"config": {...}}` — the effective configuration |
| `GET /api/health` | `{"verdict", "reason", ...}` — is the ambient loop turning |
| `GET /api/investigations` | `{"runs": [...]}` for the table — the most recent page |
| `POST /api/investigations` | `{"run": {...}}` if a run comes back, else `{"ok": true}`, or `{"error": ...}` |
| `GET /api/investigations/{run_id}` | `{"run": {...}}` for one run, events included |
| `GET /api/investigations/{run_id}/cases/{case_id}` | `{"case": {...}}` — one conversation |
| `GET /api/stats` | `{"stats": {...}, "window": {...}}` — runs' counters, summed |
| `GET /api/daily` | `{"days": [...]}` — per-day figures for the charts |
| `GET /api/insights` | `{"insights": [...], "total", "next_page_token"}`; optional `status`, `runId`, `hasRootCause`, `pageToken` |
| `GET /api/insights/{insight_id}` | `{"insight": {...}, "occurrences": [...], "next_page_token"}`; `includeTraces=1` adds the conversation traces |
| `POST /api/insights/{insight_id}/dismiss` | `{"dismissed": true}` |
| `POST /api/insights/{insight_id}/merge` | `{"merged": true}` |
| `GET /api/source` | `{"available", "reason", ...}` — can findings cite code |
| `GET /healthz` | liveness probe |

`windowStart` / `windowEnd` (ISO instants, both optional) scope `/api/stats`,
`/api/daily`, `/api/investigations` and `/api/insights` to a period; with
neither, each covers everything it can see. The front end sends `windowStart`
on the two that aggregate and on neither list — `lib/day-buckets.ts` says why.

Every route degrades to `{"error": ...}` rather than a raw 5xx, so a failed read
renders as a named failure instead of an empty page.

## Configuration (env vars)

| var | meaning |
| --- | --- |
| `AGENT_ENGINE_RESOURCE_ID` | `projects/…/locations/…/reasoningEngines/…` of the deployed AQA agent (the default) |
| `AGENT_ADK_BASE_URL` + `AQA_BACKEND=adk` | point at a locally served copy of the container's app for a fast dev loop |
| `AQA_STATIC_V2_DIR` | where the built bundle is, when it is not beside `app.py` |
| `GOOGLE_API_USE_CLIENT_CERTIFICATE=false` | disables client-certificate (mTLS) lookup, which breaks ADC token minting on machines with a device certificate |
| `PORT` | listen port (default 8080) |

## Run locally

`tools/local_ui.sh` covers both backends; which one you get depends on whether
you name an engine. Either way, build the front end first — `npm --prefix
ui/web ci && npm --prefix ui/web run build` — or `/` will tell you to.

**Against a deployed AQA** — the region is read out of the resource name, so
there is nothing else to set:

```bash
AGENT_ENGINE_RESOURCE_ID=projects/<project-number>/locations/<region>/reasoningEngines/<id> \
  tools/local_ui.sh                # http://localhost:8080
```

Note this runs the **deployed** agent's code: an agent-side change (an added
command route, say) needs a redeploy before the dashboard can exercise it, while
every UI-side change is picked up from the checkout.

**Against a local agent** — a fast iteration loop with no cloud round-trip, via
the `AdkHttpClient` backend. The script serves the container's own app
(`uvicorn ambient_quality_agent.fast_api_app:app`), which carries the command
and A2A routes, and shuts it down on exit:

```bash
GOOGLE_CLOUD_PROJECT=<your-project> tools/local_ui.sh
# UI on http://localhost:8080 -> local agent on http://localhost:8000 (app: ambient_quality_agent)
```

If you serve that app yourself in another terminal, the script detects it at
`$ADK_URL` and just launches the UI.

Overrides: `ADK_URL` (default `http://localhost:8000`), `ADK_APP` (default
`ambient_quality_agent`), `UI_PORT` (default `8080`).

**Against generated data** — a month of investigations and insights, no cloud,
no credentials, no model. This is the loop for front-end work, and the only one
with enough history to develop a visualization against:

```bash
make demo                        # http://localhost:8080; builds the bundle too
tools/mock_ui.sh                 # the same thing, with the env knobs to hand

MOCK_FAILURE_RATE=0 tools/mock_ui.sh   # a deployment where nothing fails
MOCK_DROP_RATE=1    tools/mock_ui.sh   # one where nothing survives ingestion
```

The last two are worth running before touching anything that reads the counters.
A healthy customer's deployment looks like them and a generated month never
does, so they are the states a visualization breaks in unnoticed.

`tools/mock_aqa_data.py` writes the deployment to JSON and
`tools/mock_aqa_server.py` serves it over the same command and A2A routes a
locally served agent answers, so the UI runs its ordinary `AGENT_ADK_BASE_URL`
path and nothing in it is mock-aware.

Running `app.py` by hand works too, but **from `ui/`** — it imports
`ambient_quality_shared` as a top-level package, so that directory has to be the
working directory and `src/` has to be on the path:

```bash
cd ui
export AGENT_ENGINE_RESOURCE_ID=projects/…/locations/…/reasoningEngines/…
PYTHONPATH=../src GOOGLE_API_USE_CLIENT_CERTIFICATE=false \
  ../.venv/bin/python app.py
```

## Using AQA from coding harnesses

To use the AQA agent **as a tool** inside coding harnesses (Anthropic **Claude
Code**, Google **Antigravity**), drive the same command routes from the command
line with `src/ambient_quality_cli/aqua_cli.py`.
