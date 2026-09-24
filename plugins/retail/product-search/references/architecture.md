# What the retrieval layer actually does

Read this when demoing the skill or extending it — covers what's
semantic vs what isn't, and where structured filtering would have to
live.

## Semantic search, not filtered search

`search(query, top_k)` runs Vertex AI Vector Search semantic similarity
over the embedding fields configured in `design-spec.md` (default:
`name, description, category, brand`). **It does NOT apply structured
filters on price, currency, stock, or rating** -- those words in a
query are just text the embedder sees.

Implications when you demo:

- `"headphones under 100 EUR"` returns headphones ranked by semantic
  match; any price filtering happens in the agent's LLM, not the
  retriever, so results may include items above the threshold (the LLM
  may then narrate "no matches" if it filters client-side).
- The bundled `sample-products.csv` has USD prices. If the user asks in
  EUR, the agent silently treats them as equivalent. For a real
  multi-currency demo, add a `price_eur` or `currency` column to the
  catalog and prompt the agent to use it.

## Test queries

| Query | What you're testing |
|---|---|
| `"laptop for video editing"` | Pure semantic match (works as documented) |
| `"I need a gift"` | Vague-query clarification flow (agent prompt-driven) |
| `"Which one has the best battery life?"` | RAG-style follow-up |
| `"wireless headphones under $100"` | Semantic + LLM-side price filtering; agent will narrate the price filter even though the retriever doesn't apply it |

## Where to add structured filtering

If a real catalog needs `price < X` to actually constrain results
(not just narrate around them), there are three places to add it:

1. **Inside the retriever** — pre-filter the BigQuery dataset
   before embedding ingest, or post-filter the Vector Search results
   in `scripts/retrievers.py:search()`.
2. **As a separate tool on the agent** — add a `filter_by_price` tool
   alongside `retrieve_docs` and let the LLM compose them.
3. **In the agent's system prompt** — give the LLM filter rules and
   trust it to apply them. Cheapest, least reliable.

Option 1 is the right answer for production; options 2 and 3 are
useful for demos that need quick wins.
