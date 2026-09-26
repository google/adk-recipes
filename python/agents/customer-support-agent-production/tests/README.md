# Customer Support Multi-Agent System - Test Suite

Comprehensive test suite for validating the customer support multi-agent system built with Google ADK and Vertex AI.

---

## Test Structure

```
tests/
├── conftest.py                          # Root fixtures: mock Firestore, mock RAG, mock verification
├── test_config.json                     # Global evaluation criteria (for adk eval CLI)
├── mock_firestore.py                    # In-memory Firestore (seed data)
├── mock_rag_search.py                   # Keyword-based RAG search (no embeddings)
│
├── eval_configs/                        # Switchable eval profiles (EVAL_PROFILE env var)
│   ├── __init__.py                      # load_eval_config(), load_eval_set(), get_eval_profile()
│   ├── unit/
│   │   ├── fast.json                    # Rouge-1 only (free)
│   │   ├── standard.json               # + tool_trajectory (free, default)
│   │   └── full.json                    # + final_response_match_v2 (LLM judge)
│   ├── integration/
│   │   ├── fast.json                    # Rouge-1 only (free)
│   │   ├── standard.json               # + rubric_based_tool_use (LLM judge, default)
│   │   └── full.json                    # + final_response_match_v2 (LLM judge)
│   └── post_deploy/
│       ├── standard.json               # TOOL_USE_QUALITY + FINAL_RESPONSE_QUALITY
│       └── full.json                    # + HALLUCINATION + SAFETY
│
├── unit/                                # Unit Tests (Layer 1-2)
│   ├── conftest.py                      # Vertex AI init + mock_backends autouse fixture
│   ├── test_config.json                 # Unit criteria for adk eval CLI (backward compat)
│   │
│   │  # LLM-based agent eval tests (ADK AgentEvaluator, num_runs=2)
│   ├── test_agent_eval_ci.py            # Agent eval test runner (7 test classes)
│   ├── product_agent_direct.test.json   # Product agent: search, details, inventory, reviews (15 cases)
│   ├── order_agent_direct.test.json     # Order agent: track, history, auth (10 cases)
│   ├── billing_direct.test.json         # Billing agent: invoice, payment, auth (12 cases)
│   ├── refund_workflow_direct.test.json  # Refund eligibility: check_if_refundable (5 cases)
│   │
│   │  # Pure Python tests (no LLM)
│   ├── test_tools.py                    # Direct tool function tests against mock Firestore
│   ├── test_refund_standalone.py        # Standalone refund workflow (direct function calls)
│   ├── test_mock_rag.py                 # MockRAGProductSearch tests (keyword, price, plural)
│   │
│   └── cases/                           # Additional eval datasets
│       ├── test_config.json
│       ├── out_of_scope.test.json       # Error handling: out-of-scope + ambiguous (6 cases)
│       ├── authorization_cross_user.test.json  # Auth: user1 → user2 (3 cases)
│       └── demo_user_002.test.json      # Auth: user2 → user1 (2 cases)
│
├── integration/                         # Integration Tests (Layer 3)
│   ├── conftest.py                      # Vertex AI init + mock_backends
│   ├── test_config.json                 # Integration criteria for adk eval CLI (backward compat)
│   │
│   │  # LLM-based orchestrator handoff tests (ADK AgentEvaluator, num_runs=2)
│   ├── test_integration_eval_ci.py      # Integration test runner (9 tests)
│   ├── product_agent_handoffs.evalset.json
│   ├── order_tracking_handoffs.evalset.json
│   ├── billing_handoffs.evalset.json
│   ├── refund_agent_handoffs.evalset.json
│   ├── error_handling.evalset.json
│   ├── multi_agent_handoffs.evalset.json
│   └── e2e_customer_journey.evalset.json
│
└── post_deploy/                         # Post-Deploy Eval (Layer 4)
    ├── datasets/
    │   └── post_deploy_cases.json       # 10 eval cases (product, order, billing, refund)
    └── dataset_converter.py             # ADK evalset → Vertex AI DataFrame converter
```

---

## Test Layers

### Layer 1: Pure Python (no LLM, no cost)

| File | Tests | What it covers |
|------|-------|----------------|
| `unit/test_mock_rag.py` | 5 | MockRAGProductSearch: keyword match, price filter, plural/singular, no results |
| `unit/test_tools.py` | 17 | All tool functions directly: product, order, billing, workflow tools |
| `unit/test_refund_standalone.py` | 5 | Refund workflow sequence: passing (ORD-12345, ORD-22222, ORD-67890), failing (ORD-11111, ORD-99999) |

### Layer 2: Agent Eval — Unit (LLM calls, num_runs=2)

| Test Class | Method | Dataset | Agent module | Cases |
|------------|--------|---------|--------------|-------|
| `TestProductAgentCI` | `test_product_agent_direct` | `product_agent_direct.test.json` | `test_product_agent` | 15 |
| `TestOrderAgentCI` | `test_order_agent_direct` | `order_agent_direct.test.json` | `test_order_agent` | 10 |
| `TestBillingAgentCI` | `test_billing_agent_direct` | `billing_direct.test.json` | `test_billing_agent` | 12 |
| `TestRefundEligibilityCI` | `test_refund_eligibility` | `refund_workflow_direct.test.json` | `test_refund_eligibility` | 5 |
| `TestErrorHandlingCI` | `test_out_of_scope` | `cases/out_of_scope.test.json` | `test_root_agent` | 6 |
| `TestAuthorizationUser1CI` | `test_cross_user_access_user1` | `cases/authorization_cross_user.test.json` | `test_root_agent` | 3 |
| `TestAuthorizationUser2CI` | `test_cross_user_access_user2` | `cases/demo_user_002.test.json` | `test_root_agent` | 2 |

> **Note:** Full refund flow (passing/denied) and SequentialAgent tests are NOT tested via AgentEvaluator because the SequentialAgent's gate-stopping behavior is non-deterministic. Instead, they are tested via direct tool calls in `test_tools.py` and `test_refund_standalone.py`.

### Layer 3: Agent Eval — Integration (LLM calls, num_runs=2)

| Test Class | Method | Dataset |
|------------|--------|---------|
| `TestMultiAgentHandoffs` | `test_product_agent_handoffs` | `product_agent_handoffs.evalset.json` |
| `TestMultiAgentHandoffs` | `test_order_tracking_handoffs` | `order_tracking_handoffs.evalset.json` |
| `TestMultiAgentHandoffs` | `test_billing_agent_handoffs` | `billing_handoffs.evalset.json` |
| `TestMultiAgentHandoffs` | `test_refund_agent_handoffs` | `refund_agent_handoffs.evalset.json` |
| `TestMultiAgentHandoffs` | `test_error_handling` | `error_handling.evalset.json` |
| `TestMultiAgentHandoffs` | `test_multi_agent_handoffs` | `multi_agent_handoffs.evalset.json` |
| `TestEndToEnd` | `test_e2e_customer_journey` | `e2e_customer_journey.evalset.json` |
| `TestIntegrationSuite` | `test_all_integration` | `tests/integration/` (all evalsets) |

### Layer 4: Post-Deploy Eval (Vertex AI Eval Service, deployed Agent Engine)

Evaluates the **deployed** agent on Agent Engine using the Vertex AI Gen AI Evaluation Service. Unlike Layers 1-3 which test locally with mocked backends, this layer tests the full deployed stack with real Firestore, real network calls, and real sub-agent execution.

| Script | Dataset | Metrics | Environment |
|--------|---------|---------|-------------|
| `tests/eval_vertex.py` | `tests/post_deploy/datasets/post_deploy_cases.json` (10 cases) | TOOL_USE_QUALITY, FINAL_RESPONSE_QUALITY | Deployed Agent Engine |

**Key difference from Layers 1-3:** Uses a custom inference adapter (`async_stream_query()`) instead of the SDK's `run_inference()`. The SDK's parser fails for multi-agent systems using `AgentTool` because it only captures the first 2 events (function_call + function_response) and misses the root agent's final text response. See `docs/EVALUATION.md` for full details.

```bash
# Run post-deploy eval
python tests/eval_vertex.py \
    --agent-engine-id projects/PROJECT_NUMBER/locations/LOCATION/reasoningEngines/ENGINE_ID \
    --output eval_results.json

# Debug: save raw inference results
python tests/eval_vertex.py \
    --agent-engine-id projects/PROJECT_NUMBER/locations/LOCATION/reasoningEngines/ENGINE_ID \
    --save-inference inference_debug.json
```

---

## Test Infrastructure

### Mocking (tests/conftest.py + tests/unit/conftest.py)

All tests run against **in-memory mocks** — no live Firestore or RAG calls:

| Fixture | Scope | Location | What it does |
|---------|-------|----------|--------------|
| `ci_environment_setup` | session | `unit/conftest.py` | Loads `.env`, inits Vertex AI |
| `mock_backends` | function, autouse | `unit/conftest.py` | Patches Firestore + RAG for AgentEvaluator re-runs |
| `mock_db` | session | `conftest.py` | Patches Firestore with `MockFirestoreClient` (seed data) |
| `mock_rag` | session | `conftest.py` | Patches RAG search with `MockRAGProductSearch` (keyword-based) |
| `verify_mock_active` | function, autouse | `conftest.py` | Asserts `MockFirestoreClient` is active — fails fast if mock leaks |

> **Critical:** The `mock_backends` autouse fixture in `unit/conftest.py` ensures that when `AgentEvaluator.evaluate()` re-runs the agent, it uses the same mocked data that was used to generate the eval datasets.

### Agent Wrapper Modules

AgentEvaluator requires a module with an `agent` attribute. These thin wrappers expose individual agents:

| Wrapper | Agent | Purpose |
|---------|-------|---------|
| `eval_wrappers/test_product_agent.py` | `product_agent` | Direct product agent tests |
| `eval_wrappers/test_order_agent.py` | `order_agent` | Direct order agent tests |
| `eval_wrappers/test_billing_agent.py` | `billing_agent` | Direct billing agent tests |
| `eval_wrappers/test_refund_eligibility.py` | Minimal agent with `check_if_refundable` | Refund eligibility tests |
| `eval_wrappers/test_root_agent.py` | `root_agent` (memory callback disabled) | Error handling, authorization tests |

### Logging

Both `tests/unit/conftest.py` and `tests/integration/conftest.py` use `logging.info()` (not `print()`).

### Flaky Detection

All `AgentEvaluator.evaluate()` calls use `num_runs=2` to catch non-deterministic LLM failures.

---

## Running Tests

```bash
# Pure Python tests only (fast, free)
pytest tests/unit/test_mock_rag.py tests/unit/test_tools.py tests/unit/test_refund_standalone.py -v -s

# Unit agent eval tests (default: standard profile)
pytest tests/unit/test_agent_eval_ci.py -v -s

# Unit agent eval tests with specific profile
EVAL_PROFILE=fast pytest tests/unit/test_agent_eval_ci.py -v -s
EVAL_PROFILE=full pytest tests/unit/test_agent_eval_ci.py -v -s

# Single agent test
pytest tests/unit/test_agent_eval_ci.py::TestProductAgentCI -v -s
pytest tests/unit/test_agent_eval_ci.py::TestOrderAgentCI -v -s
pytest tests/unit/test_agent_eval_ci.py::TestBillingAgentCI -v -s
pytest tests/unit/test_agent_eval_ci.py::TestRefundEligibilityCI -v -s
pytest tests/unit/test_agent_eval_ci.py::TestErrorHandlingCI -v -s
pytest tests/unit/test_agent_eval_ci.py::TestAuthorizationUser1CI -v -s
pytest tests/unit/test_agent_eval_ci.py::TestAuthorizationUser2CI -v -s

# Integration tests (default: standard profile)
pytest tests/integration/test_integration_eval_ci.py -v -s

# Integration with specific profile
EVAL_PROFILE=fast pytest tests/integration/test_integration_eval_ci.py -v -s
EVAL_PROFILE=full pytest tests/integration/test_integration_eval_ci.py -v -s

# Single integration test
pytest tests/integration/test_integration_eval_ci.py::TestMultiAgentHandoffs::test_product_agent_handoffs -v -s

# Everything (layers 1-3, local)
pytest tests/ -v -s

# Post-deploy eval (layer 4, requires deployed Agent Engine)
python tests/eval_vertex.py \
    --agent-engine-id projects/PROJECT_NUMBER/locations/LOCATION/reasoningEngines/ENGINE_ID

# Post-deploy with full profile (+ hallucination + safety)
python tests/eval_vertex.py \
    --agent-engine-id projects/PROJECT_NUMBER/locations/LOCATION/reasoningEngines/ENGINE_ID \
    --profile full
```

---

## Generating Eval Datasets

There are **two ways** to create eval datasets:

### Option A: Script (Recommended)

**Unit tests** — `tests/generate_eval_dataset.py` (single-turn, individual agents):

```bash
python tests/generate_eval_dataset.py --dry-run                    # preview
python tests/generate_eval_dataset.py --agent product --delay 10   # one agent
python tests/generate_eval_dataset.py --delay 10                   # all agents
# Available: product, order, billing, refund, error, auth_user1, auth_user2
```

**Integration tests** — `tests/generate_integration_evalset.py` (multi-turn, root orchestrator):

```bash
python tests/generate_integration_evalset.py --dry-run                          # preview
python tests/generate_integration_evalset.py --suite product_handoffs --delay 10 # one suite
python tests/generate_integration_evalset.py --delay 10                          # all suites
# Available: product_handoffs, order_handoffs, billing_handoffs, refund_handoffs,
#            multi_agent, e2e, error_handling, session_persistence
```

**How it works:**
1. Uses `InMemoryRunner` to run agents locally via Vertex AI Gemini API
2. Mocks Firestore + RAG (same mocks as test suite)
3. Captures `Event` objects → converts to ADK `EvalSet` pydantic models
4. Integration script maintains session state across multi-turn conversations
5. Retries on 429 rate limits with exponential backoff
6. Outputs JSON compatible with `AgentEvaluator.evaluate()`

### Option B: ADK Web UI

Manual approach via the ADK web evaluation dashboard:

1. `adk web customer_support_mas.agents.root` (integration) or `adk web eval_wrappers.test_product_agent` (unit)
2. Chat with the agent, verify traces
3. Eval tab → Create Evaluation Set → Add Current Session → Export
4. Save to `tests/unit/*.test.json` or `tests/integration/*.evalset.json`

---

## Eval Profiles

Agent eval tests use a **profile-based evaluation config system** controlled by the `EVAL_PROFILE` environment variable. Defaults to `standard` if not set.

```bash
EVAL_PROFILE=fast pytest tests/unit/test_agent_eval_ci.py -v -s
EVAL_PROFILE=full pytest tests/integration/test_integration_eval_ci.py -v -s
```

### Unit Test Profiles (`tests/eval_configs/unit/`)

| Profile | Metrics | Cost |
|---------|---------|------|
| **fast** | `response_match_score: 0.15` (Rouge-1) | Free |
| **standard** | + `tool_name_f1: 0.5` (F1 on tool names only) | Free |
| **standard_exact** | + `tool_trajectory_avg_score: 0.5` (strict exact match, backup) | Free |
| **full** | + `final_response_match_v2: 0.5` (LLM judge) | Gemini Flash calls |

### Integration Test Profiles (`tests/eval_configs/integration/`)

| Profile | Metrics | Cost |
|---------|---------|------|
| **fast** | `response_match_score: 0.3` (Rouge-1) | Free |
| **standard** | + `rubric_based_tool_use_quality_v1: 0.5` (LLM judge with 3 rubrics) | Gemini Flash calls |
| **full** | + `final_response_match_v2: 0.5` (LLM judge) | More Gemini Flash calls |

### Why Different Metrics for Unit vs Integration?

- **Unit tests** call tools with structured args (e.g., `product_id: "PROD-001"`) — exact `tool_trajectory_avg_score` matching works reliably
- **Integration tests** route through the root agent which passes free-form `request` args to sub-agents — the LLM paraphrases differently each run (e.g., expected `"laptops"` vs actual `"Show me laptops"`), so `rubric_based_tool_use_quality_v1` with semantic rubrics is required

### Integration Rubrics (standard + full)

| Rubric ID | What it checks |
|-----------|---------------|
| `correct_agent_delegation` | Query routed to correct sub-agent (product/order/billing) |
| `relevant_request` | Request passed to sub-agent is semantically relevant |
| `tool_called` | At least one tool call or delegation occurs |

### CI/CD Profile Mapping

```
pull_request      →  EVAL_PROFILE=fast      (free, ~1min)
push to main      →  EVAL_PROFILE=standard   (LLM judge, ~5min)
schedule/dispatch →  EVAL_PROFILE=full       (all metrics, ~10min)
```

### Legacy Config Files

The `test_config.json` files in `tests/`, `tests/unit/`, and `tests/integration/` are kept for backward compatibility with the `adk eval` CLI command. The profile system is used by the pytest-based test runners.

---

## Refund Workflow Testing

The refund workflow uses a **SequentialAgent** — tools must execute in order:

```
validate_order_id → check_refund_eligibility → process_refund
```

| Scenario | Order | Tested via | Result |
|----------|-------|-----------|--------|
| Valid refund (in transit) | ORD-12345 | AgentEvaluator + standalone | Approved |
| Valid refund (delivered, in window) | ORD-67890 | AgentEvaluator + standalone | Approved |
| Valid refund (processing) | ORD-22222 | standalone | Approved |
| Denied (past 30-day window) | ORD-11111 | standalone + test_tools | Denied |
| Invalid order | ORD-99999 | standalone + test_tools | Error |
| Eligibility check only | Various | AgentEvaluator (`test_refund_eligibility`) | Checked |

> Failing refund scenarios can't be reliably tested via AgentEvaluator because the LLM paraphrases `reason` args non-deterministically. They are tested deterministically via direct tool calls in `test_tools.py` and `test_refund_standalone.py`.

---

## Test Data (Seed)

### Orders

| Order ID | Status | Refund Eligible | Reason |
|----------|--------|-----------------|--------|
| ORD-12345 | In Transit | Yes | Can cancel |
| ORD-22222 | Processing | Yes | Can cancel before ship |
| ORD-67890 | Delivered | Yes | Within 30-day window |
| ORD-11111 | Delivered | No | Past 30-day window |
| ORD-99999 | N/A | N/A | Does not exist |

### Products

| Product ID | Name | Price |
|------------|------|-------|
| PROD-001 | ProBook Laptop 15 | $999.99 |
| PROD-002 | Wireless Headphones Pro | $199.99 |
| PROD-003 | Mechanical Gaming Keyboard | $149.99 |
| PROD-006 | ROG Gaming Laptop | $1,499.99 |

---

## Prerequisites

### Layers 1-3 (Local Tests)

```bash
# .env file
GOOGLE_CLOUD_PROJECT=your-project-id
GOOGLE_CLOUD_LOCATION=us-central1
GOOGLE_GENAI_USE_VERTEXAI=1

# Install
pip install -r requirements.txt
pip install -e .
```

### Layer 4 (Post-Deploy Eval)

Requires everything above, plus:

```bash
# Additional .env vars
GOOGLE_CLOUD_STORAGE_BUCKET=gs://your-bucket-name   # GCS bucket for eval results
AGENT_ENGINE_RESOURCE_NAME=projects/PROJECT_NUMBER/locations/LOCATION/reasoningEngines/ENGINE_ID

# Firestore permissions for Agent Engine service agent
gcloud projects add-iam-policy-binding PROJECT_ID \
    --member="serviceAccount:service-PROJECT_NUMBER@gcp-sa-aiplatform.iam.gserviceaccount.com" \
    --role="roles/datastore.user"
```

---

Last Updated: 2026-03-13
