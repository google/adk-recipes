# Brand Search Optimization

This recipe is an Agent Development Kit (ADK) multi-agent workflow that helps e-commerce brands optimize product titles for search visibility and recover zero/low-result retail queries using BigQuery and Gemini Computer Use.

## What This Recipe Does

- **Extracts Brand Catalog Data**: Queries product titles, descriptions, and attributes from BigQuery with typed tool parameters.
- **Identifies High-Intent Search Terms**: Mines high-value shopper keywords (category, style, attribute, use-case) to find what shoppers actually search for.
- **Visual Retail Search via Computer Use**: Navigates retail search engines using **Gemini Computer Use** (powered by Playwright and ADK `ComputerUseToolset`), capturing screenshots and observing how top organic competitor products structure their titles.
- **Structured Title Evaluation & Scoring**: Evaluates keyword gaps, calculates searchability scores (0-100), and generates structured, high-converting product title recommendations.

## Architecture

The workflow coordinates specialized agents:

1. `keyword_finding_agent`: queries BigQuery product catalog data and ranks high-intent search keywords.
2. `search_results_agent`: visual browser agent using **Gemini Computer Use** to navigate retail search engines and observe competitor title structures.
3. `comparison_root_agent`: coordinates generation and critique of title optimizations to deliver a structured `TitleOptimizationReport`.

## Prerequisites

- Python 3.11 - 3.12
- `uv` installed: https://docs.astral.sh/uv/
- Google Cloud project with Vertex AI and BigQuery access
- Application Default Credentials:

```bash
gcloud auth application-default login
```

## Setup

1. Clone the repository and navigate to this recipe directory:

```bash
git clone https://github.com/google/adk-recipes.git
cd adk-recipes/contrib/python/brand-search-optimization
```

2. Create your environment file:

```bash
cp .env.example .env
```

3. Sync dependencies and install Playwright browser binaries:

```bash
uv sync --dev
uv run playwright install chromium
```

4. (Optional) Populate sample BigQuery catalog data:

```bash
uv run python -m deployment.bq_populate_data
```

## Run The Agent

### CLI Mode

```bash
uv run adk run brand_search_optimization
```

### Web UI Mode

```bash
uv run adk web
```

Then select `brand_search_optimization` from the application dropdown.

## Evaluation

Run the evaluation suite:

```bash
uv run adk eval brand_search_optimization eval/data/eval_data1.evalset.json --config_file_path eval/data/test_config.json
```

## Tests, Lint, and Type Checking

Run unit and runnability tests:

```bash
uv run pytest -v
```

Run Ruff linting and formatting:

```bash
uv run ruff check . --fix
uv run ruff format .
```

Run type checking:

```bash
uv run mypy .
```

## Deployment

Deploy the agent to Vertex AI Agent Engine:

```bash
uv sync --group deployment
uv run python deployment/deploy.py --create
```

For post-deployment session testing, see `deployment/test_deployment.py`.

## Configuration

Environment variables are declared in `.env.example`:

- `GOOGLE_GENAI_USE_VERTEXAI`: Set to `1` for Vertex AI backend, `0` for Google AI Studio
- `GOOGLE_API_KEY`: Google AI Studio API key (when using AI Studio backend)
- `GOOGLE_CLOUD_PROJECT`: Google Cloud project ID
- `GOOGLE_CLOUD_LOCATION`: Vertex AI location (e.g., `us-central1` or `global`)
- `MODEL`: Model name (e.g., `gemini-3.5-flash`)
- `DATASET_ID`: BigQuery dataset ID (default: `products_data_agent`)
- `TABLE_ID`: BigQuery table ID (default: `shoe_items`)
- `DISABLE_WEB_DRIVER`: Set to `1` to run in offline/mock browser mode for headless testing (default: `0`)
- `STAGING_BUCKET`: GCS staging bucket for Cloud deployment
- `AGENT_VERSION`: Version string advertised in A2A agent card (default: `0.1.0`)
- `ALLOW_ORIGINS`: Allowed CORS origins for FastAPI server (comma-separated)
- `APP_URL`: Base URL advertised in A2A agent card (default: `http://0.0.0.0:8080`)
- `GOOGLE_CLOUD_AGENT_ENGINE_ID`: Agent Engine resource ID for remote session service
- `GOOGLE_CLOUD_AGENT_ENGINE_LOCATION`: Agent Engine location/region (e.g., `us-central1`)
- `LOGS_BUCKET_NAME`: GCS bucket for remote artifact storage
- `SESSION_SERVICE_URI`: URI for ADK session service (e.g., `shared://session`)
- `ARTIFACT_SERVICE_URI`: URI for ADK artifact service (e.g., `shared://artifact`)
- `PORT`: HTTP port for FastAPI server (default: `8080`)

## Example Interaction

See `tests/example_interaction.md` for a complete example interaction trace.

## Security Notes

The Computer Use agent drives a real Chromium instance, so any URL the model
emits becomes an outbound request from wherever the agent runs.
`brand_search_optimization/tools/browser_computer.py` filters navigation
targets before handing them to Playwright: it allows only `http`/`https`,
rejects loopback, private, link-local and metadata hosts, rejects URLs whose
host Chromium and `urllib.parse` would disagree about (embedded control
characters, backslashes, userinfo, non-ASCII labels), and resolves the hostname
to check the resulting addresses.

**That filtering is defence in depth, not a security boundary.** The validator
and the browser resolve DNS independently, so an attacker who controls an
authoritative name server can answer with a public address for the check and a
private one for Chromium (DNS rebinding). Closing that gap requires a control
below the application:

- Run the agent in a VPC whose egress firewall denies RFC1918, loopback and
  `169.254.0.0/16` (including `169.254.169.254`), or
- Launch Chromium behind an egress proxy that enforces a destination allowlist,
  and block direct egress from the container.

Treat the recipe's own checks as a way to fail fast and log, not as the thing
keeping the metadata server unreachable.

## Disclaimer

This recipe is for educational and prototyping use. It is not production hardened and should be reviewed, tested, and secured before production deployment.
