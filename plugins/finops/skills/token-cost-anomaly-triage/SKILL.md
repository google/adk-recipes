---
name: token-cost-anomaly-triage
description: Diagnose Google Cloud Vertex AI and Cloud Run spend spikes by correlating BigQuery billing exports with OpenTelemetry GenAI token spans, and generate zero-trust remediation plans. Use when asked to investigate GCP agent costs, token burn rates, or FinOps optimization.
---

# Vertex AI & Cloud Run Token Cost Anomaly Triage

## Overview

This skill guides an AI agent through a deterministic 3-stage FinOps workflow on Google Cloud:

1. **Billing & Span Correlation:** Query BigQuery Cloud Billing export (`gcp_billing_export_v1`) alongside OpenTelemetry GenAI spans (`gen_ai.usage.input_tokens`, `gen_ai.usage.output_tokens`, `adk.agent_name`).

2. **Non-Destructive Remediation Planning:** Identify high-volume sub-agents over-provisioned on Pro-tier models without context caching or pinned to excessive Cloud Run `min_instances`.

3. **Zero-Trust Mid-Task Verification:** Enforce mid-task OAuth/IAM revocation checks (`before_tool_callback`) prior to executing any routing or scaling mutation.

## Workflow Steps

1. **Step 1 — Verify Environment Configuration:** Check `.env` for `GOOGLE_CLOUD_PROJECT`, `FINOPS_BQ_BILLING_TABLE`, and `FINOPS_COST_SPIKE_THRESHOLD_USD`.

2. **Step 2 — Run Telemetry Forensics:** Execute `query_bigquery_billing_anomalies` and `inspect_agent_token_telemetry` (or scaffold `contrib/python/finops-zero-trust-governor`).

3. **Step 3 — Validate Zero-Trust Guardrail:** Ensure `verify_token_not_revoked` is attached as `before_tool_callback` on any agent holding write/mutation tools.

4. **Step 4 — Verify with `agents-cli eval`:** Run `agents-cli eval run --config tests/eval/eval_config.yaml` and confirm trajectory accuracy before deployment.
