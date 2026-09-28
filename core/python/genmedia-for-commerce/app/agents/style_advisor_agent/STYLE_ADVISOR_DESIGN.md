# Style Advisor Agent — Design Document

## Problem

Users need help composing complete, coherent outfits from the product catalog. Today the system can search for individual items, but there's no intelligence around:
- **Outfit composition** — selecting multiple pieces that work together
- **Occasion awareness** — a beach outfit shouldn't include boots
- **Style consistency** — all pieces should share a style register (casual, formal, sporty)
- **Color harmony** — avoiding clashing combinations
- **Trend awareness** — incorporating current fashion when requested

## Proposed Solution

A **Style Advisor** ADK agent that acts as a personal stylist. It receives a user request (occasion, preferences, constraints), decomposes it into outfit slots, searches the catalog for each slot with context-aware queries, validates the ensemble for consistency, and presents the result as a styled outfit card.

## Architecture

### Agent Topology

```
genmedia_router (root agent)
  ├── style_advisor (sub-agent)         <-- NEW
  │     ├── catalog_search (MCP tool)   <-- EXISTING, reused
  │     └── google_search (ADK tool)    <-- for trend queries
  └── [all existing MCP tools: product_fitting, VTO, spinning, etc.]
```

The style advisor is a **sub-agent** of the existing router. When the router detects styling intent (e.g. "suggest an outfit", "what should I wear to..."), it delegates to the style advisor. This keeps the router as the single entry point.

### Why a Sub-Agent (not a standalone agent or new MCP tool)

| Option | Pros | Cons |
|--------|------|------|
| **Sub-agent of router** | Natural delegation, shares MCP connection, user talks to one agent | Slightly coupled to router |
| Standalone agent | Fully independent | Separate entry point, duplicates MCP setup |
| New MCP tool | Simple integration | Can't do multi-step reasoning, can't call catalog_search iteratively |

**Decision:** Sub-agent. It needs multi-step LLM reasoning (decompose, search, validate, present), which is what agents are for. A single MCP tool can't iterate on catalog results or reason about coherence.

### Why No New Backend/MCP Tools

The existing `catalog_search` tool returns everything needed: description, category, color, style, audience, image URL, similarity score. The style advisor calls it multiple times with targeted queries — the intelligence is in **query formulation** and **result selection**, not in the search infrastructure.

If the catalog grows or we need category-filtered search, a `catalog_search_by_category` tool could help, but it's not needed for MVP.

## Agent Design

### Core Reasoning Flow

```
User Request
    │
    ▼
1. UNDERSTAND — Parse occasion, season, audience, preferences, constraints
    │
    ▼
2. DECOMPOSE — Determine outfit slots to fill:
    │           Top, Bottom, Footwear, Accessories, Outerwear (if needed)
    │
    ▼
3. SEARCH — For each slot, call catalog_search with a descriptive query
    │         e.g. "lightweight linen shirt casual beach summer"
    │         (NOT just "shirt")
    │
    ▼
4. SELECT — From results, pick the best item per slot considering:
    │         - Relevance to occasion
    │         - Color harmony across the outfit
    │         - Style consistency
    │
    ▼
5. VALIDATE — Check the full ensemble against consistency rules
    │           If a piece violates a rule → go back to SEARCH for that slot
    │
    ▼
6. PRESENT — Outfit card with images, descriptions, and reasoning
```

### Occasion Consistency Rules

These are embedded in the agent's instruction prompt. The LLM enforces them during validation.

| Occasion | Footwear | Fabrics | Avoid |
|----------|----------|---------|-------|
| Beach / Pool | Sandals, espadrilles | Linen, cotton, light | Boots, wool, formal shoes |
| Formal / Gala | Dress shoes, heels | Silk, wool, structured | Sneakers, flip-flops, denim |
| Office / Business | Closed-toe, loafers | Cotton, wool, smart casual | Flip-flops, athletic wear |
| Sport / Gym | Athletic shoes, trainers | Performance, moisture-wicking | Jeans, leather shoes |
| Winter / Cold | Boots, closed shoes | Wool, fleece, down | Sandals, thin fabrics |
| Casual | Flexible | Flexible | Mismatched style registers |
| Date night | Clean sneakers or dress shoes | Elevated casual | Gym wear, heavy outerwear |

### Color Harmony Strategy

The agent considers:
- **Monochromatic** — shades of one color (safe default)
- **Complementary** — opposite colors for contrast
- **Neutral base** — neutral top/bottom + one accent piece
- **No more than 3 dominant colors** in an outfit
- User preference overrides defaults

### Trend Awareness

- **Default behavior:** The LLM uses its built-in fashion knowledge (sufficient for general styling)
- **When user asks about trends** ("what's trending", "latest styles for summer"): the agent calls `google_search` to find current fashion trends, then incorporates them into catalog queries
- Trend data is used to **inform search queries**, not to override consistency rules

### Search Query Strategy

Bad query: `"shoes"`
Good query: `"casual brown leather sandals for beach vacation men"`

The agent constructs queries by combining:
1. **Garment type** for the slot (shirt, shorts, sandals)
2. **Occasion context** (beach, formal, casual)
3. **Style preference** (minimalist, bohemian, classic)
4. **Color scheme** (matching the evolving outfit palette)
5. **Audience** (men, women)
6. **Season** (summer, winter)

### Output Format

```
## Your [Occasion] Outfit

**Style:** [e.g., relaxed coastal casual]
**Season:** [e.g., summer]
**Color palette:** [e.g., white, beige, navy]

### The Look

| # | Slot | Item | Why it works |
|---|------|------|-------------|
| 1 | Top | White linen camp-collar shirt | Breathable, casual, beach-appropriate |
| 2 | Bottom | Beige chino shorts | Light color, pairs with linen |
| 3 | Footwear | Brown leather sandals | Beach-ready, matches earth tones |
| 4 | Accessory | Navy sunglasses | Adds color contrast, sun protection |

[Product images displayed inline]

**Styling tip:** [1-2 sentences on how pieces work together]
```

## Example Scenarios

### Scenario 1: "I need an outfit for a beach vacation"
- Slots: top, bottom, footwear, accessory
- Queries: "linen shirt beach casual summer", "shorts beach casual light", "sandals beach casual", "sunglasses beach"
- Validation: confirms no boots, no wool, no formal pieces

### Scenario 2: "Suggest a formal dinner outfit for a woman"
- Slots: dress OR (top + bottom), footwear, accessory
- Queries: "elegant evening dress formal dinner women", "heels formal elegant evening", "clutch bag formal evening"
- Validation: confirms no sneakers, no casual fabrics

### Scenario 3: "What's trendy for men this spring?"
- Agent calls `google_search("men's fashion trends spring 2026")`
- Extracts trend themes (e.g., "earth tones", "oversized blazers")
- Searches catalog with trend-informed queries
- Presents outfit with trend references

### Scenario 4: "I want to wear these jeans, complete the outfit" (with uploaded image)
- Agent sees the uploaded jeans image
- Picks complementary top, shoes, accessories
- Matches style register of the jeans (casual, distressed, etc.)

## Integration Points

### Router Agent Changes
- Add `style_advisor` to `sub_agents` list
- Add delegation instruction: "When the user asks about outfits, styling, what to wear, fashion advice, or wants you to suggest/compose a look — delegate to the style_advisor agent."

### Existing Infrastructure Reused
- `catalog_search` MCP tool (no changes)
- `McpToolset` with same SSE connection
- `after_tool_callback` from router (for artifact handling)
- Vector search backend (Vertex AI Matching Engine)

### No Backend Changes Required
- No new MCP tools
- No new REST endpoints
- No pipeline changes
- No database changes

## Future Enhancements (Out of Scope for MVP)

1. **Outfit history** — Remember past suggestions, avoid repeating
2. **Wardrobe building** — Track what user already owns, fill gaps
3. **Try-on integration** — After composing outfit, offer to VTO each piece
4. **Category-filtered search** — `catalog_search_by_category(query, category="footwear")` for more precise results
5. **Price-aware styling** — Budget constraints
6. **Body type awareness** — Suggest flattering silhouettes
7. **Multi-outfit response** — Propose 2-3 alternatives per request

## Open Questions

1. **Catalog coverage** — Does the current catalog have enough variety across all categories (tops, bottoms, shoes, accessories) to compose full outfits? If accessories are sparse, the agent should gracefully skip that slot.
2. **Audience handling** — Should the agent ask for gender, or infer from context? Current catalog has women/men/unisex.
3. **Number of catalog calls** — 4-5 calls per outfit request. Is this acceptable latency-wise? Could parallelize if needed.
4. **google_search availability** — Need to confirm `google_search` ADK tool is available in the project's GCP setup (requires Vertex AI Search or Grounding API).
