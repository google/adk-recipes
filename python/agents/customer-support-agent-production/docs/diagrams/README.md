# Architecture Diagrams

Mermaid diagrams for the Multi-Agent Customer Support System.

## Diagrams

### CI/CD Pipeline
**File:** `cicd-pipeline.mmd`
- Single `main` branch promotion flow: feat/* → PR → main → rc tag → prod tag
- All Cloud Build triggers: PR checks (auto-detects agent changes: fast eval or standard + integration tests), push to main (dev deploy), rc tag (staging), prod tag (prod release), nightly
- Release pipeline: shadow deploy → post-deploy eval gate → canary enable
- Nightly regression: Cloud Scheduler → full Vertex AI eval → compare vs GCS baseline

### System Overview
**File:** `system-overview.mmd`
- Complete system architecture
- Shows all agents, services, and data flows
- Includes Vertex AI Agent Engine, Memory Bank, and Firestore

### Agent Hierarchy
**File:** `agent-hierarchy.mmd`
- Multi-agent structure
- Root coordinator and specialist agents
- Workflow patterns

### Individual Agents

**Root Agent** - `root-agent.mmd`
- Coordinator and router
- Routes to specialist agents

**Product Agent** - `product-agent.mmd`
- 8 tools including RAG search (PreloadMemoryTool + 7 product tools)
- Smart default: `get_product_info` (comprehensive)
- Efficient multi-product: `get_all_saved_products_info`
- Firestore integration with vector search

**Order Agent** - `order-agent.mmd`
- 5 tools (PreloadMemoryTool + track_order + get_my_order_history + get_order_history + get_order_details)
- Authenticated user context — all tools verify ownership
- Firestore integration

**Billing Agent** - `billing-agent.mmd`
- 8 tools (PreloadMemoryTool + get_invoice + get_invoice_by_order_id + get_my_invoices + check_payment_status + get_my_payments + get_refundable_items + get_acceptable_refund_reasons)
- Note: Refunds processed via `refund_workflow` only
- Firestore integration

## Color Legend (Google Cloud Style)

- 🟢 Green (#34A853) - Root/Coordinator
- 🟡 Yellow (#FBBC04) - Specialist Agents
- 🔴 Red (#EA4335) - Workflows
- 🔵 Blue (#4285F4) - Tools
- 🟣 Purple (#9334E6) - Memory Bank
- 🟠 Orange (#FF6F00) - Firestore

## Technical Notes

**Callbacks:**
All agents use `auto_save_to_memory` callback which:
- Calls `memory_service.add_session_to_memory(session)` after each agent turn
- Skips evaluation sessions (session IDs starting with `___eval___session___`)
- Note: `track_agent_start` callback is defined but not active by default

**Agent Hierarchy:**
- Root Agent: Gemini 2.5 Pro (complex reasoning, coordination)
- Specialist Agents: Gemini 2.5 Flash (cost-optimized, simple tool calls)
- Sequential Workflow: Gemini 2.5 Pro (refund validation logic)

## Rendering

View these diagrams using:
- Mermaid Live Editor: https://mermaid.live
- GitHub (native Mermaid support)
- VS Code with Mermaid extension
