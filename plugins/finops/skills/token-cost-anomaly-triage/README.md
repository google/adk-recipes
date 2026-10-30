# `token-cost-anomaly-triage`

Autonomous FinOps skill for diagnosing runaway token consumption spikes on Google Cloud.

## Features

- **BigQuery Billing Analytics**: Identifies runaway Vertex AI Gemini and Cloud Run SKUs.
- **OpenTelemetry GenAI Spans**: Analyzes `gen_ai.usage.input_tokens` and `gen_ai.usage.output_tokens` to detect recursive loops.
- **5-Gate Enterprise Governance Harness**: Safeguards all remediation actions through circuit breakers, mid-task auth checks, idempotency, mutex locks, and blast-radius escalation gates.

## Usage

```bash
uv run python scripts/triage.py --project-id my-project --service-name my-agent
```
