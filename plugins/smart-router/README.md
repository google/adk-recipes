# Smart Router Plugin (`plugins/smart-router`)

The **Smart Router Plugin** (`adk-smart-router`) provides an AI coding assistant skill following the [Google Agent Plugins v1.0.0 specification](https://agent-plugins.org/specification) (`plugin.json` + `skills/adk-smart-router/SKILL.md`). It guides developers through qualifying, wiring, configuring, and operating `SmartRouterPlugin` on ADK agents to prune large tool catalogs per session while preserving Gemini implicit context caching.

**Team:** `cloud-gtm/agentic-data-science` · **Author / Point of Contact:** Mike Henderson ([`@statmike`](https://github.com/statmike)) · **License:** Apache-2.0

---

## Included Skill

| Skill | Directory | Capabilities |
| --- | --- | --- |
| [`adk-smart-router`](./skills/adk-smart-router/) | `skills/adk-smart-router/SKILL.md` | • Offline prompt and argument-graph qualification (`smart-router qualify`)<br>• Runner-level `SmartRouterPlugin` wiring before `BigQueryAgentAnalyticsPlugin`<br>• Day-0 zero-shot `PrototypeJudge` (`gemini-embedding-2`), trained `xgboost`, and hybrid routing configuration<br>• Principal-scoped deterministic tool result caching (`cacheable_tools` + automatic write invalidation) |

---

## Prerequisites

- Python 3.11+ and [`uv`](https://docs.astral.sh/uv/)
- [`adk-smart-router`](https://pypi.org/project/adk-smart-router/) (`uv add adk-smart-router`)
- Google Cloud project with Gemini Enterprise and BigQuery APIs enabled when running active routing (`SMART_ROUTER_MODE=active`)

---

## Quickstart & Installation

Install the skill directly from `google/adk-recipes` or from the upstream repository:

```bash
npx skills add google/adk-recipes --skill adk-smart-router
```

Or install the Python package in your ADK project and run the offline qualification check:

```bash
uv add adk-smart-router
uv run smart-router qualify --app myapp.agents:root_agent
```

---

## Resources

- [Upstream repository (`statmike/adk-smart-router`)](https://github.com/statmike/adk-smart-router)
- [PyPI package (`adk-smart-router`)](https://pypi.org/project/adk-smart-router/)
