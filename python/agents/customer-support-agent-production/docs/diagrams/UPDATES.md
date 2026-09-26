# Architecture Diagrams - Updates Log

## Summary
All Mermaid architecture diagrams have been updated to accurately reflect the current codebase implementation.

## Changes Made (Dec 3, 2025)

### ✅ Simplified Diagrams - Removed Technical Details

**Issue:** Timeout and max iteration details cluttered the diagrams
- ❌ Old: `Timeout: 60s • Max Iterations: 10` in agent nodes
- ✅ New: Removed - focus on architecture, not config

**Reason:** Diagrams should show architecture and relationships, not configuration details. Timeout/iteration settings are better documented in code comments or config files.

**What Was Removed:**
- All `Timeout: XXs` references
- All `Max Iterations: X` or `Max Iter: X` references

**What Remains:**
- ✅ Agent models (Gemini 2.5 Pro vs Flash)
- ✅ Tool counts and key tool names
- ✅ Callback information
- ✅ Agent roles and responsibilities
- ✅ Workflow patterns and data flows

**Files Updated:**
1. ✅ `agent-hierarchy.mmd` - Cleaner agent nodes
2. ✅ `product-agent.mmd` - Cleaner agent description
3. ✅ `order-agent.mmd` - Cleaner agent description
4. ✅ `billing-agent.mmd` - Cleaner agent description
5. ✅ `root-agent.mmd` - Cleaner agent description
6. ✅ `system-overview.mmd` - Cleaner all agent nodes

### ✅ Callback System Updates

**Issue:** Diagrams showed incorrect callback references
- ❌ Old: `Callbacks: track_agent_start, auto_save_to_memory`
- ✅ New: `Callback: auto_save_to_memory_explicit`

**Reason:** In the actual code:
- `track_agent_start` is commented out (not active)
- `auto_save_to_memory_explicit` is the active callback
- This callback creates `VertexAiMemoryBankService` for explicit memory management

**Files Updated:**
1. ✅ `agent-hierarchy.mmd` - Updated all agent callback references
2. ✅ `product-agent.mmd` - Updated callback reference
3. ✅ `order-agent.mmd` - Updated callback reference
4. ✅ `billing-agent.mmd` - Updated callback reference
5. ✅ `root-agent.mmd` - Updated callback reference
6. ✅ `system-overview.mmd` - Updated all callback references + renamed "Performance Monitoring" to "Memory Management"

### ✅ README Updates

**Enhanced Documentation:**
- Added tool count details (PreloadMemoryTool + domain-specific tools)
- Added technical notes about callbacks and agent hierarchy
- Clarified agent model selection (Pro for complex, Flash for simple)
- Added specific tool highlights:
  - Product Agent: `get_product_info` as default, `get_all_saved_products_info` for multi-product
  - Order Agent: `get_my_order_history` with authenticated context
  - Billing Agent: Refunds via `refund_workflow` only

## Verification

All diagrams now accurately match the code in:
- `/customer_support_mas/agents/root/agent.py` - Line 97: `after_agent_callback=auto_save_to_memory_explicit`
- `/customer_support_mas/agents/product/agent.py` - Line 151: `after_agent_callback=auto_save_to_memory_explicit`
- `/customer_support_mas/agents/order/agent.py` - Line 64: `after_agent_callback=auto_save_to_memory_explicit`
- `/customer_support_mas/agents/billing/agent.py` - Line 61: `after_agent_callback=auto_save_to_memory_explicit`

## Current Agent Configuration

| Agent | Model | Timeout | Max Iter | Tools | Callback |
|-------|-------|---------|----------|-------|----------|
| Root | Gemini 2.5 Pro | 60s | 10 | 4 agents | auto_save_to_memory_explicit |
| Product | Gemini 2.5 Flash | 30s | 5 | 8 tools | auto_save_to_memory_explicit |
| Order | Gemini 2.5 Flash | 20s | 3 | 3 tools | auto_save_to_memory_explicit |
| Billing | Gemini 2.5 Flash | 20s | 3 | 4 tools | auto_save_to_memory_explicit |
| Sequential Workflow | Gemini 2.5 Pro | 40s | 5 | 3 steps | N/A |

## Disabled Features (Not in Diagrams)

The following agents are **disabled** in the code and are **NOT** shown in the diagrams:
- ❌ ParallelAgent (commented out in `workflow_agents.py`)
- ❌ LoopAgent (commented out in `workflow_agents.py`)

These behaviors are now handled by the Product Agent using:
- `get_product_info()` for comprehensive lookups
- `get_all_saved_products_info()` for multi-product details

## Diagram Accuracy Checklist

- ✅ All callback references match actual code
- ✅ All tool counts are accurate
- ✅ All model assignments are correct
- ✅ All timeout/iteration limits match config
- ✅ No references to disabled agents (ParallelAgent, LoopAgent)
- ✅ Memory Bank integration accurately represented
- ✅ Firestore integration accurately represented
- ✅ Sequential workflow (refund) accurately represented

## Viewing the Diagrams

1. **GitHub**: View directly in the repository (native Mermaid support)
2. **Mermaid Live Editor**: Copy/paste `.mmd` files to https://mermaid.live
3. **VS Code**: Install Mermaid extension for preview
4. **Documentation**: Diagrams can be embedded in Markdown files using:
   ```markdown
   ```mermaid
   [paste diagram content here]
   ```
   ```

## Next Steps

If agent architecture changes in the future:
1. Update the relevant `.mmd` diagram file(s)
2. Update `README.md` if tool counts or capabilities change
3. Add entry to this `UPDATES.md` log
4. Verify diagrams render correctly in Mermaid Live Editor
5. Commit changes with descriptive message

---

**Last Updated:** December 3, 2025
**Updated By:** Comprehensive architecture diagram audit
**Verification Status:** ✅ All diagrams match current codebase
