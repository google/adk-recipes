# Deployment

This directory contains the Terraform configurations for provisioning the necessary Google Cloud infrastructure for your agent.

## Deployment Targets

The project supports two deployment targets:

### Cloud Run (`make deploy`)

Deploys the full application (ADK agent + REST API + MCP server + React frontend) as a container to Cloud Run. Uses the `Dockerfile` and `gcloud run deploy --source .`.

- **What's deployed**: Everything — agent, REST API endpoints, MCP server, frontend
- **Best for**: Full application deployment, custom infrastructure, event-driven workloads
- **Entrypoint**: `app/fast_api_app.py`

### Agent Runtime (`make deploy-agent-engine`)

Deploys only the ADK agent to Agent Runtime as a managed service. No Dockerfile needed — source code is packaged and uploaded directly.

- **What's deployed**: ADK agent only (conversational interface)
- **Best for**: Managed infrastructure, minimal ops, agent-only deployments
- **Entrypoint**: `app/agent_engine_app.py`
- **Note**: REST API endpoints (VTO, spinning, etc.) are not included — those still require Cloud Run

## Setup

The recommended way to deploy the infrastructure and set up the CI/CD pipeline is by using the `agents-cli scaffold enhance . --cicd-runner github_actions` command from the root of your project.

However, for a more hands-on approach, you can always apply the Terraform configurations manually for a do-it-yourself setup.

For detailed information on the deployment process, infrastructure, and CI/CD pipelines, please refer to the official documentation:

**[Google Agents CLI Deployment Guide](https://github.com/google/agents-cli)**
