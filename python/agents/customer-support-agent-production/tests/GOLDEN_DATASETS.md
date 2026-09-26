# Golden Datasets

This document lists ALL golden datasets, classified by **Unit Tests** (single agent, direct tool calls) and **Integration Tests** (multi-agent handoffs, end-to-end workflows).

---

## How to Generate Datasets

There are **two ways** to create eval datasets:

### Option A: Script (Recommended)

**Unit tests** — `tests/generate_eval_dataset.py` (single-turn, individual agents):

```bash
python tests/generate_eval_dataset.py --dry-run                    # preview
python tests/generate_eval_dataset.py --agent product --delay 10   # one agent
python tests/generate_eval_dataset.py --delay 10                   # all agents

# Available --agent values:
#   product    → tests/unit/product_agent_direct.test.json     (15 cases)
#   order      → tests/unit/order_agent_direct.test.json       (10 cases)
#   billing    → tests/unit/billing_direct.test.json           (12 cases)
#   refund     → tests/unit/refund_workflow_direct.test.json   (5 cases)
#   error      → tests/unit/cases/out_of_scope.test.json       (6 cases)
#   auth_user1 → tests/unit/cases/authorization_cross_user.test.json (3 cases)
#   auth_user2 → tests/unit/cases/demo_user_002.test.json      (2 cases)
```

**Integration tests** — `tests/generate_integration_evalset.py` (multi-turn, root orchestrator):

```bash
python tests/generate_integration_evalset.py --dry-run                          # preview
python tests/generate_integration_evalset.py --suite product_handoffs --delay 10 # one suite
python tests/generate_integration_evalset.py --delay 10                          # all suites

# Available --suite values:
#   product_handoffs    → tests/integration/product_agent_handoffs.evalset.json   (1 journey)
#   order_handoffs      → tests/integration/order_tracking_handoffs.evalset.json  (1 journey)
#   billing_handoffs    → tests/integration/billing_handoffs.evalset.json         (1 journey)
#   refund_handoffs     → tests/integration/refund_agent_handoffs.evalset.json    (3 journeys)
#   multi_agent         → tests/integration/multi_agent_handoffs.evalset.json     (4 cases)
#   e2e                 → tests/integration/e2e_customer_journey.evalset.json     (3 journeys)
#   error_handling      → tests/integration/error_handling.evalset.json           (3 journeys)
#   session_persistence → tests/integration/session_persistence.evalset.json      (3 journeys)
```

**How it works:**
- Uses `InMemoryRunner` to run agents locally via Vertex AI Gemini API
- Mocks Firestore + RAG backends (same mocks used in tests)
- Captures `Event` objects and converts to ADK `EvalSet` pydantic models
- Integration script maintains session state across multi-turn conversations
- Retries on 429 rate limits with exponential backoff
- Outputs JSON compatible with `AgentEvaluator.evaluate()`

**Wrapper modules** (used by both scripts and AgentEvaluator):

| Module | Exposes | Used by |
|--------|---------|---------|
| `eval_wrappers/test_product_agent.py` | `product_agent` as `agent` | unit: product tests |
| `eval_wrappers/test_order_agent.py` | `order_agent` as `agent` | unit: order tests |
| `eval_wrappers/test_billing_agent.py` | `billing_agent` as `agent` | unit: billing tests |
| `eval_wrappers/test_refund_eligibility.py` | minimal agent with `check_if_refundable` | unit: refund eligibility tests |
| `eval_wrappers/test_root_agent.py` | `root_agent` (no memory callback) | unit: error/auth, integration: all |

### Option B: ADK Web UI

Manual approach using the ADK web evaluation dashboard:

1. Start the ADK web server: `adk web customer_support_mas.agents.root`
2. Set the correct `user_id` in the session (see each test case)
3. Chat with the agent, verify tool call traces
4. Eval tab → Create Evaluation Set → Add Current Session → Export
5. Save to `tests/unit/*.test.json` or `tests/integration/*.evalset.json`

---

## Prerequisites

Before creating datasets:
1. **Script:** `GOOGLE_CLOUD_PROJECT` env var set, `.env` configured
2. **ADK Web:** Start `adk web customer_support_mas.agents.root`, set `user_id`
3. Ensure Firestore is seeded with demo data (script uses mocks automatically)

## Demo Users Reference

| User ID | Owns Orders | Owns Invoices |
|---------|-------------|---------------|
| `demo-user-001` | ORD-12345, ORD-67890, ORD-11111 | INV-2025-001, INV-2025-002, INV-2025-003 |
| `demo-user-002` | ORD-22222 | INV-2025-004 |

## Order Data Reference

| Order ID | Owner | Status | Delivery Date | Refund Eligible |
|----------|-------|--------|---------------|-----------------|
| ORD-12345 | demo-user-001 | In Transit | N/A | No (not delivered) |
| ORD-67890 | demo-user-001 | Delivered | ~5 days ago | Yes |
| ORD-11111 | demo-user-001 | Delivered | ~45 days ago | No (past 30-day window) |
| ORD-22222 | demo-user-002 | Processing | N/A | No (not delivered) |

---

# PART 1: UNIT TESTS

Unit tests verify **single agent behavior** with **direct tool calls**. Each test targets one agent and validates correct tool selection and arguments.

**Save location:** `tests/unit/` or `tests/unit/cases/`

---

## 1.1 Product Agent Unit Tests

**Agent:** `product_agent`
**User ID:** `demo-user-001` (products are public, any user works)
**File:** `tests/unit/product_agent_direct.test.json`

### Search Products

| Test ID | Prompt | Expected Tool Call | Expected Response |
|---------|--------|-------------------|-------------------|
| `search_laptops` | "Show me laptops" | `search_products(query="laptops")` | ProBook Laptop 15, ROG Gaming Laptop |
| `search_keyboards` | "What gaming keyboards do you have?" | `search_products(query="gaming keyboards")` | Mechanical Gaming Keyboard |
| `search_chairs` | "Show me office chairs" | `search_products(query="office chairs")` | Ergonomic Office Chair |
| `search_headphones` | "I need wireless headphones" | `search_products(query="wireless headphones")` | Wireless Headphones Pro |

### Search with Price Filter

| Test ID | Prompt | Expected Tool Call | Expected Response |
|---------|--------|-------------------|-------------------|
| `search_under_500` | "Show me products under $500" | `search_products(query="products under $500")` | Headphones, Keyboard, Chair |
| `search_under_200` | "What can I get for under $200?" | `search_products(query="under $200")` | Headphones, Keyboard |
| `search_under_1000` | "Laptops under $1000" | `search_products(query="laptops under $1000")` | ProBook Laptop 15 |
| `search_no_results` | "Show me products under $100" | `search_products(query="under $100")` | No products found |

### Product Details

| Test ID | Prompt | Expected Tool Call | Expected Response |
|---------|--------|-------------------|-------------------|
| `details_prod001` | "Tell me about product PROD-001" | `get_product_info(product_id="PROD-001")` | ProBook details with specs, price, reviews |
| `details_prod002` | "Details for PROD-002" | `get_product_info(product_id="PROD-002")` | Wireless Headphones Pro details |
| `details_invalid` | "Tell me about product PROD-999" | `get_product_info(product_id="PROD-999")` | Product not found |

### Inventory Check

| Test ID | Prompt | Expected Tool Call | Expected Response |
|---------|--------|-------------------|-------------------|
| `inventory_prod001` | "Is the ProBook laptop in stock?" | `check_inventory(product_id="PROD-001")` | Stock levels by warehouse |
| `inventory_prod003` | "How many gaming keyboards are available?" | `check_inventory(product_id="PROD-003")` | Inventory count |

### Product Reviews

| Test ID | Prompt | Expected Tool Call | Expected Response |
|---------|--------|-------------------|-------------------|
| `reviews_prod001` | "Show me reviews for the ProBook laptop" | `get_product_reviews(product_id="PROD-001")` | Recent reviews with ratings |
| `reviews_prod002` | "What do people say about the headphones?" | `get_product_reviews(product_id="PROD-002")` | Headphones reviews |

---

## 1.2 Order Agent Unit Tests

**Agent:** `order_agent`
**File:** `tests/unit/order_agent_direct.test.json`

### Track Order - Valid (User: demo-user-001)

| Test ID | Prompt | Expected Tool Call | Expected Response |
|---------|--------|-------------------|-------------------|
| `track_in_transit` | "Where is my order ORD-12345?" | `track_order(order_id="ORD-12345")` | In Transit with tracking details |
| `track_delivered` | "What's the status of order ORD-67890?" | `track_order(order_id="ORD-67890")` | Delivered status |
| `track_old_order` | "Track order ORD-11111" | `track_order(order_id="ORD-11111")` | Delivered (45 days ago) |

### Track Order - Valid (User: demo-user-002)

| Test ID | Prompt | Expected Tool Call | Expected Response |
|---------|--------|-------------------|-------------------|
| `track_processing` | "Track order ORD-22222" | `track_order(order_id="ORD-22222")` | Processing status |

### Track Order - Invalid/Unauthorized

**User ID:** `demo-user-001`

| Test ID | Prompt | Expected Tool Call | Expected Response |
|---------|--------|-------------------|-------------------|
| `track_not_found` | "Track order ORD-99999" | `track_order(order_id="ORD-99999")` | Order not found |
| `track_unauthorized` | "Track order ORD-22222" | `track_order(order_id="ORD-22222")` | No permission (belongs to demo-user-002) |

**User ID:** `demo-user-002`

| Test ID | Prompt | Expected Tool Call | Expected Response |
|---------|--------|-------------------|-------------------|
| `track_unauthorized_user2` | "Track order ORD-12345" | `track_order(order_id="ORD-12345")` | No permission (belongs to demo-user-001) |

### Order History

**User ID:** `demo-user-001`

| Test ID | Prompt | Expected Tool Call | Expected Response |
|---------|--------|-------------------|-------------------|
| `order_history` | "Show me my recent orders" | `get_my_order_history()` | ORD-12345, ORD-67890, ORD-11111 |
| `order_history_alt` | "What have I ordered?" | `get_my_order_history()` | Same as above |

**User ID:** `demo-user-002`

| Test ID | Prompt | Expected Tool Call | Expected Response |
|---------|--------|-------------------|-------------------|
| `order_history_user2` | "Show me my orders" | `get_my_order_history()` | Only ORD-22222 |

---

## 1.3 Billing Agent Unit Tests

**Agent:** `billing_agent`
**File:** `tests/unit/billing_direct.test.json`

### Invoice by ID (User: demo-user-001)

| Test ID | Prompt | Expected Tool Call | Expected Response |
|---------|--------|-------------------|-------------------|
| `invoice_by_id_001` | "Show me invoice INV-2025-001" | `get_invoice(invoice_id="INV-2025-001")` | Invoice details |
| `invoice_by_id_002` | "Get invoice INV-2025-002" | `get_invoice(invoice_id="INV-2025-002")` | Invoice for ORD-67890 |
| `invoice_not_found` | "Show me invoice INV-9999" | `get_invoice(invoice_id="INV-9999")` | Invoice not found |
| `invoice_unauthorized` | "Show me invoice INV-2025-004" | `get_invoice(invoice_id="INV-2025-004")` | No permission |

### Invoice by Order (User: demo-user-001)

| Test ID | Prompt | Expected Tool Call | Expected Response |
|---------|--------|-------------------|-------------------|
| `invoice_by_order_12345` | "What's the invoice for order ORD-12345?" | `get_invoice_by_order_id(order_id="ORD-12345")` | INV-2025-001 |
| `invoice_by_order_67890` | "Invoice for order ORD-67890" | `get_invoice_by_order_id(order_id="ORD-67890")` | INV-2025-002 |
| `invoice_by_order_unauth` | "Invoice for order ORD-22222" | `get_invoice_by_order_id(order_id="ORD-22222")` | No permission |

### Payment Status (User: demo-user-001)

| Test ID | Prompt | Expected Tool Call | Expected Response |
|---------|--------|-------------------|-------------------|
| `payment_status_12345` | "Has my payment for order ORD-12345 been processed?" | `check_payment_status(order_id="ORD-12345")` | Payment status |
| `payment_status_67890` | "Payment status for ORD-67890" | `check_payment_status(order_id="ORD-67890")` | Payment status |

### My Invoices / My Payments

**User ID:** `demo-user-001`

| Test ID | Prompt | Expected Tool Call | Expected Response |
|---------|--------|-------------------|-------------------|
| `my_invoices` | "Show me all my invoices" | `get_my_invoices()` | User's invoices list |
| `my_payments` | "Show me my payment history" | `get_my_payments()` | User's payments list |

**User ID:** `demo-user-002`

| Test ID | Prompt | Expected Tool Call | Expected Response |
|---------|--------|-------------------|-------------------|
| `my_invoices_user2` | "Show me all my invoices" | `get_my_invoices()` | Only INV-2025-004 |

---

## 1.4 Refund Workflow Unit Tests

**Agent:** `refund_workflow` (SequentialAgent)
**User ID:** `demo-user-001`
**File:** `tests/unit/refund_workflow_direct.test.json`

### Check Eligibility (New Conversational Flow)

| Test ID | Prompt | Expected Tool Call | Expected Response |
|---------|--------|-------------------|-------------------|
| `check_eligible` | "I want a refund for order ORD-67890" | `check_if_refundable(order_id="ORD-67890")` | Eligible - asks for reason |
| `check_past_window` | "I want a refund for order ORD-11111" | `check_if_refundable(order_id="ORD-11111")` | Not eligible - past 30-day window |
| `check_not_delivered` | "Refund order ORD-12345" | `check_if_refundable(order_id="ORD-12345")` | Not eligible - still in transit |
| `check_not_found` | "Refund order ORD-99999" | `check_if_refundable(order_id="ORD-99999")` | Order not found |
| `check_unauthorized` | "Refund order ORD-22222" | `check_if_refundable(order_id="ORD-22222")` | No permission |

### Full Refund Flow - Passing

**File:** `tests/unit/refund_workflow_direct.test.json`

| Test ID | Prompt Sequence | Expected Tool Calls | Expected Response |
|---------|-----------------|---------------------|-------------------|
| `refund_conversational` | 1. "Refund ORD-67890" | 1. `check_if_refundable` | 1. Asks for reason |
| | 2. "The headphones are defective" | 2. `validate_refund_request` → `check_refund_eligibility` → `process_refund` | 2. Refund approved |
| `refund_with_reason` | "Refund ORD-67890, item is defective" | `check_if_refundable` → `validate` → `check` → `process` | Refund approved |

### Full Refund Flow - Denied (Invalid Reason)

**File:** `tests/unit/refund_denied_direct.test.json`

| Test ID | Prompt Sequence | Expected Tool Calls | Expected Response |
|---------|-----------------|---------------------|-------------------|
| `refund_invalid_reason` | 1. "Refund ORD-67890" | 1. `check_if_refundable` | 1. Asks for reason |
| | 2. "I changed my mind" | 2. `process_refund` (rejects) | 2. Not a valid refund reason |

### Valid Refund Reasons Reference

| Reason | Valid? | Notes |
|--------|--------|-------|
| "defective" | ✅ Yes | Product defect |
| "damaged" | ✅ Yes | Shipping damage |
| "wrong item" | ✅ Yes | Incorrect product sent |
| "not as described" | ✅ Yes | Misleading listing |
| "changed my mind" | ❌ No | Not product-related |
| "don't want it" | ❌ No | Not product-related |

---

## 1.5 Error Handling Unit Tests

**Agent:** Root orchestrator
**User ID:** `demo-user-001`
**File:** `tests/unit/cases/out_of_scope.test.json`

### Out of Scope Queries (No Tool Calls Expected)

| Test ID | Prompt | Expected Tool Call | Expected Response |
|---------|--------|-------------------|-------------------|
| `out_of_scope_weather` | "What's the weather today?" | None | "I can only help with products, orders, and billing" |
| `out_of_scope_joke` | "Tell me a joke" | None | Politely declines |
| `out_of_scope_general` | "What's the capital of France?" | None | Politely declines |

### Ambiguous Requests

| Test ID | Prompt | Expected Tool Call | Expected Response |
|---------|--------|-------------------|-------------------|
| `ambiguous_details` | "Give me details" | None | Asks for clarification |
| `ambiguous_refund` | "I want a refund" | None | Asks for order ID |
| `ambiguous_track` | "Track it" | None | Asks for order ID |

---

## 1.6 Authorization Unit Tests

**File:** `tests/unit/cases/authorization_cross_user.test.json`

### demo-user-001 Accessing demo-user-002 Resources

| Test ID | Prompt | Expected Tool Call | Expected Response |
|---------|--------|-------------------|-------------------|
| `auth_track_other` | "Track order ORD-22222" | `track_order(order_id="ORD-22222")` | No permission |
| `auth_refund_other` | "Refund order ORD-22222" | `check_if_refundable(order_id="ORD-22222")` | No permission |
| `auth_invoice_other` | "Invoice for ORD-22222" | `get_invoice_by_order_id(order_id="ORD-22222")` | No permission |

### demo-user-002 Accessing demo-user-001 Resources

**File:** `tests/unit/cases/demo_user_002.test.json`

| Test ID | Prompt | Expected Tool Call | Expected Response |
|---------|--------|-------------------|-------------------|
| `auth_track_other_user2` | "Track order ORD-12345" | `track_order(order_id="ORD-12345")` | No permission |
| `auth_refund_other_user2` | "Refund order ORD-67890" | `check_if_refundable(order_id="ORD-67890")` | No permission |

---

# PART 2: INTEGRATION TESTS

Integration tests verify **multi-agent interactions**, **handoffs**, and **end-to-end workflows**. These test the orchestrator's ability to route requests and maintain context across agents.

**Save location:** `tests/integration/`

---

## 2.1 Product Agent Handoff Tests

**File:** `tests/integration/product_agent_handoffs.evalset.json`
**User ID:** `demo-user-001`

### Product Search → Details Flow

| Test ID | Prompt Sequence | Agents Involved | Expected Behavior |
|---------|-----------------|-----------------|-------------------|
| `product_browse_flow` | 1. "Show me laptops" | product_agent | Search results |
| | 2. "Tell me more about the ProBook" | product_agent | Identifies PROD-001, shows details |
| | 3. "Is it in stock?" | product_agent | Shows inventory |
| | 4. "What are the reviews?" | product_agent | Shows reviews |

---

## 2.2 Order Agent Handoff Tests

**File:** `tests/integration/order_tracking_handoffs.evalset.json`
**User ID:** `demo-user-001`

### Order Tracking Multi-Query

| Test ID | Prompt Sequence | Agents Involved | Expected Behavior |
|---------|-----------------|-----------------|-------------------|
| `track_multiple_orders` | 1. "Where is order ORD-12345?" | order_agent | In Transit |
| | 2. "What about ORD-67890?" | order_agent | Delivered |
| | 3. "Show me all my orders" | order_agent | Full order history |

---

## 2.3 Billing Agent Handoff Tests

**File:** `tests/integration/billing_handoffs.evalset.json`
**User ID:** `demo-user-001`

### Billing Overview Flow

| Test ID | Prompt Sequence | Agents Involved | Expected Behavior |
|---------|-----------------|-----------------|-------------------|
| `billing_overview` | 1. "Show me all my invoices" | billing_agent | Lists invoices |
| | 2. "What's the status of invoice INV-2025-001?" | billing_agent | Invoice details |
| | 3. "Has the payment gone through?" | billing_agent | Payment status |

---

## 2.4 Refund Agent Handoff Tests

**File:** `tests/integration/refund_agent_handoffs.evalset.json`
**User ID:** `demo-user-001`

### Refund Conversational Flow

| Test ID | Prompt Sequence | Agents Involved | Expected Behavior |
|---------|-----------------|-----------------|-------------------|
| `refund_eligible_flow` | 1. "I want a refund for order ORD-67890" | root → refund_workflow | Checks eligibility, asks reason |
| | 2. "The item arrived damaged" | refund_workflow | Processes refund |
| `refund_denied_flow` | 1. "Refund order ORD-11111" | root → refund_workflow | Past return window - STOPS |
| `refund_then_other` | 1. "Refund ORD-67890, defective" | refund_workflow | Refund processed |
| | 2. "Track order ORD-12345" | order_agent | Shows tracking (context switch) |

---

## 2.5 Multi-Agent Handoff Tests

**File:** `tests/integration/multi_agent_handoffs.evalset.json`
**User ID:** `demo-user-001`

### Product → Order Handoff

| Test ID | Prompt | Agents Involved | Expected Behavior |
|---------|--------|-----------------|-------------------|
| `product_to_order` | "I ordered the ProBook laptop, where is it? Order ORD-12345" | product_agent → order_agent | Product info then tracking |

### Order → Billing Handoff

| Test ID | Prompt | Agents Involved | Expected Behavior |
|---------|--------|-----------------|-------------------|
| `order_to_billing` | "Track order ORD-12345 and show me its invoice" | order_agent → billing_agent | Tracking then invoice |
| `order_payment` | "What's the status of ORD-12345 and has the payment gone through?" | order_agent → billing_agent | Order status + payment |

### Complex Multi-Agent

| Test ID | Prompt | Agents Involved | Expected Behavior |
|---------|--------|-----------------|-------------------|
| `three_agent_query` | "Tell me about the ProBook, track ORD-12345, and show the invoice" | product → order → billing | All three respond |

---

## 2.6 End-to-End Customer Journey Tests

**File:** `tests/integration/e2e_customer_journey.evalset.json`
**User ID:** `demo-user-001`

### Purchase Research Journey

| Test ID | Prompt Sequence | Description |
|---------|-----------------|-------------|
| `journey_purchase` | 1. "Show me laptops under $1500" | Browse products |
| | 2. "Tell me about the ProBook" | Get details |
| | 3. "Is it in stock?" | Check inventory |
| | 4. "What do customers say about it?" | Read reviews |

### Post-Purchase Support Journey

| Test ID | Prompt Sequence | Description |
|---------|-----------------|-------------|
| `journey_support` | 1. "Track order ORD-67890" | Check delivery status |
| | 2. "Show me the invoice" | Get billing info |
| | 3. "I want a refund, the item is defective" | Request refund |

### Multi-Order Management Journey

| Test ID | Prompt Sequence | Description |
|---------|-----------------|-------------|
| `journey_multi_order` | 1. "Where is order ORD-12345?" | Track first order |
| | 2. "What about ORD-67890?" | Track second order |
| | 3. "Show me all my invoices" | Billing overview |
| | 4. "What's my payment history?" | Payment summary |

---

## 2.7 Error Handling Integration Tests

**File:** `tests/integration/error_handling.evalset.json`
**User ID:** `demo-user-001`

### Graceful Error Recovery

| Test ID | Prompt Sequence | Expected Behavior |
|---------|-----------------|-------------------|
| `error_then_valid` | 1. "Track order ORD-99999" | Order not found |
| | 2. "Try ORD-12345 instead" | Successfully tracks |
| `unauthorized_then_own` | 1. "Track order ORD-22222" | No permission |
| | 2. "Show me my orders" | Shows user's own orders |

### Mixed Valid/Invalid Requests

| Test ID | Prompt Sequence | Expected Behavior |
|---------|-----------------|-------------------|
| `mixed_requests` | 1. "What's the weather?" | Out of scope - declines |
| | 2. "Show me laptops" | Successfully searches products |
| | 3. "Tell me a joke" | Out of scope - declines |
| | 4. "Track ORD-12345" | Successfully tracks order |

---

## 2.8 Session Context Persistence Tests

**File:** `tests/integration/session_persistence.evalset.json`
**User ID:** `demo-user-001`

### Product Context Retention

| Test ID | Prompt Sequence | Expected Behavior |
|---------|-----------------|-------------------|
| `context_product` | 1. "Tell me about PROD-001" | Shows ProBook details |
| | 2. "Is it in stock?" | Remembers PROD-001, checks inventory |
| | 3. "What are the reviews?" | Remembers PROD-001, shows reviews |

### Order Context Retention

| Test ID | Prompt Sequence | Expected Behavior |
|---------|-----------------|-------------------|
| `context_order` | 1. "Track ORD-12345" | Shows order status |
| | 2. "Show me the invoice for it" | Remembers ORD-12345, shows invoice |
| | 3. "What's the payment status?" | Remembers order, shows payment |

### Cross-Agent Context

| Test ID | Prompt Sequence | Expected Behavior |
|---------|-----------------|-------------------|
| `context_cross_agent` | 1. "Track ORD-67890" | Order agent: shows status |
| | 2. "I want a refund for it" | Refund workflow: uses ORD-67890 |

---

# Summary Tables

## Unit Tests Summary

| Category | File | Test Count |
|----------|------|------------|
| Product Agent | `tests/unit/product_agent_direct.test.json` | 15 |
| Order Agent | `tests/unit/order_agent_direct.test.json` | 10 |
| Billing Agent | `tests/unit/billing_direct.test.json` | 12 |
| Refund Workflow | `tests/unit/refund_workflow_direct.test.json` | 8 |
| Refund Denied | `tests/unit/refund_denied_direct.test.json` | 3 |
| Refund Failing | `tests/unit/refund_failing_direct.test.json` | 4 |
| Error Handling | `tests/unit/cases/out_of_scope.test.json` | 6 |
| Authorization | `tests/unit/cases/authorization_cross_user.test.json` | 3 |
| Demo User 002 | `tests/unit/cases/demo_user_002.test.json` | 5 |
| **Total Unit** | | **~66** |

## Integration Tests Summary

| Category | File | Test Count |
|----------|------|------------|
| Product Handoffs | `tests/integration/product_agent_handoffs.evalset.json` | 1 journey |
| Order Handoffs | `tests/integration/order_tracking_handoffs.evalset.json` | 1 journey |
| Billing Handoffs | `tests/integration/billing_handoffs.evalset.json` | 1 journey |
| Refund Handoffs | `tests/integration/refund_agent_handoffs.evalset.json` | 3 journeys |
| Multi-Agent | `tests/integration/multi_agent_handoffs.evalset.json` | 4 tests |
| E2E Journeys | `tests/integration/e2e_customer_journey.evalset.json` | 3 journeys |
| Error Handling | `tests/integration/error_handling.evalset.json` | 2 journeys |
| Session Persistence | `tests/integration/session_persistence.evalset.json` | 3 journeys |
| **Total Integration** | | **~18 journeys** |

---

## Quick Reference: Expected Tool Calls

| User Intent | Tool | Agent |
|-------------|------|-------|
| Search products | `search_products` | product_agent |
| Product details | `get_product_info` | product_agent |
| Check stock | `check_inventory` | product_agent |
| Product reviews | `get_product_reviews` | product_agent |
| Track order | `track_order` | order_agent |
| Order history | `get_my_order_history` | order_agent |
| Get invoice by ID | `get_invoice` | billing_agent |
| Get invoice by order | `get_invoice_by_order_id` | billing_agent |
| Payment status | `check_payment_status` | billing_agent |
| My invoices | `get_my_invoices` | billing_agent |
| My payments | `get_my_payments` | billing_agent |
| Check refund eligibility | `check_if_refundable` | root/refund |
| Process refund | `refund_workflow` | refund_workflow |

---

## Recording Tips

### Script (Option A)
1. **Run with `--dry-run` first** to verify test case definitions
2. **Use `--delay 10`** to avoid Vertex AI rate limits (429 errors)
3. **Generate one agent at a time** if rate limits are an issue
4. **Verify output** with `python -c "from google.adk.evaluation.eval_set import EvalSet; EvalSet.model_validate_json(open('file.test.json').read())"`

### ADK Web UI (Option B)
1. **Start fresh session** for each test case to avoid context pollution
2. **Set user_id** before starting the conversation
3. **Record the full conversation** including intermediate tool calls
4. **Verify tool calls** match expected behavior before saving
5. **Export as JSON** using ADK web export feature

## Validation Checklist

After creating each dataset:
- [ ] Correct `user_id` in `session_input`
- [ ] Expected tool calls in `intermediate_data.invocation_events`
- [ ] Reasonable `final_response` (doesn't need to be exact)
- [ ] No sensitive data exposed
- [ ] File saved in correct location (unit vs integration)
- [ ] File loads with `EvalSet.model_validate_json()` (no validation errors)

## Running Tests Against Generated Datasets

```bash
# Run all unit eval tests
pytest tests/unit/test_agent_eval_ci.py -v -s

# Run a specific agent's tests
pytest tests/unit/test_agent_eval_ci.py::TestProductAgentCI -v -s
pytest tests/unit/test_agent_eval_ci.py::TestOrderAgentCI -v -s
pytest tests/unit/test_agent_eval_ci.py::TestBillingAgentCI -v -s
pytest tests/unit/test_agent_eval_ci.py::TestRefundEligibilityCI -v -s
pytest tests/unit/test_agent_eval_ci.py::TestErrorHandlingCI -v -s
pytest tests/unit/test_agent_eval_ci.py::TestAuthorizationUser1CI -v -s
pytest tests/unit/test_agent_eval_ci.py::TestAuthorizationUser2CI -v -s

# Run all integration eval tests
pytest tests/integration/test_integration_eval_ci.py -v -s

# Run a specific integration test
pytest tests/integration/test_integration_eval_ci.py::TestMultiAgentHandoffs::test_product_agent_handoffs -v -s
pytest tests/integration/test_integration_eval_ci.py::TestMultiAgentHandoffs::test_order_tracking_handoffs -v -s
pytest tests/integration/test_integration_eval_ci.py::TestMultiAgentHandoffs::test_billing_agent_handoffs -v -s
pytest tests/integration/test_integration_eval_ci.py::TestMultiAgentHandoffs::test_refund_agent_handoffs -v -s
pytest tests/integration/test_integration_eval_ci.py::TestMultiAgentHandoffs::test_error_handling -v -s
pytest tests/integration/test_integration_eval_ci.py::TestMultiAgentHandoffs::test_multi_agent_handoffs -v -s
pytest tests/integration/test_integration_eval_ci.py::TestEndToEnd::test_e2e_customer_journey -v -s
pytest tests/integration/test_integration_eval_ci.py::TestSessionPersistence::test_session_persistence -v -s

# Run everything
pytest tests/ -v -s
```
