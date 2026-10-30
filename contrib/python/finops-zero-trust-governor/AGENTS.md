# Agent Guidelines: finops-zero-trust-governor

## Purpose & End-to-End Flow

This recipe implements an autonomous FinOps governance workflow across three sequential sub-agents using session state handoffs:

1. `telemetry_investigator`: Queries BigQuery billing exports and OpenTelemetry GenAI token spans, storing findings into `session.state["anomaly_diagnosis"]`.
2. `remediation_planner`: Evaluates `session.state["anomaly_diagnosis"]`, determines optimal model routing and instance scaling, and writes the plan into `session.state["remediation_plan"]`.
3. `zero_trust_executor`: Reads `session.state["remediation_plan"]` and executes mutations (`apply_model_routing_downgrade`, `update_cloud_run_scaling_limits`). All mutations pass through `verify_token_not_revoked` (`before_tool_callback`), which logs every verification and block to `session.state["security_audit_log"]`.

## Most Interesting Files to Study (in order)

1. `app/callbacks/mid_task_auth_guard.py` — The `before_tool_callback` implementation for mid-task token revocation (Issue #2769), token circuit breaker, idempotency replay protection, mutex locks, and HITL gates.
2. `app/agent.py` — The `SequentialAgent` pipeline composition and `output_key` handoffs.
3. `app/tools/telemetry_bq_tools.py` — The `INTEGRATION_TEST=TRUE` zero-credential seam and BigQuery billing query logic.

## Where to Run Things

| Action | Command |
|:---|:---|
| Run Unit & Runnability Tests | `INTEGRATION_TEST=TRUE uv run pytest -v` |
| Run CLI Agent Interactively | `uv run adk run app` |
| Launch ADK Web UI | `uv run adk web` |
| Run Trajectory Flywheel Eval | `agents-cli eval run --config tests/eval/eval_config.yaml` |
