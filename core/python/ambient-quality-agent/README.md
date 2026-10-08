# Ambient Quality Agent (AQuA)

AQuA gives production ADK agents continuous observability and autonomous
diagnosis from the telemetry they already emit. It is an ADK agent that runs
beside the agent it observes, evaluates production conversations in BigQuery,
and tracks recurring defects as insights. It works with ADK agents that emit
traces through the [BigQuery Agent Analytics plugin](https://adk.dev/integrations/bigquery-agent-analytics/),
Cloud Telemetry with a [linked BigQuery dataset (`_AllSpans`)](https://docs.cloud.google.com/trace/docs/analytics-query-linked-dataset),
or [`agents-cli` default telemetry](https://google.github.io/agents-cli/guide/observability/cloud-trace/).

## Features

- **Multiple telemetry sources:** Reads conversations from the ADK [BigQuery
  Agent Analytics plugin](https://adk.dev/integrations/bigquery-agent-analytics/)
  (`big_query`), Cloud Telemetry [linked BigQuery datasets
  (`_AllSpans`)](https://docs.cloud.google.com/trace/docs/analytics-query-linked-dataset)
  (`cloud_ops`), and [`agents-cli` default
  telemetry](https://google.github.io/agents-cli/guide/observability/cloud-trace/)
  (`cloud_logging`).
- **Scheduled, deployment-triggered, and on-demand trace evaluation:** Runs
  investigations on a schedule, after each deployment of the observed agent, or
  on demand (`agents-cli aqua schedule-investigation`), or over a custom
  conversation slice you describe in chat.
- **Session review and custom metrics:** Reviews each conversation with an LLM
  against the agent's configuration and your evaluation goal, runs deterministic
  Python checks you publish (`agents-cli aqua metrics publish`), or scores
  sessions with evaluation metrics in Gemini platform.
- **Deduplicated insights across runs:** Groups failures into insights with
  status (`NEW`, `RECURRING`, `RESOLVED`), occurrence history, and failing
  conversation traces, so a recurring problem appears once with its history
  instead of as a new finding on every run.
- **Web dashboard for insights and traces:** Explores insights, failing
  conversation traces and tool calls, investigation runs, daily trends, and
  evaluation settings, and includes a chat interface. Runs on Cloud Run behind
  Identity-Aware Proxy (IAP), or locally with `make standalone` and `make demo`.
- **Root-cause analysis and agent memory:** Diagnoses insights in chat or from
  the CLI against the observed agent's published source snapshot and proposes
  fixes, while retaining facts you ask AQuA to remember about your agent.
- **Flexible `agents-cli` integration and coding-agent skill:** Installs as an
  [agents-cli extension](https://google.github.io/agents-cli/guide/extensions/)
  without changing your agent's code. Deploy AQuA beside an `agents-cli` agent
  (`--apply-aqua`, `--deploy-aqua`), or attach an existing agent to an AQuA
  deployed on its own (`agents-cli aqua attach`). Includes the
  `agents-cli-aqua` skill so a coding agent can deploy AQuA, inspect insights,
  and fix defects.

## Prerequisites

- Python 3.11 to 3.13
- [`uv`](https://docs.astral.sh/uv/) and `npm`
- [agents-cli](https://google.github.io/agents-cli/) 1.6 or later
- Terraform
- A Google Cloud project that belongs to an organization, with
  `GOOGLE_CLOUD_PROJECT` set to it and Application Default Credentials
  configured. The dashboard is protected by Identity-Aware Proxy (IAP), which
  needs the organization's managed OAuth client. Without an organization, set
  `TF_VAR_ui_iap_enabled=false` and open the dashboard through
  `agents-cli aqua ui-proxy`.

## Quickstart: add AQuA to your agent

Run these commands in your agent's agents-cli project:

```sh
agents-cli extension add google/adk-recipes#core/python/ambient-quality-agent \
  --ref ambient-quality-agent/v0.1.0
agents-cli install
```

The extension is vendored into `extensions/aqua/`. Commit that directory and
`agents-cli-extensions.yaml` so that CI deploys the pinned revision. The
extension adds AQuA to three commands:

| Command | What AQuA adds |
|---|---|
| `agents-cli infra single-project --apply-aqua` | AQuA's Terraform, applied after your agent's infrastructure |
| `agents-cli deploy --deploy-aqua` | The AQuA engine and its dashboard, deployed after your agent |
| `agents-cli aqua` | The AQuA CLI, configured for this project's deployment |

Without `--apply-aqua` and `--deploy-aqua`, `infra single-project` and `deploy`
work on your agent alone.

## Deploy

```sh
export TF_VAR_ui_iap_members='["user:you@example.com"]'  # who may open the dashboard
agents-cli infra single-project --project="${GOOGLE_CLOUD_PROJECT}" --apply-aqua
agents-cli infra single-project --project="${GOOGLE_CLOUD_PROJECT}" --apply-aqua --apply
agents-cli deploy --project="${GOOGLE_CLOUD_PROJECT}" --deploy-aqua
agents-cli aqua info --ui-url   # prints the dashboard URL
```

The dashboard runs on Cloud Run behind IAP. Nobody can open it until they are
listed in `TF_VAR_ui_iap_members` or granted `roles/iap.httpsResourceAccessor`.

To remove AQuA, run the same `infra` command with `--destroy --apply`. Your
agent's own infrastructure is left in place.

## Usage

The query subcommands of `agents-cli aqua` print JSON:

```sh
agents-cli aqua show-config
agents-cli aqua schedule-investigation --wait
agents-cli aqua list-insights --status NEW
agents-cli aqua get-insight <insight_id>
agents-cli aqua list-memories             # what you asked AQuA to remember
agents-cli aqua dump-traces --since 36h   # AQuA's own spans, as a zip
```

## Run locally

`make standalone` runs AQuA from a checkout of this directory against your own
deployed agent, with nothing of AQuA deployed. It uses the real models, keeps
its storage in the agent process, and serves the dashboard on
http://localhost:8080. Its Application Default Credentials need Gemini platform
model access and read access to the observed agent's telemetry dataset (and,
for the `cloud_ops` and `cloud_logging` sources, to the bucket the rows point
into).

```sh
make install
cp .env.example src/ambient_quality_agent/.env
# Set AQA_OBSERVED_AGENT_NAME, GOOGLE_CLOUD_PROJECT, and the telemetry source,
# dataset and table your agent writes to.
make standalone
```

`make demo` shows the dashboard on a generated month of data, with no Google
Cloud project needed.

## Feedback

Report bugs and suggestions as issues in
[google/adk-recipes](https://github.com/google/adk-recipes/issues).
