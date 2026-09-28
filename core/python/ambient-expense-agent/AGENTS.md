# Ambient Expense Agent — Event-Driven Pub/Sub Workflow with HITL

## Intent

A clone-and-study **ambient agent**: there is no interactive chat loop. Expense
reports arrive as **Pub/Sub** events, get pushed to an ADK trigger endpoint, and
flow through an **ADK 2.0 graph-based `Workflow`**. Business rules stay in code
(a `$100` threshold routes low-value expenses straight to auto-approval); only
high-value expenses reach an LLM `review_agent`, which then **pauses for
human-in-the-loop approval** via `RequestInput` before logging a decision. The
graph itself is compact — the interesting parts are the **ambient plumbing**:
authenticated Pub/Sub push, session-based HITL discovery, an IAP-protected
approval UI, and Cloud Monitoring email alerts, all provisioned by **Terraform**.

## When To Use

- The user wants an **event-driven / ambient** agent (triggered by Pub/Sub, no
  user typing at it) rather than a request/response chat agent.
- The user wants **business rules in code + the LLM only for judgment calls**
  (deterministic threshold routing, LLM used just for risk analysis).
- The user needs **human-in-the-loop approval** that pauses a workflow mid-run
  and resumes it later from a separate UI.
- The user wants a reproducible **two-service Cloud Run + Pub/Sub + IAP +
  Monitoring** deployment as a starting point.

## Eval

- **Scenarios Path**: none — this recipe ships **no eval datasets** and no
  `tests/eval/` directory; nothing is wired into `make test`.
- What exists instead lives under **`tests/`**:
  - `tests/test_runnability.py` — imports `app.agent`, patches
    `google.auth.default()`, and asserts `root_agent` is defined (a smoke test).
  - `tests/test_integration.py` — drives the **full flow in-process** over
    `httpx.ASGITransport` (no real servers): Pub/Sub trigger → auto-approve,
    trigger → review → HITL approve, → HITL reject, and subscription-name
    normalization. This is the closest thing to behavioral coverage.
- **To add eval:** create `tests/eval/` with an `eval_config.yaml` + `datasets/`
  (mirroring the RAG recipes' `agents-cli` LLM-judge format), grading the
  `review_agent`'s risk assessment against fixed expense payloads.

## End-to-end flow

```
expense published to Pub/Sub topic "expense-reports"
   -> authenticated OIDC push -> POST /apps/app/trigger/pubsub
   -> parse_expense_email (base64/plain JSON -> ExpenseData fields)
   -> route_by_amount ($100 threshold, stashes expense_data in ctx.state)
        |                                   |
     < $100                             >= $100
        |                                   |
   auto_approve                        review_agent (LLM)
   (logs INFO, done)                     -> emit_expense_alert (JSON stdout)
                                          -> request_approval (RequestInput -> PAUSE)
```

While paused, a structured `WARNING` log fires a **log-based metric → alert
policy → email** to the manager. The manager opens the **IAP-protected approval
UI**, which finds the pending `adk_request_input` by scanning ADK session events
and resumes the workflow via `POST /run`:

```
   ... PAUSED ... -> manager approves/rejects in approval UI
   -> frontend POST /approve -> backend POST /run (functionResponse)
   -> process_decision (logs approved/rejected, emits final summary) -> done
```

The Pub/Sub push completes as soon as the workflow **pauses** — the ack deadline
is not held for the human; the HITL resume is entirely out-of-band.

## Most interesting files to study (in order)

### Trigger server (the ambient entry point)
1. **`app/fast_api_app.py`** — the whole "ambient" seam. Builds the ADK
   app with `get_fast_api_app(..., trigger_sources=["pubsub"])` so Pub/Sub can
   POST expenses to `/apps/{app}/trigger/pubsub`. Adds middleware that
   **normalizes** `projects/.../subscriptions/NAME` → `NAME`, because the ADK
   trigger handler uses the subscription as the session `user_id` — and the
   frontend must query with that same short name.

### Deployment plumbing (Terraform — what makes it production-ambient)
2. **`terraform/pubsub.tf`** — the trigger wiring: an `expense-reports` topic and
   an **authenticated push** subscription whose `push_config` targets the backend
   trigger URL with an `oidc_token`. `600s` ack deadline, exponential retry, and
   a **dead-letter topic after 5 attempts**.
3. **`terraform/cloud_run.tf`** — the two services. Backend runs the ADK agent;
   frontend is `iap_enabled = true` and gets `BACKEND_URL`, `APP_NAME`,
   `PUBSUB_SUBSCRIPTION`, and `USE_SERVICE_AUTH` wired as env. `min_instance_count
   = 1` keeps one instance warm (relevant to in-memory sessions — see Gotchas).
4. **`terraform/monitoring.tf`** — the alert path. A `google_logging_metric`
   filters `jsonPayload.alert_type="expense_review"` from Cloud Run logs; an alert
   policy fires an **email notification channel** with a markdown link straight to
   the approval UI. This is what turns a stdout log into a manager's email.
5. **`terraform/iam.tf`** — least-privilege service accounts: `pubsub_invoker`
   (`run.invoker` on backend + `serviceAccountTokenCreator` for the Pub/Sub
   service agent's OIDC), `frontend_invoker`, and the **IAP** bindings
   (`iap.httpsResourceAccessor` for the notification email + `run.invoker` for the
   IAP service agent).

### Approval UI (HITL frontend — separate uv sub-project)
6. **`frontend/main.py`** — a thin proxy, not an agent. `GET /pending-approvals`
   queries the **backend's built-in ADK session APIs**, then `_extract_pending_
   approval` scans each session's events for an `adk_request_input` function call
   **without a matching response** (and grabs the `emit_expense_alert` risk
   summary). `POST /approve` forwards a `functionResponse` to the backend's
   `POST /run` to resume the paused workflow. On Cloud Run it mints ID tokens for
   service-to-service auth; locally it calls the backend unauthenticated.

### Agent graph & config (read last)
7. **`app/config.py`** — the auth bootstrap and the two knobs. Picks
   AI Studio if `GOOGLE_API_KEY` is set, else Vertex via `google.auth.default()`
   **at import time**. Exposes `model` (from `MODEL_NAME` env) and
   `review_threshold = 100.0`.
8. **`app/agent.py`** — the payoff, once you understand how events
   arrive and approvals flow back. Defines the `ExpenseData` Pydantic schema, the
   function nodes (`parse_expense_email`, `route_by_amount`, `auto_approve`,
   `request_approval`, `process_decision`), the LLM `review_agent`
   (`emit_expense_alert` tool), and the **`Workflow` graph** that stitches them
   together — a mixed function/LLM graph with conditional routing and a
   `RequestInput` HITL pause. `app/__init__.py` runs `load_dotenv()`
   before the submodule imports (hence the `# noqa: E402`).

## Data handling

- **No documents, no ingestion.** "Data" here is the **expense event payload**
  moving between graph nodes, shaped by the `ExpenseData` Pydantic model
  (`amount`, `submitter`, `category`, `description`, `date`).
- **Decoding:** `parse_expense_email` accepts the Pub/Sub message `data` as
  base64 (real Pub/Sub) or plain JSON (local testing) and coerces fields with
  safe defaults.
- **State handoff:** `route_by_amount` stashes the parsed dict into
  `ctx.state["expense_data"]` so the HITL `request_approval` node and the
  frontend can read it after the pause.
- **Sessions are the durable substrate.** Pending approvals are discovered by
  scanning **ADK session events** for an unanswered `adk_request_input`. The
  session `user_id` is the **Pub/Sub subscription name** (normalized by the
  middleware). No session service URI is configured, so the ADK default
  **in-memory** session service is used.
- **Structured logs are the "output".** `auto_approve`, `emit_expense_alert`,
  and `process_decision` `print(json.dumps(...))` to stdout; Cloud Run captures
  these as structured Cloud Logging entries that drive the alert metric.

## Gotchas / things to know

- **Sessions are in-memory.** With no persistent session service configured,
  pending approvals live only in the backend instance's memory —
  `min_instance_count = 1` keeps one instance warm, but a restart/redeploy
  **drops in-flight approvals**. Wire a persistent session service for real use.
- **The `$100` threshold lives in two places.** `config.review_threshold` (backend
  routing) **and** `REVIEW_THRESHOLD = 100` in `frontend/main.py` (pre-filters
  which sessions to show). Keep them in sync or the UI and the agent disagree.
- **`user_id` == subscription name.** The frontend queries pending approvals
  using `PUBSUB_SUBSCRIPTION` (default `test-sub` locally). If it doesn't match
  the `subscription` field in trigger requests, the UI shows nothing — which is
  exactly why `fast_api_app.py` normalizes the fully-qualified subscription path.
- **Model comes from `MODEL_NAME`.** `config.py` reads `os.getenv("MODEL_NAME")`
  with **no default** (so it is `None` if unset). `.env.example` sets
  `gemini-3.5-flash`; tests set it explicitly. Don't hardcode model names
  and don't use deprecated ones (`gemini-2.0-flash`, `gemini-2.5-flash`).
- **Import-time credentials.** Without `GOOGLE_API_KEY`, importing `config`
  calls `google.auth.default()` immediately — a credential-less import fails
  unless ADC is present. `test_runnability.py` patches it.
- **Alerts only fire when deployed.** `emit_expense_alert` just prints locally;
  the email path (log-based metric → alert policy → channel) exists only in the
  deployed Cloud Monitoring setup.
- **IAP propagation lag.** After `make deploy`, IAP can take **5–10 minutes** to
  propagate; a `403 Forbidden` on the approval UI early on is expected.

## Where to run things

`Makefile` targets:

- `make install` / `make install-frontend` — `uv sync` (backend / frontend
  sub-project).
- `make dev` — run the backend trigger server (`fast_api_app.py`, port 8080).
- `make dev-frontend` — run the approval UI against `BACKEND_URL=localhost:8080`
  (port 8081).
- `make playground` — local ADK web UI (`adk web`, port 8501).
- `make test` — `pytest tests/ -xvs` (runnability + in-process integration).
- `make lint` — `codespell` + `ruff check --fix` + `ruff format` + `mypy`.
- `make deploy NOTIFICATION_EMAIL=...` — build both images via Cloud Build, then
  `terraform apply` the whole stack.
- `make remote-test` — publish a `$250` test expense to the deployed topic.
- `make clean NOTIFICATION_EMAIL=...` — `terraform destroy` (retries once after
  60s for alert-policy propagation).

## Reuse (copy as-is)

- **`terraform/`** is self-contained — two Cloud Run services, Pub/Sub (+
  dead-letter), IAM, IAP, and Cloud Monitoring. Copy the directory and set
  `project_id`, `region`, `notification_email`, and the `backend_image` /
  `frontend_image` vars. It expects **prebuilt images**, so build them first (the
  `Makefile` uses Cloud Build) before `terraform apply`.
- **`frontend/`** is an independent uv sub-project (its own `pyproject.toml`,
  `uv.lock`, and `Dockerfile`) — a generic **ADK HITL approval proxy**. It has no
  code coupling to the backend beyond the ADK session-API contract; point it at
  any ADK agent via `BACKEND_URL`, `APP_NAME`, and `PUBSUB_SUBSCRIPTION`.
- There is **no code coupling into `app/`**: the agent is configured
  purely through env (`MODEL_NAME`, `GOOGLE_API_KEY` / `GOOGLE_CLOUD_*`), and the
  frontend reaches it only over HTTP.
