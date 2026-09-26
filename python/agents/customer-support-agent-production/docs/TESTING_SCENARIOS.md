# Testing Scenarios for Live Demo

This document contains testing scenarios for demonstrating the Multi-Agent Customer Support System.

---

## Demo Credentials

| User | Email | Password | Tier | Notes |
|------|-------|----------|------|-------|
| Demo User | `demo@example.com` | `demo123` | Gold | 3 orders, 2 invoices |
| Jane Smith | `jane@example.com` | `jane123` | Silver | 1 order |

---

## Test Data Reference

**Products:**

| ID | Name | Price |
|----|------|-------|
| PROD-001 | ProBook Laptop 15 | $999.99 |
| PROD-002 | Wireless Headphones Pro | $199.99 |
| PROD-003 | Mechanical Gaming Keyboard | — |
| PROD-004 | Ergonomic Office Chair | $449.99 |
| PROD-005 | Standing Desk Pro | $599.99 |
| PROD-006 | ROG Gaming Laptop | — |

**Orders (demo@example.com):**

| ID | Status | Items | Refundable |
|----|--------|-------|-----------|
| ORD-12345 | In Transit | ProBook Laptop 15 + Wireless Headphones Pro | Yes |
| ORD-67890 | Delivered (5 days ago) | Wireless Headphones Pro | Yes (within 30-day window) |
| ORD-11111 | Delivered (45 days ago) | Ergonomic Office Chair | No (past 30-day window) |

**Invoices:**

| ID | Order | Status |
|----|-------|--------|
| INV-2025-001 | ORD-12345 | Pending |
| INV-2025-002 | ORD-67890 | Paid |
| INV-2024-003 | ORD-11111 | Paid |

---

## Feature 1: User Authentication

### Test 1.1: Email/Password Login
**Steps:**
1. Navigate to the login screen
2. Enter email: `demo@example.com`, password: `demo123`
3. Click "Sign In"
4. Verify chat interface loads

**Expected:** User authenticates and reaches the chat UI.

---

### Test 1.2: Guest Access
**Steps:**
1. Navigate to the login screen
2. Click "Continue as Guest"
3. Send message: "Hello, I need help"
4. Verify agent responds

**Expected:** Guest user can access the system without an account.

---

## Feature 2: RAG Semantic Search

### Test 2.1: Semantic Search with Natural Language
**Steps:**
1. Send: "I'm looking for a gaming computer under $1500"
2. Verify results include ROG Gaming Laptop (PROD-006)

**Expected:** Vector search matches "gaming computer" to ROG Gaming Laptop without exact keyword match.

---

### Test 2.2: Category Search
**Steps:**
1. Send: "Show me something for my home office"
2. Verify results include office-related products (e.g. Ergonomic Office Chair, Standing Desk Pro)

**Expected:** Agent understands intent and returns relevant products.

---

## Feature 3: Product Information Retrieval

### Test 3.1: Comprehensive Product Info
**Steps:**
1. Send: "Tell me about PROD-001"
2. Verify response includes: name, price, stock level, customer reviews/ratings

**Expected:** Agent returns details + inventory + reviews in a single response using `get_product_info`.

---

### Test 3.2: Multiple Products After Search
**Steps:**
1. Send: "Search for laptops"
2. Wait for results
3. Send: "Show me details on all of them"

**Expected:** Agent fetches all product details in a single call.

---

## Feature 4: Order Tracking

### Test 4.1: Track Specific Order
**Steps:**
1. Login as `demo@example.com`
2. Send: "Track my order ORD-12345"
3. Verify response includes: status (In Transit), carrier (FastShip), tracking number

**Expected:** Agent retrieves and displays order tracking information.

---

### Test 4.2: View Order History
**Steps:**
1. Send: "Show me my order history"
2. Verify response lists ORD-12345, ORD-67890, ORD-11111 with dates and statuses

**Expected:** Agent displays complete order history for the authenticated user.

---

## Feature 5: Billing and Invoices

### Test 5.1: Get Invoice by Order ID
**Steps:**
1. Send: "Get the invoice for order ORD-12345"
2. Verify response includes: invoice number (INV-2025-001), line items, total, payment status (Pending)

**Expected:** Agent retrieves and displays the full invoice.

---

### Test 5.2: Check Payment Status
**Steps:**
1. Send: "Check payment status for order ORD-67890"
2. Verify response shows: Paid, with payment date

**Expected:** Agent displays accurate payment status from INV-2025-002.

---

## Feature 6: Refund Processing (Sequential Workflow)

### Test 6.1: Successful Refund
**Steps:**
1. Send: "I want a refund for order ORD-67890"
2. Observe 3-step workflow:
   - Step 1: Order validation
   - Step 2: Eligibility check (delivered 5 days ago, within 30-day window)
   - Step 3: Refund processed
3. Verify success message

**Expected:** SequentialAgent processes all 3 steps and confirms the refund.

---

### Test 6.2: Refund Denied (Past Return Window)
**Steps:**
1. Send: "I want a refund for order ORD-11111"
2. Verify workflow stops at eligibility check
3. Verify response explains the order is past the 30-day return window

**Expected:** Sequential workflow validates eligibility and returns a clear denial reason.

---

## Feature 7: Multi-Session Conversations

### Test 7.1: Create New Chat Session
**Steps:**
1. In current session, send: "I need help with my order"
2. Click "New Chat"
3. Verify new empty chat appears
4. Send: "Show me laptops"
5. Verify this is an independent conversation

**Expected:** Multiple independent sessions can be created and maintained.

---

### Test 7.2: Switch Between Sessions
**Steps:**
1. In Session A, send: "Tell me about order ORD-12345"
2. Create Session B, send: "Show me gaming laptops"
3. Switch back to Session A
4. Verify previous messages are intact
5. Send: "What's the status?" — agent uses ORD-12345 context

**Expected:** Each session maintains its own independent conversation context.

---

## Feature 8: Memory Bank (Cross-Session Memory)

### Test 8.1: User Preference Memory
**Steps:**
1. **Session 1:** Login, send: "I prefer products under $500 and I'm interested in office furniture", logout
2. **Session 2:** Login again, send: "Show me some product recommendations"
3. Verify agent references the budget and furniture preference

**Expected:** Memory Bank recalls user preferences across sessions.

---

### Test 8.2: Past Issue Memory
**Steps:**
1. **Session 1:** Send: "I had a delivery issue with order ORD-12345", end session
2. **Session 2:** Send: "I want to place a new order"
3. Verify agent references the previous delivery issue

**Expected:** Agent remembers past issues to provide continuity.

---

## Feature 9: Multi-Agent Orchestration

### Test 9.1: Root Agent Routing to Product Agent
**Steps:**
1. Send: "I'm looking for wireless headphones"
2. Verify Product Agent (Gemini 2.5 Flash) handles the query
3. Verify `search_products` tool is called

**Expected:** Root Agent (Gemini 2.5 Pro) routes to Product Agent for product queries.

---

### Test 9.2: Complex Multi-Agent Request
**Steps:**
1. Send: "What's the status of my last order? Also send me the invoice"
2. Verify Root Agent coordinates:
   - Order Agent: retrieves order status
   - Billing Agent: retrieves invoice
3. Verify both are included in a single reply

**Expected:** Root Agent orchestrates multiple specialist agents for a compound request.

---

## Feature 10: Context-Aware Follow-ups

### Test 10.1: Product Context Across Turns
**Steps:**
1. Send: "Tell me about the ROG Gaming Laptop"
2. Send follow-up: "What's the warranty on that?"
3. Verify agent understands "that" refers to ROG Gaming Laptop

**Expected:** Agent maintains product context across conversation turns.

---

### Test 10.2: Sequential Follow-ups
**Steps:**
1. Send: "Show me gaming laptops"
2. Agent returns list including PROD-006
3. Send: "Is it in stock?"
4. Verify agent checks inventory for the gaming laptop

**Expected:** Agent tracks which product is being discussed without re-asking.

---

## Recommended Demo Flow (15 minutes)

| # | Feature | Scenario | Time |
|---|---------|----------|------|
| 1 | Authentication | Test 1.2 - Guest Access | 1 min |
| 2 | RAG Semantic Search | Test 2.1 - Gaming Computer | 2 min |
| 3 | Product Info | Test 3.1 - PROD-001 | 1.5 min |
| 4 | Order Tracking | Test 4.1 - ORD-12345 | 1.5 min |
| 5 | Refund Workflow | Test 6.1 - ORD-67890 | 2.5 min |
| 6 | Multi-Session | Test 7.1 - New Chat | 2 min |
| 7 | Memory Bank | Test 8.1 - Preferences | 2.5 min |
| 8 | Multi-Agent | Test 9.2 - Complex Request | 2 min |

**Total: ~15 minutes**

---

## Presenter Notes

- **RAG**: Emphasize that "gaming computer" finds ROG Gaming Laptop via vector similarity, not keyword match
- **Refund workflow**: Point out the 3 sequential validation steps and that each must pass before the next runs
- **Memory Bank**: Best demonstrated across two separate browser sessions (logout/login between them)
- **Multi-agent routing**: Mention which specialist agent (Product/Order/Billing) handles each request and why Root uses Pro while specialists use Flash (cost optimization)
