# Market Research Agent

An AI-powered market research system that performs comprehensive competitive analysis for any location and business type. Given an address and business category, it runs four specialist agents in parallel — analyzing competitors, scoring location suitability, estimating foot traffic, and identifying market gaps — then synthesizes everything into a structured report.

## Agent Details

| Feature | Description |
| --- | --- |
| **Interaction Type** | Conversational |
| **Complexity** | Advanced |
| **Agent Type** | Multi Agent |
| **Components** | LlmAgent, AgentTool, FunctionTool, Google Places API |
| **Vertical** | Retail / Business Analytics |

### Architecture

```
market_research_orchestrator (LlmAgent)
├── geocode_address          (FunctionTool)
├── competitor_agent         (AgentTool → LlmAgent)
│   └── nearby_search, place_details, text_search
├── location_agent           (AgentTool → LlmAgent)
│   └── nearby_search, text_search, geocode_address
├── traffic_agent            (AgentTool → LlmAgent)
│   └── nearby_search, place_details
└── gap_agent                (AgentTool → LlmAgent)
    └── nearby_search, text_search, place_details
```

The orchestrator geocodes the input address, then calls all four specialist agents in parallel via `AgentTool`. Each specialist uses Google Places API tools to gather real-world data and returns structured JSON. The orchestrator synthesizes the results into a final market research report.

## Setup and Installation

### Prerequisites

- Python 3.11+
- [`uv`](https://docs.astral.sh/uv/) for dependency management
- A Google AI Studio API key **or** a Google Cloud project with Vertex AI enabled
- A [Google Places API key](https://developers.google.com/maps/documentation/places/web-service/get-api-key)

### Installation

```bash
# Clone the repo
git clone https://github.com/google/adk-samples.git
cd adk-samples/contrib/python/market-research-agent

# Install dependencies
uv sync
```

### Configuration

Copy `.env.example` to `.env` and fill in your credentials:

```bash
cp .env.example .env
```

**Option A — Google AI Studio (simplest):**
```bash
GOOGLE_GENAI_USE_VERTEXAI=false
GOOGLE_API_KEY=your_google_api_key
GOOGLE_PLACES_API_KEY=your_places_api_key
```

**Option B — Vertex AI:**
```bash
GOOGLE_GENAI_USE_VERTEXAI=true
GOOGLE_CLOUD_PROJECT=your-project-id
GOOGLE_CLOUD_LOCATION=us-central1
GOOGLE_PLACES_API_KEY=your_places_api_key
```

If using Vertex AI, authenticate your GCloud account:
```bash
gcloud auth application-default login
gcloud auth application-default set-quota-project $GOOGLE_CLOUD_PROJECT
```

Both options also require `MODEL_NAME` (defaults to `gemini-3.5-flash` in `.env.example`), which is used by the orchestrator and every sub-agent.

## Running the Agent

**With the ADK web UI:**
```bash
uv run adk web
```

**With the ADK CLI:**
```bash
uv run adk run app
```

### Example Interaction

```
You: Analyze the market for opening a specialty coffee shop at 15 Shoreditch High Street, London

Agent: I'll research the market at that location. Let me geocode the address and run
       the full analysis...

       [Runs competitor, location, traffic, and gap analysis in parallel]

       Here is the market research report:

       **Executive Summary**
       Shoreditch High Street is a high-footfall urban corridor with 8 direct competitors
       within 500m. Location scores 71/100 overall. Strong morning commuter traffic
       detected. Key opportunity: zero third-wave specialty roasters in the area despite
       high review volumes at existing cafes.

       **Competitors (8 found)**
       ...

       **Location Score: 71/100**
       - Competition density: 52/100 (saturated)
       - Accessibility: 89/100 (2 tube stations within 400m)
       - Demand signal: 72/100

       **Traffic Estimate**
       Peak hours: 7:30–9:30am and 12–2pm weekdays
       Estimated daily footfall: High | Confidence: Medium

       **Market Gaps**
       1. Specialty/third-wave coffee (score: 85) — 0 roasters found in 1km radius
       2. Evening study cafe (score: 67) — competitors close by 6pm
```

## Running Tests

```bash
uv run pytest tests/ -v
```

## Disclaimer

This agent is provided as a sample for demonstration purposes. Market research outputs are estimates based on publicly available Google Places data and should not be used as the sole basis for business decisions.
