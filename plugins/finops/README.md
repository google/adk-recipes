# `plugins/finops`

The FinOps and Zero-Trust Governance plugin for Google Cloud, following the
[Google Agent Plugins v1.0.0 specification](https://agent-plugins.org/specification).

## Purpose

Teaches AI coding assistants (Gemini Enterprise, Antigravity, Claude Code, Cursor) how to:
1. Connect to **BigQuery Cloud Billing exports** and **Cloud Trace / OpenTelemetry GenAI spans**.
2. Triage runaway token spikes (`gen_ai.usage.input_tokens`, `gen_ai.usage.output_tokens`).
3. Scaffold deterministic FinOps remediation pipelines protected by a **5-Gate Enterprise Governance Harness**.

## Included Skills

| Skill | Description | Companion Deployable Recipe |
|---|---|---|
| [`token-cost-anomaly-triage`](./skills/token-cost-anomaly-triage/) | Autonomous triage of runaway token costs and 5-gate zero-trust remediation. | [`contrib/python/finops-zero-trust-governor`](../../contrib/python/finops-zero-trust-governor/) |

## Installation

Install directly via `npx skills add`:

```bash
npx skills add google/adk-recipes --skill token-cost-anomaly-triage
```

Or run locally from this directory:

```bash
cd plugins/finops
make surface
```
