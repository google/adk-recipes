# Ambient Expense Agent

A production-ready **ambient agent** that processes expense reports arriving via
Pub/Sub and routes them through an **ADK 2.0 graph-based workflow**. Low-value
expenses are auto-approved instantly; high-value ones go through LLM risk
analysis and **human-in-the-loop approval** before a decision is made.

<table>
  <thead>
    <tr>
      <th colspan="2">Key Features</th>
    </tr>
  </thead>
  <tbody>
    <tr>
      <td>🔄</td>
      <td><strong>ADK 2.0 Graph Workflow:</strong> Conditional routing with function nodes and LLM agents in the same graph — business rules stay in code, LLM handles judgment calls.</td>
    </tr>
    <tr>
      <td>📡</td>
      <td><strong>Ambient & Event-Driven:</strong> Listens for expense events via <a href="https://cloud.google.com/pubsub">Pub/Sub</a> triggers and processes them automatically in the background.</td>
    </tr>
    <tr>
      <td>✋</td>
      <td><strong>Human-in-the-Loop:</strong> High-value expenses pause the workflow with <code>RequestInput</code> until a manager approves or rejects via a dedicated approval UI.</td>
    </tr>
    <tr>
      <td>☁️</td>
      <td><strong>Production-Ready Deployment:</strong> One-command <a href="https://www.terraform.io/">Terraform</a> setup — two <a href="https://cloud.google.com/run">Cloud Run</a> services, Pub/Sub, Cloud Monitoring alerts, IAM, and <a href="https://cloud.google.com/iap">IAP</a>.</td>
    </tr>
  </tbody>
</table>

| Attribute | Description |
| :--- | :--- |
| **Interaction Type** | Ambient (event-driven) with HITL approval |
| **Complexity** | Intermediate |
| **Agent Type** | ADK 2.0 Graph-based Workflow |
| **Trigger Sources** | Pub/Sub push |

## How It Works

The agent is built as an ADK 2.0 [`Workflow`](https://adk.dev/workflows/) with
conditional routing. The $100 threshold lives in code, not in a prompt — only
high-value expenses hit the LLM. See
[`expense_agent/agent.py`](expense_agent/agent.py) for the full graph definition.

```
  Expense arrives (Pub/Sub)
            │
     parse & extract data
            │
      route by amount
       │          │
   < $100       >= $100
       │          │
  auto-approve   LLM reviews risk
   (done)        & emails alert
                  │
            manager approves
             or rejects
             (approval UI)
                  │
            agent logs decision
             & takes action
```

### Deployment Architecture

The agent deploys as two [Cloud Run](https://cloud.google.com/run) services
with [Cloud Monitoring](https://cloud.google.com/monitoring) for email alerts:

- **Backend** — runs the ADK agent. Pub/Sub pushes expense messages to it
  directly (authenticated via service account).
- **Frontend** — the approval UI. Protected by
  [Identity-Aware Proxy (IAP)](https://cloud.google.com/iap) so only
  authorized managers can access it. Calls the backend on behalf of the user.
- **Monitoring** — when the agent flags a high-value expense, it emits a
  structured log. A log-based metric triggers an email alert to the manager
  with a link to the approval UI.

```
                       ┌─────────────────────────┐
  Pub/Sub ───────────► │  Backend  (Cloud Run)   │
                       │  ADK agent + triggers   │
                       └──────┬─────────▲────────┘
                              │         │
                    structured log      │
                              │         │
                       ┌──────▼──────┐  │
                       │  Cloud      │  │
                       │  Monitoring │  │
                       └──────┬──────┘  │
                              │         │
                        email alert     │
                              │         │
                       ┌──────▼──────┐  │
                       │  Manager    │  │
                       └──────┬──────┘  │
                              │         │
                       ┌──────▼─────────┴────────┐
  Browser ── login ──► │  Frontend  (Cloud Run)  │
                       │  Approval UI (IAP)      │
                       └─────────────────────────┘
```

## Getting Started

**Prerequisites:** [Python 3.11+](https://www.python.org/downloads/), [uv](https://github.com/astral-sh/uv)

### 1. Clone the repository

```bash
git clone https://github.com/google/adk-recipes.git
cd adk-recipes/core/python/ambient-expense-agent
```

### 2. Configure authentication

Create a `.env` file (see [`.env.example`](.env.example)).

**Option A: [Google AI Studio](https://aistudio.google.com/app/apikey)**

```bash
echo "GOOGLE_API_KEY=YOUR_AI_STUDIO_API_KEY" >> .env
```

**Option B: [Google Cloud Vertex AI](https://cloud.google.com/vertex-ai)**

```bash
echo "GOOGLE_GENAI_USE_VERTEXAI=TRUE" >> .env
echo "GOOGLE_CLOUD_PROJECT=YOUR_PROJECT_ID" >> .env
echo "GOOGLE_CLOUD_LOCATION=global" >> .env
gcloud auth application-default login
```

### 3. Install and run

Start the backend:

```bash
make install && make dev
```

In a separate terminal, start the approval UI:

```bash
make install-frontend && make dev-frontend
```

### 4. Try it out

Open the ADK playground to interact with the agent directly:

```bash
make playground
```

This starts the ADK web UI at `http://localhost:8501`.

To test the full Pub/Sub trigger flow, send an expense in another terminal:

```bash
curl -s http://localhost:8080/apps/expense_agent/trigger/pubsub \
  -H "Content-Type: application/json" \
  -d "{\"message\":{\"data\":\"$(echo '{"amount":250,"submitter":"alice@company.com","category":"travel","description":"Flight to NYC","date":"2026-04-10"}' | base64)\",\"attributes\":{\"source\":\"test\"}},\"subscription\":\"test-sub\"}"
```

This $250 expense triggers review + HITL approval. Open the approval UI
at `http://localhost:8081/approval` to approve or reject it.

> **Tip:** Expenses under $100 are auto-approved — change `amount` to
> `45` to test that path.

## Cloud Deployment

Deploy both services and all supporting infrastructure with a single command.

**Prerequisites:** [Google Cloud SDK](https://cloud.google.com/sdk/docs/install), [Terraform](https://www.terraform.io/)

```bash
gcloud config set project YOUR_PROJECT_ID
make deploy NOTIFICATION_EMAIL=finance@example.com
```

This builds container images (in parallel) and deploys everything via
Terraform: two Cloud Run services, Pub/Sub (with dead-letter), Cloud
Monitoring alerts, IAM, and IAP.

> **Note:** IAP can take **5–10 minutes** to fully propagate after the
> initial deployment. If you see a `403 Forbidden` when opening the
> approval UI, wait a few minutes and refresh.

### Test the deployed agent

```bash
make remote-test
```

This publishes a $250 travel expense. The agent will route it to the review
agent, analyze risk factors, email an alert to `NOTIFICATION_EMAIL`, and pause
for human approval. Open the approval UI (URL printed by `make deploy`) to
approve or reject.

### Cleanup

```bash
make clean NOTIFICATION_EMAIL=finance@example.com
```

## Customization

| What to change | How |
| --- | --- |
| **Approval threshold** | Change `review_threshold` in `expense_agent/config.py` |
| **LLM model** | Change `model` in `expense_agent/config.py` |
| **Expense schema** | Edit the `ExpenseData` Pydantic model in `expense_agent/agent.py` |
| **Review logic** | Edit the `review_agent` instruction in `expense_agent/agent.py` |
| **Approval UI** | Edit `frontend/static/approval.html` |
| **Downstream actions** | Add workflow nodes for Slack, databases, or notifications |
| **Multi-level routing** | Add routes (e.g., `ESCALATE` for expenses > $1000) |
| **Notification channel** | Replace email with Slack, PagerDuty, or SMS in `terraform/monitoring.tf` ([docs](https://cloud.google.com/monitoring/support/notification-options)) |
| **Email content** | The alert email uses a static template. To include dynamic expense data (amount, submitter) in the email, switch from log-based metrics to [custom metrics with template variables](https://cloud.google.com/monitoring/alerts/doc-variables) |

## Troubleshooting

- For general ADK issues, see the [ADK documentation](https://adk.dev).
- For trigger endpoint details, see [Ambient Agents](https://adk.dev/runtime/ambient-agents/).
- For Cloud Run deployment, see [Deploy to Cloud Run](https://adk.dev/deploy/cloud-run/).

## Disclaimer

This agent recipe is provided for illustrative purposes only. It serves as a basic example of an agent and a foundational starting point for individuals or teams to develop their own agents.

Users are solely responsible for any further development, testing, security hardening, and deployment of agents based on this recipe. We recommend thorough review, testing, and the implementation of appropriate safeguards before using any derived agent in a live or critical system.
