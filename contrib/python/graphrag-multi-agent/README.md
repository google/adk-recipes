# GraphRAG Multi-Agent (Neo4j + ADK)

A multi-agent **GraphRAG** (Graph Retrieval-Augmented Generation) recipe
that answers investment-research questions by reasoning over a **Neo4j
knowledge graph** instead of a vector store. A root orchestrator routes each
question to the specialist best suited to answer it, and the specialists all
read from the same graph of companies, people, articles, industries and
investors.

This recipe adapts the
[Building GraphRAG Agents with ADK and Neo4j](https://codelabs.developers.google.com/neo4j-adk-graphrag-agents)
codelab into the ADK recipe layout. It demonstrates three complementary
retrieval patterns in one agent system:

1. **Text-to-Cypher** — an agent that reads the live schema, writes read-only
   Cypher, and self-corrects when a query errors.
2. **Hand-written graph tools** — a focused agent backed by a single,
   purpose-built Cypher function (`get_investors`).
3. **Pre-validated MCP Toolbox queries** — expert-authored, parameterised
   queries served over the Model Context Protocol (optional).

## Architecture

```
                        ┌─────────────────────┐
   user question  ─────▶│  root orchestrator  │
                        │  (investment_agent) │
                        └──────────┬──────────┘
                delegates to the best specialist
        ┌──────────────────┬───────┴───────────┬─────────────────────┐
        ▼                  ▼                                          ▼
┌────────────────┐ ┌───────────────────┐              ┌────────────────────────┐
│ investor_      │ │ investment_       │              │ graph_database_agent   │
│ research_agent │ │ research_agent    │              │ (Text-to-Cypher,       │
│ get_investors  │ │ MCP Toolbox tools │              │  schema-aware fallback)│
└───────┬────────┘ └─────────┬─────────┘              └───────────┬────────────┘
        └────────────────────┴───── read-only Cypher ─────────────┘
                                     ▼
                          ┌────────────────────┐
                          │  Neo4j companies   │
                          │   knowledge graph  │
                          └────────────────────┘
```

The graph holds `Organization`, `Person`, `Article`, `IndustryCategory` and
investor nodes, connected by relationships such as `HAS_INVESTOR`,
`HAS_COMPETITOR`, `MENTIONS`, `HAS_CEO` and `HAS_CATEGORY`. All access is
read-only — write statements are rejected before they reach the database.

## Prerequisites

- Python **3.11+** and [`uv`](https://docs.astral.sh/uv/).
- A **Gemini API key** from [Google AI Studio](https://aistudio.google.com/app/apikey),
  or a Google Cloud project configured for Vertex AI.
- **Neo4j access.** The defaults target Neo4j's public, read-only companies
  demo database (`neo4j+s://demo.neo4jlabs.com`, user/password `companies`),
  so no database setup is required to try the recipe.

## Setup

From the recipe directory:

```bash
cd contrib/python/graphrag-multi-agent

# 1. Install dependencies into a local virtual environment.
uv sync

# 2. Create your .env from the template and fill in your Gemini API key.
cp .env.example .env
#   Edit .env: set GOOGLE_API_KEY (the Neo4j demo defaults work as-is).
```

## Run

Launch the ADK developer web UI and chat with the agent:

```bash
uv run adk web
```

Then open http://127.0.0.1:8000 and select **app**. You can also run it in the
terminal:

```bash
uv run adk run app
```

### Example prompts

```text
What industries are in the database?
Which companies are in the "Software" industry?
Who are the investors in the company named "Neo4j"?
How many organizations are in the graph?
Find recent articles that mention companies in the automotive industry.
```

## Optional: MCP Toolbox

The `investment_research_agent` can load pre-validated, expert-authored
queries from an [MCP Toolbox](https://googleapis.github.io/genai-toolbox/)
server. This is **optional** — when `MCP_TOOLBOX_URL` is unset, the agent
falls back to the schema + Cypher tools and the recipe still runs.

To enable it:

1. Generate the toolbox config (embeds your Neo4j credentials from `.env`):

   ```bash
   uv run python setup_tools_yaml.py
   ```

2. Download the `genai-toolbox` binary (see the
   [MCP Toolbox docs](https://googleapis.github.io/genai-toolbox/getting-started/introduction/))
   and start it against the generated config:

   ```bash
   ./toolbox --tools-file app/.adk/tools.yaml --port 5000
   ```

3. Set `MCP_TOOLBOX_URL` in `.env` to the server's SSE endpoint (for example
   `http://127.0.0.1:5000/mcp/sse`) and restart `adk web`.

## Environment variables

| Variable | Required | Description |
|---|---|---|
| `GOOGLE_API_KEY` | Yes (AI Studio path) | Gemini API key. |
| `GOOGLE_GENAI_USE_VERTEXAI` | No | `1` to use Vertex AI instead of the API key. |
| `GOOGLE_CLOUD_PROJECT` | Vertex only | GCP project id. |
| `GOOGLE_CLOUD_LOCATION` | Vertex only | GCP region, e.g. `us-central1`. |
| `MODEL_NAME` | No | Gemini model id. Defaults to `gemini-3.5-flash`. |
| `NEO4J_URI` | Yes | Bolt/Neo4j URI. |
| `NEO4J_USERNAME` | Yes | Neo4j username. |
| `NEO4J_PASSWORD` | Yes | Neo4j password. |
| `NEO4J_DATABASE` | No | Neo4j database name. |
| `MCP_TOOLBOX_URL` | No | MCP Toolbox SSE endpoint; enables pre-validated queries. |

## Tests

```bash
uv run pytest
```

The runnability test imports the agent and asserts `root_agent` is defined.
It needs no network access: the Neo4j driver connects lazily on the first
tool call, and the MCP Toolbox is contacted only when configured.

## Security

The agent runs against the graph **read-only**, enforced at two layers:

- **Database-level (primary):** every query executes with
  `routing_=RoutingControl.READ`, so the server rejects any write in the
  transaction regardless of the query text.
- **Application-level (defense-in-depth):** a keyword pre-check rejects
  obvious mutating statements and `LOAD CSV` with a clear message.

A query-string filter alone cannot fully sandbox LLM-generated Cypher (for
example, some procedures can perform network I/O). For production or
untrusted input, connect with a **database user that has only read
privileges**, and restrict procedure allowlists (APOC) and network egress at
the database — that is the definitive control. This recipe targets a public,
read-only demo database, so those controls are already in place upstream.

## Credits

Based on the
[Building GraphRAG Agents with ADK and Neo4j](https://codelabs.developers.google.com/neo4j-adk-graphrag-agents)
codelab and its
[companion repository](https://github.com/sidagarwal04/neo4j-adk-multiagents)
by Neo4j.
