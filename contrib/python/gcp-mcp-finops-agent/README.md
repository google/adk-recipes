# FinOps agent on the Google Cloud BigQuery MCP server

An ADK agent that answers Google Cloud cost questions from your Cloud Billing
export in BigQuery. It reaches BigQuery through the
[Google-hosted BigQuery MCP server](https://docs.cloud.google.com/mcp/supported-products),
so there is no MCP server to install, run or deploy. The agent connects to the
remote endpoint over Streamable HTTP and authenticates with your Google Cloud
credentials.

Ask it things like:

- "What date range does the billing data cover?"
- "Which five services cost the most last month, and how did that change?"
- "Break down the spend of project `my-project` by SKU for the last 30 days."

## What this recipe teaches

The connection pattern is the main lesson, and it carries over to the other
Google Cloud MCP servers (Cloud Logging, Cloud Monitoring, Cloud Run and more).
FinOps is the concrete problem that keeps it useful on its own.

| Lesson | Where | Why it matters |
| --- | --- | --- |
| Connect `McpToolset` to a Google-hosted MCP server | `app/mcp_tools.py` | One URL and your credentials. Nothing to host. |
| Refresh the token on every request with `header_provider` | `gcp_auth_headers` in `app/mcp_tools.py` | An access token lives for about an hour. A token read once at import works in a demo, then every tool call fails in an agent that stays up longer. |
| Give the model a read-only tool allowlist with `tool_filter` | `READONLY_BIGQUERY_TOOLS` in `app/config.py` | A write tool that is never listed cannot be called. An allowlist is safer than a blocklist, which misses a mutating tool with a harmless name. |
| Load credentials on first use, not at import | `gcp_auth_headers` | The agent loads, and the tests run, on a machine with no credentials. |
| Tell the model how the billing export really behaves | `app/prompt.py` | The export belongs to a billing account and not to a project. The agent runs queries in the export's project and filters by the project being asked about, checks what dates the data covers before it answers, and reports only numbers it queried. |

If `BILLING_TABLE` is not set, the agent still starts and tells the user what
to configure. It never makes up a cost figure.

## Requirements

- **uv**: Python package manager - [Install](https://docs.astral.sh/uv/getting-started/installation/)
- **gcloud CLI** with Application Default Credentials:
  ```bash
  gcloud auth application-default login
  ```
- A Google Cloud project with the Agent Platform (formerly Vertex AI) API and
  the BigQuery API enabled. Enabling the BigQuery API also enables its MCP
  server:
  ```bash
  gcloud services enable aiplatform.googleapis.com bigquery.googleapis.com
  ```
- A [Cloud Billing export to BigQuery](https://docs.cloud.google.com/billing/docs/how-to/export-data-bigquery)
  (standard or detailed usage cost). Note the table name, in the form
  `project.dataset.table`.
- IAM roles for the account the agent runs as:

  | Role | On | Why |
  | --- | --- | --- |
  | MCP Tool User (`roles/mcp.toolUser`) | the project in `GOOGLE_CLOUD_PROJECT` | Call tools on Google Cloud MCP servers |
  | BigQuery Job User (`roles/bigquery.jobUser`) | the project that holds the export | Run queries |
  | BigQuery Data Viewer (`roles/bigquery.dataViewer`) | the export dataset | Read the billing data |
  | `roles/aiplatform.user` | the project in `GOOGLE_CLOUD_PROJECT` | Call Gemini |

  ```bash
  gcloud projects add-iam-policy-binding PROJECT_ID \
      --member=user:EMAIL --role=roles/mcp.toolUser
  ```

## Quick Start

> **Note**: All commands below must be run from the recipe root directory (`contrib/python/gcp-mcp-finops-agent/`).

1. Install required packages:
   ```bash
   uv sync
   ```

2. Set up environment variables. Copy `.env.example` to `.env`, then set
   `GOOGLE_CLOUD_PROJECT` and `BILLING_TABLE`:
   ```bash
   cp .env.example .env
   ```

   | Variable | Required | Meaning |
   | --- | --- | --- |
   | `GOOGLE_CLOUD_PROJECT` | yes | Project for Gemini calls. MCP requests are also billed to it. |
   | `BILLING_TABLE` | yes | Billing export table, `project.dataset.table` |
   | `BILLING_PROJECT` | no | Project to run the cost queries in. Defaults to the project in `BILLING_TABLE`. |
   | `MODEL_NAME` | no | Gemini model. Defaults to `gemini-3.5-flash`. |
   | `GOOGLE_CLOUD_LOCATION` | no | Region for Gemini calls. Defaults to `global` in `.env.example`. |
   | `GOOGLE_GENAI_USE_VERTEXAI` | no | Keep `True` to call Gemini through Agent Platform (formerly Vertex AI). |

   The rest of `.env.example` configures the FastAPI server used for Cloud
   Run (CORS, the A2A agent card, session and artifact storage). `adk web` and
   `adk run` ignore it, and the shipped values match the code's defaults.

3. Run the agent in the ADK web UI:
   ```bash
   uv run adk web
   ```
   Open http://localhost:8000 and pick `app`.

4. Or run it in the command line:
   ```bash
   uv run adk run app
   ```

## Running Tests

The unit and runnability tests need no credentials and no network:

```bash
uv run pytest
```

The integration tests call Gemini and the BigQuery MCP server, so they need
the setup above:

```bash
uv run pytest tests/integration
```

The evals in `tests/eval/` score the final answer with a rubric judged by
Gemini, so they also need the setup above. They run through ADK's eval
module, which is an extra, and the config file has to be named because it is
not beside the evalset:

```bash
uv sync --extra eval
uv run adk eval app tests/eval/evalsets/basic.evalset.json \
  --config_file_path tests/eval/eval_config.json
```

## Troubleshooting

| Symptom | Likely cause |
| --- | --- |
| The agent says the billing table is not configured | `BILLING_TABLE` is unset or is not in the form `project.dataset.table` |
| Tool calls fail with 403 | The account is missing `roles/mcp.toolUser`, or the BigQuery API is not enabled on `GOOGLE_CLOUD_PROJECT` |
| Queries fail with "access denied" on the table | Missing BigQuery Data Viewer on the export dataset, or BigQuery Job User on the export's project |
| The agent reports zero spend for a project | The query ran in the wrong project. Cost queries must run in the project that holds the export. Check `BILLING_PROJECT`. |
| The data stops weeks ago | The export lags or was turned off. The agent reports the window it found. Check the export settings in the Billing console. |

## Going further

- Add a second server, for example Cloud Monitoring at
  `https://monitoring.googleapis.com/mcp`, by building another `McpToolset`
  with the same `header_provider`.
- In production, run the agent as a dedicated service account that holds only
  the roles above, not as your user account.

## Maintainer

Vishal Bulbule ([@vishal-bulbule](https://github.com/vishal-bulbule)),
vishal.bulbule@techtrapture.com. Open an issue in this repository and mention
the handle for questions or fixes.
