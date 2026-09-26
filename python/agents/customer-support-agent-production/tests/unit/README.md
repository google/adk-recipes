# Unit Tests - ADK Evaluation

## Overview

Unit tests for the Customer Support Multi-Agent System using Google ADK's AgentEvaluator framework plus pure Python tests.

---

## Test Files

### Pure Python (no LLM calls, no cost)

| File | Tests | Description |
|------|-------|-------------|
| `test_mock_rag.py` | 5 | MockRAGProductSearch: keyword match, price filter, singular/plural, no results |
| `test_tools.py` | 17 | Direct tool function calls: product, order, billing, workflow tools |
| `test_refund_standalone.py` | 5 | Refund workflow sequence via direct function calls |

### LLM-based Agent Eval (ADK AgentEvaluator, num_runs=2)

| File | Class | Method | Dataset | Cases |
|------|-------|--------|---------|-------|
| `test_agent_eval_ci.py` | `TestProductAgentCI` | `test_product_agent_direct` | `product_agent_direct.test.json` | 15 |
| `test_agent_eval_ci.py` | `TestOrderAgentCI` | `test_order_agent_direct` | `order_agent_direct.test.json` | 10 |
| `test_agent_eval_ci.py` | `TestBillingAgentCI` | `test_billing_agent_direct` | `billing_direct.test.json` | 12 |
| `test_agent_eval_ci.py` | `TestRefundEligibilityCI` | `test_refund_eligibility` | `refund_workflow_direct.test.json` | 5 |
| `test_agent_eval_ci.py` | `TestErrorHandlingCI` | `test_out_of_scope` | `cases/out_of_scope.test.json` | 6 |
| `test_agent_eval_ci.py` | `TestAuthorizationUser1CI` | `test_cross_user_access_user1` | `cases/authorization_cross_user.test.json` | 3 |
| `test_agent_eval_ci.py` | `TestAuthorizationUser2CI` | `test_cross_user_access_user2` | `cases/demo_user_002.test.json` | 2 |

> **Note:** Full refund flow (passing/denied) and SequentialAgent tests are NOT tested via AgentEvaluator because the SequentialAgent's gate-stopping behavior is non-deterministic. They are tested via direct tool calls in `test_tools.py` and `test_refund_standalone.py`.

---

## Dataset Files

```
tests/unit/
├── product_agent_direct.test.json       # search, details, inventory, reviews (15 cases)
├── order_agent_direct.test.json         # track, history, unauthorized (10 cases)
├── billing_direct.test.json             # invoice, payment, unauthorized (12 cases)
├── refund_workflow_direct.test.json     # eligibility checks only (5 cases)
└── cases/
    ├── test_config.json
    ├── out_of_scope.test.json           # out-of-scope + ambiguous (6 cases)
    ├── authorization_cross_user.test.json  # user1 accessing user2 (3 cases)
    └── demo_user_002.test.json          # user2 accessing user1 (2 cases)
```

---

## Running Tests

```bash
# All unit tests
pytest tests/unit/ -v -s

# Pure Python only (fast, free)
pytest tests/unit/test_mock_rag.py tests/unit/test_tools.py tests/unit/test_refund_standalone.py -v -s

# Agent eval only (LLM calls)
pytest tests/unit/test_agent_eval_ci.py -v -s

# Single agent test
pytest tests/unit/test_agent_eval_ci.py::TestProductAgentCI -v -s
pytest tests/unit/test_agent_eval_ci.py::TestOrderAgentCI -v -s
pytest tests/unit/test_agent_eval_ci.py::TestBillingAgentCI -v -s
pytest tests/unit/test_agent_eval_ci.py::TestRefundEligibilityCI -v -s
pytest tests/unit/test_agent_eval_ci.py::TestErrorHandlingCI -v -s
pytest tests/unit/test_agent_eval_ci.py::TestAuthorizationUser1CI -v -s
pytest tests/unit/test_agent_eval_ci.py::TestAuthorizationUser2CI -v -s
```

---

## Configuration

### `test_config.json`
```json
{
  "criteria": {
    "tool_trajectory_avg_score": 0.5,
    "response_match_score": 0.15
  }
}
```

---

## Generating Eval Datasets

### Option A: Script (Recommended)

```bash
# Preview all cases
python tests/generate_eval_dataset.py --dry-run

# Generate for one agent
python tests/generate_eval_dataset.py --agent product --delay 10
python tests/generate_eval_dataset.py --agent order --delay 10
python tests/generate_eval_dataset.py --agent billing --delay 10
python tests/generate_eval_dataset.py --agent refund --delay 10
python tests/generate_eval_dataset.py --agent error --delay 10
python tests/generate_eval_dataset.py --agent auth_user1 --delay 10
python tests/generate_eval_dataset.py --agent auth_user2 --delay 10

# Generate all at once
python tests/generate_eval_dataset.py --delay 10
```

The script:
- Runs the actual agent via `InMemoryRunner` with Vertex AI Gemini API
- Uses the same Firestore + RAG mocks as the test suite
- Captures real tool call traces and LLM responses
- Outputs `.test.json` in exact ADK `EvalSet` pydantic format
- Retries on 429 rate limits with exponential backoff

### Option B: ADK Web UI

1. Start: `adk web eval_wrappers.test_product_agent` (or other agent wrapper)
2. Chat with the agent, verify tool call traces
3. Eval tab → Create Evaluation Set → Add Current Session → Export
4. Save to `tests/unit/*.test.json`

---

## Adding New Tests

### Pure Python test
Add to `test_tools.py` or create a new `test_*.py` file.

### Agent eval test

**Using the script:**

1. Add test cases to `AGENT_REGISTRY` in `tests/generate_eval_dataset.py`
2. Create a wrapper module `eval_wrappers/test_<agent>.py` that exposes the agent as `agent`
3. Run: `python tests/generate_eval_dataset.py --agent <key> --delay 10`
4. Add test method to `test_agent_eval_ci.py`:

```python
class TestMyAgentCI:
    @pytest.mark.asyncio
    async def test_my_agent(self):
        await AgentEvaluator.evaluate(
            agent_module="eval_wrappers.test_my_agent",
            eval_dataset_file_path_or_dir="tests/unit/my_tests.test.json",
            num_runs=2,
            print_detailed_results=True
        )
```

**Using ADK Web UI:**

1. Start `adk web eval_wrappers.test_<agent>`
2. Chat, verify traces, export evalset
3. Save to `tests/unit/` and add test method as above

---

## Agent Wrapper Modules

AgentEvaluator requires a module with an `agent` attribute. These wrappers live in `eval_wrappers/`:

| Module | Exposes | Used for |
|--------|---------|----------|
| `test_product_agent.py` | `product_agent` | Product search, details, inventory, reviews |
| `test_order_agent.py` | `order_agent` | Order tracking, history |
| `test_billing_agent.py` | `billing_agent` | Invoice lookup, payment status |
| `test_refund_eligibility.py` | Minimal agent with `check_if_refundable` | Refund eligibility checks |
| `test_root_agent.py` | `root_agent` (memory callback disabled) | Error handling, authorization |

---

Last Updated: 2026-02-06
