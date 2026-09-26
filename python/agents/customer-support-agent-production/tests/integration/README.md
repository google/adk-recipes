# Integration Tests - ADK Evaluation

## Overview

Integration tests for the Customer Support Multi-Agent System using Google ADK's AgentEvaluator framework. All tests use `num_runs=2` for flaky detection.

**Key Characteristic:** Multi-turn conversations through the root orchestrator. Tool calls are `AgentTool` calls (`product_agent`, `order_agent`, `billing_agent`, `refund_workflow`), not underlying tools.

---

## Test Files

### Datasets

```
tests/integration/
├── conftest.py                            # Vertex AI init + mock_backends autouse fixture
├── test_config.json                       # Criteria (tool_trajectory: 0.5, response_match: 0.3)
├── test_integration_eval_ci.py            # Test runner (9 test methods)
├── product_agent_handoffs.evalset.json    # Product browse flow (1 journey, 4 turns)
├── order_tracking_handoffs.evalset.json   # Order tracking multi-query (1 journey, 3 turns)
├── billing_handoffs.evalset.json          # Billing overview flow (1 journey, 3 turns)
├── refund_agent_handoffs.evalset.json     # Refund flows: eligible, denied, context switch (3 journeys)
├── error_handling.evalset.json            # Error recovery + mixed requests (3 journeys)
├── multi_agent_handoffs.evalset.json      # Cross-agent single prompts (4 cases)
├── e2e_customer_journey.evalset.json      # Full customer journeys (3 journeys)
└── session_persistence.evalset.json       # Context retention across turns (3 journeys)
```

### Tests (all in `test_integration_eval_ci.py`)

| Class | Method | Dataset | Journeys |
|-------|--------|---------|----------|
| `TestMultiAgentHandoffs` | `test_product_agent_handoffs` | `product_agent_handoffs.evalset.json` | 1 |
| `TestMultiAgentHandoffs` | `test_order_tracking_handoffs` | `order_tracking_handoffs.evalset.json` | 1 |
| `TestMultiAgentHandoffs` | `test_billing_agent_handoffs` | `billing_handoffs.evalset.json` | 1 |
| `TestMultiAgentHandoffs` | `test_refund_agent_handoffs` | `refund_agent_handoffs.evalset.json` | 3 |
| `TestMultiAgentHandoffs` | `test_error_handling` | `error_handling.evalset.json` | 3 |
| `TestMultiAgentHandoffs` | `test_multi_agent_handoffs` | `multi_agent_handoffs.evalset.json` | 4 |
| `TestEndToEnd` | `test_e2e_customer_journey` | `e2e_customer_journey.evalset.json` | 3 |
| `TestSessionPersistence` | `test_session_persistence` | `session_persistence.evalset.json` | 3 |
| `TestIntegrationSuite` | `test_all_integration` | `tests/integration/` (all evalsets) | all |

---

## Running Tests

```bash
# All integration tests
pytest tests/integration/test_integration_eval_ci.py -v -s

# Specific class
pytest tests/integration/test_integration_eval_ci.py::TestMultiAgentHandoffs -v -s
pytest tests/integration/test_integration_eval_ci.py::TestEndToEnd -v -s
pytest tests/integration/test_integration_eval_ci.py::TestSessionPersistence -v -s

# Specific test
pytest tests/integration/test_integration_eval_ci.py::TestMultiAgentHandoffs::test_product_agent_handoffs -v -s
pytest tests/integration/test_integration_eval_ci.py::TestMultiAgentHandoffs::test_order_tracking_handoffs -v -s
pytest tests/integration/test_integration_eval_ci.py::TestMultiAgentHandoffs::test_billing_agent_handoffs -v -s
pytest tests/integration/test_integration_eval_ci.py::TestMultiAgentHandoffs::test_refund_agent_handoffs -v -s
pytest tests/integration/test_integration_eval_ci.py::TestMultiAgentHandoffs::test_error_handling -v -s
pytest tests/integration/test_integration_eval_ci.py::TestMultiAgentHandoffs::test_multi_agent_handoffs -v -s
pytest tests/integration/test_integration_eval_ci.py::TestEndToEnd::test_e2e_customer_journey -v -s
pytest tests/integration/test_integration_eval_ci.py::TestSessionPersistence::test_session_persistence -v -s

# Full suite (runs all evalsets in directory)
pytest tests/integration/test_integration_eval_ci.py::TestIntegrationSuite -v -s
```

---

## Configuration

### `test_config.json`
```json
{
  "criteria": {
    "tool_trajectory_avg_score": 0.5,
    "response_match_score": 0.3
  }
}
```

---

## Generating Eval Datasets

### Option A: Script (Recommended)

```bash
# Preview all suites
python tests/generate_integration_evalset.py --dry-run

# Generate one suite
python tests/generate_integration_evalset.py --suite product_handoffs --delay 10
python tests/generate_integration_evalset.py --suite order_handoffs --delay 10
python tests/generate_integration_evalset.py --suite billing_handoffs --delay 10
python tests/generate_integration_evalset.py --suite refund_handoffs --delay 10
python tests/generate_integration_evalset.py --suite multi_agent --delay 10
python tests/generate_integration_evalset.py --suite e2e --delay 10
python tests/generate_integration_evalset.py --suite error_handling --delay 10
python tests/generate_integration_evalset.py --suite session_persistence --delay 10

# Generate all suites
python tests/generate_integration_evalset.py --delay 10
```

The script:
- Runs the **root agent** (via `eval_wrappers.test_root_agent`) with multi-turn conversations
- Maintains session state across turns within each eval case
- Mocks Firestore + RAG (same mocks as test suite)
- Captures real tool call traces and LLM responses
- Outputs `.evalset.json` in exact ADK format
- Retries on 429 rate limits with exponential backoff

### Option B: ADK Web UI

1. Start: `adk web customer_support_mas.agents.root`
2. Set `user_id` to `demo-user-001`
3. Chat with multi-turn prompts, verify tool traces
4. Eval tab -> Create Evaluation Set -> Add Current Session -> Export
5. Save as `tests/integration/<name>.evalset.json`

---

## Adding New Tests

### Using the script

1. Add test cases to `SUITE_REGISTRY` in `tests/generate_integration_evalset.py`
2. Run: `python tests/generate_integration_evalset.py --suite <key> --delay 10`
3. Add test method to `test_integration_eval_ci.py`:

```python
@pytest.mark.asyncio
async def test_my_workflow(self):
    await AgentEvaluator.evaluate(
        agent_module="eval_wrappers.test_root_agent",
        eval_dataset_file_path_or_dir="tests/integration/my_tests.evalset.json",
        num_runs=2,
        print_detailed_results=True
    )
```

### Using ADK Web UI

1. Start `adk web customer_support_mas.agents.root`
2. Chat, verify traces, export evalset
3. Save to `tests/integration/` and add test method as above

---

Last Updated: 2026-02-06
