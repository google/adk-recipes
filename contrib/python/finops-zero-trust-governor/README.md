# FinOps Zero-Trust Governor

An autonomous 3-stage FinOps & Zero-Trust Governance pipeline built with Google ADK that correlates BigQuery billing spikes with OpenTelemetry GenAI token spans and enforces mid-task OAuth revocation checks (before_tool_callback) prior to executing infrastructure remediations.

## Architecture Diagram

```
[ Anomaly Alert / User Request ]
                │
                ▼
┌─────────────────────────────────────────────────────────────┐
│          finops_governor_pipeline (SequentialAgent)         │
│          Wrapped with ResumabilityConfig(is_resumable=True) │
├─────────────────────────────────────────────────────────────┤
│ 1. telemetry_investigator (LlmAgent)                        │
│    - query_bigquery_billing_anomalies                       │
│    - inspect_agent_token_telemetry                          │
│    - output_key: "anomaly_diagnosis"                        │
│                                                             │
│ 2. remediation_planner (LlmAgent)                           │
│    - Downscaling savings calculation                        │
│    - Model routing recommendation                           │
│    - output_key: "remediation_plan"                         │
│                                                             │
│ 3. zero_trust_executor (LlmAgent)                           │
│    - apply_model_routing_downgrade                          │
│    - update_cloud_run_scaling_limits                        │
│    - guarded by verify_token_not_revoked                    │
│    - output_key: "execution_summary"                        │
└─────────────────────────────────────────────────────────────┘
                │
                ▼
┌─────────────────────────────────────────────────────────────┐
│        Unified 5-Gate Enterprise Governance Harness         │
│             (app/callbacks/mid_task_auth_guard.py)          │
├─────────────────────────────────────────────────────────────┤
│ Gate 1: Token & Retry Circuit Breaker (before_model)        │
│ Gate 2: Mid-Task OAuth Revocation Guard (Issue #2769)       │
│ Gate 3: Crash-Resumable Idempotency Replay Guard            │
│ Gate 4: Concurrent Workload Mutex Lock                      │
│ Gate 5: Blast-Radius HITL Escalation Threshold Gate         │
└─────────────────────────────────────────────────────────────┘
```

## Key ADK Patterns Demonstrated

- **Deterministic Multi-Agent Orchestration**: Demonstrates `SequentialAgent` chaining `telemetry_investigator`, `remediation_planner`, and `zero_trust_executor` with state passing via `output_key` (`anomaly_diagnosis` -> `remediation_plan` -> `execution_summary`).
- **Mid-Task Revocation Guardrail**: Solves GitHub Issue #2769 via ADK's `before_tool_callback` (`verify_token_not_revoked`) which verifies token validity immediately before executing state mutations, halting destructive operations if permissions were revoked mid-run.
- **Dual-Mode Telemetry Seam**: Features `INTEGRATION_TEST=TRUE` zero-credential execution seam enabling complete CI unit and runnability testing offline while supporting live BigQuery Cloud Billing and Cloud Trace queries in production.
- **Companion Agent Plugin**: Ships with companion vertical skill plugin in `plugins/finops/skills/token-cost-anomaly-triage` for interactive developer-in-the-loop triage.

## Prerequisites & Environment Setup

Configure the environment variables by creating `.env` from `.env.example`:

| Environment Variable | Description | Default / Example Value |
|:---|:---|:---|
| `GOOGLE_GENAI_USE_VERTEXAI` | Enables Vertex AI backend for Google GenAI SDK | `TRUE` |
| `GOOGLE_CLOUD_PROJECT` | Target Google Cloud Project ID | `your-gcp-project-id` |
| `GOOGLE_CLOUD_LOCATION` | Target Google Cloud Region | `us-central1` |
| `GEMINI_MODEL` | Gemini model name for all agents | `gemini-3.7-flash` |
| `FINOPS_BQ_BILLING_TABLE` | Full BigQuery Cloud Billing export table identifier | `your-gcp-project-id.billing_export.gcp_billing_export_v1` |
| `FINOPS_COST_SPIKE_THRESHOLD_USD` | Hourly spend spike threshold (USD) to trigger triage | `250.0` |
| `FINOPS_MAX_LLM_TURNS_PER_RUN` | Gate 1 Circuit Breaker maximum turn threshold | `12` |
| `ZERO_TRUST_TOKENINFO_URL` | Endpoint to re-verify OAuth token validity | `https://oauth2.googleapis.com/tokeninfo` |
| `ZERO_TRUST_ENFORCE_REVOCATION` | Toggles mid-task OAuth revocation enforcement | `TRUE` |
| `INTEGRATION_TEST` | Enables zero-credential mock seam for offline CI | `TRUE` |
| `AGENT_VERSION` | Version of the agent container for A2A metadata | `0.1.0` |
| `ALLOW_ORIGINS` | Permitted CORS origins for the FastAPI server | `["*"]` |
| `APP_URL` | Base public URL where the agent service is reachable | `http://localhost:8080` |
| `GOOGLE_CLOUD_AGENT_ENGINE_ID` | Optional Vertex AI Agent Engine instance ID | `""` |
| `GOOGLE_CLOUD_AGENT_ENGINE_LOCATION` | Optional Vertex AI Agent Engine region | `us-central1` |
| `LOGS_BUCKET_NAME` | Cloud Storage bucket for audit and runtime logs | `""` |
| `SESSION_SERVICE_URI` | Custom URI for external persistent session storage | `""` |

## Quickstart (Local CLI, Web UI & Pytest)

```bash
# 1. Copy environment template
cp .env.example .env

# 2. Synchronize project dependencies
uv sync

# 3. Run zero-credential runnability tests
INTEGRATION_TEST=TRUE uv run pytest tests/test_runnability.py

# 4. Launch the ADK Web UI
uv run adk web
```

## Evaluation with agents-cli

Evaluate multi-turn trajectory accuracy and tool call conformance with `agents-cli`:

```bash
agents-cli eval run --config tests/eval/eval_config.yaml
```

## Container Deployment (Cloud Run / Agent Engine)

This recipe is packaged as a deployable container application with A2A protocol and FastAPI endpoints:

```bash
# Verify container build locally
docker build -t finops-zero-trust-governor .

# Run the container
docker run -p 8080:8080 -e INTEGRATION_TEST=TRUE finops-zero-trust-governor

# Deploy using agents-cli to Cloud Run / Agent Engine
agents-cli deploy --service-name finops-zero-trust-governor
```
