# Cross-Border Data Router

A multi-agent ADK recipe that demonstrates a common enterprise compliance
problem: **deciding which regional agent is legally allowed to process a
piece of data, based on where that agent says it operates.**

The root orchestrator receives a data-processing request (a data
classification like "PII" and a data-origin region), evaluates it against a
registry of regional processor agents, and only then delegates the work —
or refuses outright if no agent qualifies. The eligibility check reads
location and compliance metadata directly off each candidate agent's
[Agent2Agent (A2A)](https://a2a-protocol.org/) `AgentCard` — specifically a
custom `capabilities.extensions` entry, since the core A2A spec has no
built-in concept of jurisdiction or data residency. That extension pattern,
and the two-stage hard-filter-then-score routing algorithm this recipe
implements, are modeled on
[OpenEAGO](https://openeago.finos.org/) ([spec repo](https://github.com/finos-labs/open-eago)),
a FINOS Labs specification for governing multi-agent workflows in regulated
industries. OpenEAGO is not a published Python package — this recipe is a
from-scratch implementation of the pattern it describes, not a dependency on
it.

Three regional sub-agents stand in for real backend processors: `eu_processor`
(EU/EEA, GDPR), `uk_processor` (UK, UK-GDPR), and `us_processor` (US, CCPA).
Each is described by a real `a2a.types.AgentCard` carrying its jurisdiction,
data-residency regions, and cross-border restrictions. The policy engine
(`app/policy/engine.py`) eliminates any agent whose declared residency
doesn't cover the request or whose jurisdiction is explicitly excluded, then
scores survivors by jurisdiction preference — never falling back to a
non-compliant agent when nothing qualifies.

**Note on terminology:** "policy" here means declarative data-residency and
jurisdiction rules used to make a *routing* decision. That's distinct from
how other recipes in this repo use "policy" — e.g.
`core/python/long-horizon-harness`'s tool-call guardrails, which gate a
single agent's own actions rather than choosing between agents.

This is also a different problem from `python/agents/global-kyc-agent`,
which routes to a UK or US sub-agent by reading the *company being checked*
out of the query text. Here, routing is driven by metadata each candidate
*agent* declares about itself, evaluated by an explicit policy engine —
closer to how a real multi-agent registry would decide who's allowed to
handle a request at all.

## Setup

Before you begin, ensure you have:
- **uv**: Python package manager — [Install](https://docs.astral.sh/uv/getting-started/installation/)
- A Gemini API key or a Google Cloud project with Vertex AI enabled

From the recipe root (`contrib/python/cross-border-data-router/`):

1. Install dependencies:
   ```bash
   uv sync
   ```

2. Configure credentials — copy `.env.example` to `.env` and fill in either
   a Gemini API key or your Vertex AI project:
   ```bash
   cp .env.example .env
   ```

## Run

Run the agent interactively from the command line:

```bash
uv run adk run app
```

Try prompts like:

- `"A customer in Germany wants their PII record processed. Which regional agent should handle it?"`
  — routes to `eu_processor` or `uk_processor`, both of which declare
  EU-covering data residency.
- `"Route this to whichever EU-compliant agent is preferred in the UK."`
  — same residency requirement, but `uk_processor` wins the scoring stage.
- `"A US customer's financial record must stay in the US, but US-based
  processors are contractually excluded. Route it."`
  — every candidate is eliminated (the only residency-compliant agent is
  also the excluded jurisdiction), so the router refuses rather than
  silently picking a non-compliant region.

Or start the local FastAPI web server and use the ADK dev UI:

```bash
uv run uvicorn app.fast_api_app:app --reload
```

## Running Tests

```bash
uv run pytest
```

`tests/unit/test_policy_engine.py` covers the routing policy directly:
residency filtering, jurisdiction exclusion, preference-based scoring, and
the no-compliant-agent rejection path. `tests/unit/test_tools.py` covers the
`evaluate_and_route` tool the orchestrator calls, plus each regional
sub-agent's own processing tool. Run just those with:

```bash
uv run pytest tests/unit
```

Integration tests that exercise the LLM directly require credentials and
are excluded from CI:

```bash
uv run pytest tests/integration
```

## Commands

| Command | Description |
| ------- | ----------- |
| `uv run adk run app` | Run the agent in interactive CLI mode |
| `uv run uvicorn app.fast_api_app:app --reload` | Start the local FastAPI development server |
| `uv run pytest` | Run the unit test suite (`tests/integration` is excluded by default) |
