# Production Evaluation Architecture

## Overview

This document describes the 5-stage production evaluation architecture for the Customer Support Multi-Agent System. Each stage uses different tools and serves a different purpose in the quality assurance pipeline.

```
┌───────────────────────────────────────────────────────────────────┐
│                    EVALUATION PIPELINE                            │
│                                                                   │
│  Stage 1       Stage 2       Stage 3       Stage 4       Stage 5 │
│  ┌─────┐      ┌─────┐      ┌─────┐      ┌─────┐      ┌─────┐   │
│  │LOCAL│──────│ CI  │──────│STAGE│──────│PROD │──────│NIGHT│   │
│  │ DEV │      │PIPE │      │EVAL │      │GATE │      │ LY  │   │
│  └─────┘      └─────┘      └─────┘      └─────┘      └─────┘   │
│  ADK local    ADK+pytest   Vertex AI    Vertex AI    Vertex AI  │
│  InMemory     InMemory     Eval Svc     Eval Svc     Eval Svc   │
│  Runner       Runner       (deployed)   (deployed)   (scheduled)│
└───────────────────────────────────────────────────────────────────┘
```

## Stage Summary

| Stage | Name | Tool | Trigger |
|-------|------|------|---------|
| 1 | Local Development | ADK AgentEvaluator + InMemoryRunner | Manual (`pytest tests/`) |
| 2 | CI Pipeline | ADK AgentEvaluator + pytest | Every PR and push |
| 3 | Post-Deploy Eval | Vertex AI Gen AI Eval Service | After deploy to staging or prod |
| 4 | Production Pre-Canary Eval Gate | Vertex AI Gen AI Eval Service | After shadow deploy to prod, before canary enable |
| 5 | Nightly Regression | Vertex AI Gen AI Eval Service | Cloud Scheduler (midnight UTC) |

Stages 3, 4, and 5 use the same script (`tests/eval_vertex.py`) against the live Agent Engine. Stage 3 runs after staging deploy; Stage 4 runs against the prod shadow revision before canary is enabled; Stage 5 runs on a nightly schedule.

---

## Stage 1: Local Development

**Purpose:** Fast feedback loop during development.

**Tools:** ADK `AgentEvaluator`, `InMemoryRunner`, mocked Firestore backend

**Metrics:** Rouge-1, tool trajectory (exact match on structured args)

**How to run:**
```bash
# Run unit tests with eval
pytest tests/unit/ -v -s

# Run integration tests
pytest tests/integration/ -v -s

# Use fast profile for quick feedback
EVAL_PROFILE=fast pytest tests/unit/ -v -s
```

**Key files:**
- `tests/unit/test_agent_eval_ci.py`: unit-level agent evals
- `tests/integration/test_integration_eval_ci.py`: integration evals
- `tests/conftest.py`: mock setup for Firestore

---

## Stage 2: CI Pipeline

**Purpose:** Automated quality gate on every PR/push.

**Tools:** ADK `AgentEvaluator` + `InMemoryRunner` + pytest

**Metrics:** Vary by profile:
- `fast` (PR): Rouge-1 only: free, fast
- `standard` (push to main): + tool trajectory (unit), rubric-based LLM judge (integration)
- `full` (nightly/release): + final_response_match_v2

**CI/CD mapping:**
| Event | Profile | Gate |
|-------|---------|------|
| PR to `main` (no agent changes) | `fast` | Must pass to merge |
| PR to `main` (agent code changed) | `standard` | Must pass to merge (auto-detected) |
| Push to `main` (dev deploy) | `standard` | Blocks deployment |
| Tag `v*.*.*-rc.*` (staging) | `standard` | Blocks staging deploy |
| Tag `v*.*.*` (prod release) | `standard` | Post-deploy eval gates canary enable |
| Nightly | `full` | Regression monitoring vs GCS baseline |

**How to run:**
```bash
# Simulate CI profiles locally
EVAL_PROFILE=fast pytest tests/ -v
EVAL_PROFILE=standard pytest tests/ -v
EVAL_PROFILE=full pytest tests/ -v
```

**Key files:**
- `cloudbuild/pr-checks.yaml`, `cloudbuild/cloudbuild-deploy.yaml`: CI pipeline definitions
- `tests/eval_configs/unit/{fast,standard,full}.json`
- `tests/eval_configs/integration/{fast,standard,full}.json`
- `tests/eval_configs/__init__.py`: profile loader

---

## Stage 3: Staging Deployment Eval

**Purpose:** Evaluate the *deployed* agent (not local code) before promoting to production.

**Tools:** Vertex AI Gen AI Evaluation Service (`client.evals`)

**Metrics:**
- `TOOL_USE_QUALITY`: Did the agent use the right tools with correct parameters?
- `FINAL_RESPONSE_QUALITY`: Is the response accurate and helpful?
- `HALLUCINATION` (full profile): Did the agent fabricate information?
- `SAFETY` (full profile): Is the response safe and appropriate?

**How it works:**
1. Deploy agent to staging Agent Engine
2. Run `eval_vertex.py` against the staging deployment
3. Script sends prompts → collects responses → runs Vertex AI eval metrics
4. HTML report saved locally as `eval-TIMESTAMP.html` (e.g. `eval-20260306-152928.html`)
5. If `GOOGLE_CLOUD_STORAGE_BUCKET` is set: report uploaded to `gs://BUCKET/eval-reports/eval-TIMESTAMP.html`
6. Results logged to Vertex AI Experiments as run `eval-TIMESTAMP`; GCS URI recorded as a param in the run: all three (file, GCS path, experiment run) share the same timestamp for easy correlation
7. If all metrics pass thresholds → promote to production
8. If any metric fails → block promotion, alert team

**How to run:**
```bash
# Standard eval via make (recommended)
make eval-post-deploy ENV=staging

# Standard eval (tool use + response quality) — multi-agent setup
python tests/eval_vertex.py \
    --agent-engine-id projects/PROJECT/locations/LOCATION/reasoningEngines/ENGINE_ID \
    --custom-inference

# Full eval (+ hallucination + safety)
python tests/eval_vertex.py \
    --agent-engine-id projects/PROJECT/locations/LOCATION/reasoningEngines/ENGINE_ID \
    --custom-inference \
    --profile full

# Save results + debug inference (includes intermediate_events)
python tests/eval_vertex.py \
    --agent-engine-id projects/PROJECT/locations/LOCATION/reasoningEngines/ENGINE_ID \
    --custom-inference \
    --output eval_results.json \
    --save-inference inference_debug.json

# Dump SDK intermediate_events format to file (for debugging/format research)
python tests/eval_vertex.py \
    --agent-engine-id projects/PROJECT/locations/LOCATION/reasoningEngines/ENGINE_ID \
    --inspect-sdk-events sdk_events.json

# Adjust delay between prompts (default 3s, increase for rate limit issues)
python tests/eval_vertex.py \
    --agent-engine-id projects/PROJECT/locations/LOCATION/reasoningEngines/ENGINE_ID \
    --custom-inference \
    --delay 8.0
```

**CLI flags:**

| Flag | Default | Description |
|------|---------|-------------|
| `--agent-engine-id` | required | Full resource name of the deployed Agent Engine |
| `--profile` | `standard` | Eval config profile (`standard` or `full`) |
| `--dataset` | `post_deploy_cases.json` | Path to eval dataset JSON |
| `--output` | none | Save results to JSON file |
| `--delay` | `3.0` | Seconds between prompts (rate limit protection) |
| `--custom-inference` | off | Use custom `async_stream_query()` adapter (required for multi-agent AgentTool) |
| `--save-inference` | none | Save raw prompt/response/intermediate_events to JSON for debugging |
| `--inspect-sdk-events` | none | Run SDK inference, dump raw `intermediate_events` to FILE, then exit (format research) |
| `--sdk-inference` | off | Legacy alias for `--custom-inference` |
| `--update-baseline` | none | GCS path to baseline JSON (`gs://...`). Compares composite score; saves updated baseline on pass; exits 1 on regression |
| `--regression-threshold` | `0.10` | Max allowed relative composite score drop vs baseline (nightly only) |

**Key files:**
- `tests/eval_vertex.py`: main eval script
- `tests/eval_configs/post_deploy/{standard,full}.json`: metric configs
- `tests/post_deploy/datasets/post_deploy_cases.json`: eval dataset (10 cases)
- `tests/post_deploy/dataset_converter.py`: ADK → Vertex AI format converter

### Custom Inference Adapter vs SDK Inference

Pass `--custom-inference` for multi-agent systems that use `AgentTool`. Without it, the SDK's built-in `run_inference()` is used, which fails on AgentTool.

**Why the SDK's `run_inference()` fails for AgentTool:**

The SDK's internal parser extracts the final response with:
```python
resp_item[-1]["content"]["parts"][0]["text"]
```

With `AgentTool`, the conversation flow is:
1. Root agent emits a `function_call` event (delegating to sub-agent)
2. Sub-agent returns a `function_response` event (with result text)
3. Root agent emits a final `text` event (human-readable response)

The SDK stops at event 2 and fails to parse it as text. The custom adapter uses `async_stream_query()` which yields all events, matching how the production backend works.

```
SDK run_inference():   function_call → function_response → STOPS (parse error)
Custom adapter:        function_call → function_response → text response → DONE
Production backend:    function_call → function_response → text response → DONE
```

> `stream_query()` (sync) also has issues: it only yields the first event for AgentTool calls. The custom adapter always uses `async_stream_query()`.

**intermediate_events and TOOL_USE_QUALITY:**

The custom adapter preserves `thought_signature` and `function_call.id` in intermediate events — these fields are required by the `TOOL_USE_QUALITY` rubric evaluator. Stripping them causes the judge to see an empty trajectory and score ~0.02.

**When to use SDK inference (no `--custom-inference`):**
- Single-agent systems that don't use `AgentTool`
- Use `--inspect-sdk-events FILE` to dump what the SDK captures for format comparison

### Resilience Features

The script includes retry logic for common transient failures:

- **Agent Engine 503s:** `async_stream_query()` sometimes returns `503 UNAVAILABLE` (gRPC connection issues). The adapter retries up to 3 times with exponential backoff (5s, 10s).
- **Polling SSL errors:** `get_evaluation_run()` can hit transient SSL/connection errors during the polling loop. Up to 5 consecutive failures are tolerated before giving up.

### Judge Rate Limits

The eval service uses Gemini as an LLM judge to score responses. With 10 items and 2 metrics, that's 20 judge calls which can hit `RESOURCE_EXHAUSTED` rate limits. Items that fail at the judge level show as `failed_items` in the results: they are excluded from scoring, not counted as quality failures.

**Tips to improve judge success rate:**
- Use smaller datasets (3-5 items) for more reliable scoring
- Increase `--delay` between prompts
- Run at off-peak hours

### Prerequisites

Before running post-deploy eval:

1. **Agent Engine deployed** with a valid resource name
2. **Firestore permissions:** The Vertex AI service agent (`service-PROJECT_NUMBER@gcp-sa-aiplatform.iam.gserviceaccount.com`) must have `roles/datastore.user` to access Firestore:
   ```bash
   gcloud projects add-iam-policy-binding PROJECT_ID \
     --member="serviceAccount:service-PROJECT_NUMBER@gcp-sa-aiplatform.iam.gserviceaccount.com" \
     --role="roles/datastore.user"
   ```
3. **GCS bucket** for report upload: set `GOOGLE_CLOUD_STORAGE_BUCKET` in `.env`: the script uploads the HTML report to `gs://BUCKET/eval-reports/eval-TIMESTAMP.html` and records the URI in the Vertex AI Experiments run. Without this, the report is saved locally only and the experiment run will have no `report_gcs_uri` param.
4. **Dataset IDs must match seeded Firestore data**: use real order/invoice IDs (e.g., `ORD-12345`, `ORD-67890`, `INV-2025-001`), not placeholder IDs

### AgentInfo Workaround

The eval service requires `AgentInfo` to provide tool declarations and agent instructions. The standard method `AgentInfo.load_from_agent()` fails for this project because:
- ADK `PreloadMemoryTool` lacks `__globals__` → `typing.get_type_hints()` crashes
- Sub-agents wrapped as `AgentTool` cause recursive introspection failures

The script builds `AgentInfo` manually instead, intentionally omitting `agent_resource_name`:
```python
agent_info = types.evals.AgentInfo(
    name=root_agent.name,
    instruction=root_agent.instruction or "",
    # agent_resource_name is intentionally omitted: when set, evaluate() re-runs
    # its own SDK inference which breaks on AgentTool and returns empty responses.
)
```

---

## Stage 4: Production Pre-Canary Eval Gate

**Purpose:** Eval gate against the shadow revision before enabling canary traffic. The new revision is deployed with `--no-traffic` — real users are unaffected. If eval fails, the shadow stays at 0% and the canary is never enabled.

**Tools:** Same as Stage 3 (`eval_vertex.py`)

**How it differs from Stage 3:**
- Runs against the shadow revision URL (prod, 0% traffic), not 100% traffic
- Runs with `standard` profile (same as staging)
- If eval fails → pipeline stops, shadow stays at 0%, no canary is enabled, no rollback needed

**How to run:**
```bash
python tests/eval_vertex.py \
    --agent-engine-id projects/PROJECT/locations/LOCATION/apps/PROD_APP_ID \
    --dataset tests/post_deploy/datasets/post_deploy_cases.json
```

---

## Stage 5: Nightly Regression

**Purpose:** Detect model drift and quality degradation over time. Also drives canary promotion/rollback.

**Tools:** Same as Stage 3 (`eval_vertex.py`), run on a schedule via Cloud Scheduler

**How the decision works:**

Scores are combined into a **composite weighted score**: Tool Use Quality (40%) + Final Response Quality (40%) + Hallucination (10%) + Safety (10%). This absorbs LLM judge variance across metrics — a single-case flip in one metric won't trigger a false rollback.

The composite score is compared against the GCS baseline (`$_STAGING_BUCKET/baselines/nightly-baseline.json`). If the relative drop exceeds `_REGRESSION_THRESHOLD` (default 10%), the build fails and the canary is rolled back.

**Agent Engine resolution:** The nightly reads `AGENT_ENGINE_RESOURCE_NAME` directly from the active Cloud Run revision's env vars — canary if one is live, champion otherwise. This is always accurate and self-corrects after rollback without manual intervention.

**How to run:**
```bash
# Nightly full regression (manual trigger)
python tests/eval_vertex.py \
    --agent-engine-id ENGINE_ID \
    --profile full \
    --custom-inference \
    --output /workspace/eval_scores.json \
    --update-baseline gs://YOUR_BUCKET/baselines/nightly-baseline.json \
    --regression-threshold 0.10 \
    --delay 5
```

---

## Tool Comparison: ADK AgentEvaluator vs Vertex AI Eval Service

| Feature | ADK AgentEvaluator | Vertex AI Eval Service |
|---------|-------------------|----------------------|
| **Runs against** | Local agent (InMemoryRunner) | Deployed Agent Engine app |
| **Speed** | Fast (in-process) | Slower (network calls) |
| **Cost** | Free (local compute) | Vertex AI pricing |
| **Metrics** | Rouge-1, tool trajectory, response match, rubric judges | TOOL_USE_QUALITY, FINAL_RESPONSE_QUALITY, HALLUCINATION, SAFETY |
| **Use case** | Dev/CI (Stages 1-2) | Post-deploy (Stages 3-4, 6) |
| **Mocking** | Mocked backends | Real backends (Firestore, etc.) |
| **Environment** | Local / CI runner | GCP project with Agent Engine |

**When to use which:**
- **ADK AgentEvaluator**: Development and CI: fast, free, tests agent logic
- **Vertex AI Eval Service**: Post-deployment: tests the full deployed stack including infrastructure, latency, and real data access

---

## Eval Profile System

All stages support the `EVAL_PROFILE` environment variable:

| Profile | Unit Metrics | Integration Metrics | Post-Deploy Metrics |
|---------|-------------|--------------------|--------------------|
| `fast` | Rouge-1 | Rouge-1 | |
| `standard` | Rouge-1 + tool trajectory | Rouge-1 + rubric judge | TOOL_USE_QUALITY + FINAL_RESPONSE_QUALITY |
| `full` | + response match v2 | + response match v2 | + HALLUCINATION + SAFETY |

**CI/CD mapping:**
```
PR          → fast       (quick feedback, free)
Push main   → standard   (balanced quality gate)
Nightly     → full       (comprehensive regression)
Release     → standard   (gates canary enable)
Post-deploy → standard   (deployed agent quality)
```

---

## Dataset Format

### Post-Deploy Dataset (`post_deploy_cases.json`)

Simple JSON array for the Vertex AI Eval Service:

```json
[
  {
    "prompt": "Where is my order ORD-12345?",
    "reference": "Your order ORD-12345 is currently in transit via FastShip.",
    "expected_tool_use": [
      {"tool_name": "order_agent", "tool_input": {"request": "track order ORD-12345"}}
    ]
  }
]
```

**Important:** The `"prompt"` column name is required: the eval service SDK looks for this exact key when building `EvaluationItemRequest` objects. Using `"request"` instead will cause all items to fail with `INTERNAL` errors.

**Dataset IDs must match seeded Firestore data:**

| Entity | Valid IDs (demo-user-001) |
|--------|--------------------------|
| Orders | `ORD-12345` (In Transit), `ORD-67890` (Delivered, refundable), `ORD-11111` (Delivered, past window) |
| Invoices | `INV-2025-001` (Pending), `INV-2025-002` (Paid), `INV-2024-003` (Paid) |
| Products | `PROD-001` through `PROD-006` |

### ADK EvalSet (`.evalset.json`)

Use the dataset converter to transform ADK evalsets to Vertex AI format:

```python
from tests.post_deploy.dataset_converter import adk_evalset_to_dataframe

df = adk_evalset_to_dataframe("tests/integration/product_agent_handoffs.evalset.json")
```

---

## Thresholds

### Standard Profile
| Metric | Threshold | Rationale |
|--------|-----------|-----------|
| TOOL_USE_QUALITY | 0.5 | Agent uses correct tools for >50% of queries |
| FINAL_RESPONSE_QUALITY | 0.5 | Responses are accurate and helpful >50% of time |

### Full Profile
| Metric | Threshold | Rationale |
|--------|-----------|-----------|
| TOOL_USE_QUALITY | 0.5 | Same as standard |
| FINAL_RESPONSE_QUALITY | 0.5 | Same as standard |
| HALLUCINATION | 0.5 | Agent doesn't fabricate information |
| SAFETY | 0.8 | Higher bar: safety is critical |

Thresholds should be adjusted upward as the agent matures. Start conservative and ratchet up.
