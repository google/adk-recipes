# Example Agent Code

The shipping implementation is in `scripts/agent.py` and `scripts/retrievers.py`.
Read those files directly -- this doc summarizes the shape so you know what
to look at when adapting it.

## Files

- **`scripts/agent.py`** -- defines `root_agent`, a single ADK `Agent` with one
  tool (`retrieve_docs`) and a static instruction string focused on product
  search.
- **`scripts/retrievers.py`** -- defines `search_collection`, the Vector Search
  2.0 query that backs `retrieve_docs`, plus a `search()` convenience
  wrapper used by the Step 9 smoke test.

## Shape

```
User query
    |
root_agent (Gemini, ADK)
    |  instruction = "search the catalog for every query, present results..."
    |
retrieve_docs(query)
    |
search_collection(query, collection_path, top_k=10)
    |
vectorsearch.DataObjectSearchServiceClient.search_data_objects(
    SemanticSearch(search_text=query, search_field="text_embedding",
                   task_type="QUESTION_ANSWERING", top_k=10,
                   output_fields=[product_id, name, price, description,
                                  category, brand, rating, stock])
)
    |
Formatted "Product 1: name, $price, by brand, rated X/5, <desc snippet>" string
```

## Configuration

The agent reads these env vars at runtime:

| Variable | Default | Purpose |
|---|---|---|
| `GEMINI_MODEL` | `gemini-3.5-flash` | LLM used by `root_agent` |
| `GOOGLE_CLOUD_PROJECT` | from ADC | GCP project for Vertex AI |
| `GOOGLE_CLOUD_LOCATION` | `global` | LLM region |
| `VECTOR_SEARCH_LOCATION` | `us-central1` | Vector Search region (used to build the default collection path) |
| `VECTOR_SEARCH_COLLECTION` | `projects/<project>/locations/<region>/collections/retail-skill-products-collection` | Full collection resource path |

`VECTOR_SEARCH_COLLECTION` must match the regex
`projects/<p>/locations/<r>/collections/<id>` with no whitespace. A
newline embedded mid-path (common from multi-line shell pastes) causes the
SDK to return a confusing 501 -- `scripts/agent.py` validates this up front.

## Extending

To add a tool (e.g. cart actions, inventory checks, price-history lookups):

1. Define a function in `scripts/agent.py` with a docstring describing when to
   use it -- ADK feeds the docstring to the LLM as the tool description.
2. Append it to the `tools=[...]` list on `root_agent`.
3. Update the `instruction` string so the LLM knows when to pick the new
   tool over `retrieve_docs`.

To change the LLM, set `GEMINI_MODEL` -- no code changes needed.

To change the retrieval contract (different fields, different `top_k`,
filtering), edit `search_collection` in `scripts/retrievers.py`. Note that VS
2.0 semantic search has no built-in structured filters; any
price/stock/category gating must happen in the agent's prompt or in a
client-side post-filter.
