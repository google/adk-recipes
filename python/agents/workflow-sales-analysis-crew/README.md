# Sales Analysis Crew

A minimal multi-agent example showing a sequential analyst-critic-reporter
pipeline using ADK's `Workflow` class.

## Architecture

Three `Agent` instances are wired in a straight line:

- **analyst** — fetches embedded sales CSV via a function tool, computes
  regional totals and trends.
- **critic** — receives the analyst's output and surfaces limitations:
  sample size, seasonality, missing context.
- **report_writer** — synthesises both prior outputs into a concise
  executive summary addressed to senior leadership.

```python
root_agent = Workflow(
    name="sales_analysis_crew",
    edges=[("START", analyst, critic, report_writer)],
)
```

## Running locally

```sh
pip install google-adk python-dotenv
adk run python/agents/workflow-sales-analysis-crew/agent.py:root_agent
```

Set `GOOGLE_CLOUD_PROJECT` and `GOOGLE_CLOUD_LOCATION` in a `.env` file
or export them before running.
