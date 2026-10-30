# Quickstart

- **Deployers** run `make onboard` once to deploy the voice app to Google Cloud (Cloud Run behind IAP, Cloud SQL, Memory Bank, and computer infrastructure) and register Google and GitHub OAuth apps.
- **Users** sign in, connect their apps in Settings, and talk to it. Each user's computer is created on first use.

In this doc:

- [Prerequisites](#prerequisites)
- [1. Deploy](#1-deploy)
- [2. Use it](#2-use-it)
- [Develop locally](#develop-locally)
- [Deploy details](#deploy-details)
- [Run as a coding agent](#run-as-a-coding-agent)
- [Tests](#tests)
- [Troubleshooting](#troubleshooting)

## Prerequisites

- Python 3.11+ and [`uv`](https://docs.astral.sh/uv/)
- Node.js 20+ and `npm`
- [Google Cloud CLI](https://cloud.google.com/sdk/docs/install) (`gcloud auth login` and `gcloud auth application-default login`) on a billing-enabled project with access to `LIVE_MODEL` and your chosen harness models (`claude` on Vertex AI, `antigravity` on Gemini)
- Terraform
- Optional: Docker (`make docker` and `docker` computer provider)

## 1. Deploy

```bash
make onboard
```

`make onboard` (`PROJECT=<id>` skips the project prompt; `DRY_RUN=1` previews without changes):

1. Installs dependencies, writes `.env` from [`.env.example`](../.env.example), and checks ADC.
2. Registers Google and GitHub apps, each skippable ([Connected apps](connected-apps.md)).
3. Runs `terraform apply` ([`terraform/README.md`](../terraform/README.md)), enabling Workspace and MCP APIs (`enable_workspace_apis`).
4. Builds and pushes the computer image ([`sandbox/Dockerfile`](../sandbox/Dockerfile)) with Cloud Build if not already in the registry.
5. Deploys the app ([`scripts/deploy.py`](../scripts/deploy.py)).

To grant access to other users, set `iap_users` in `terraform/terraform.tfvars` (from [`prod.tfvars.example`](../terraform/prod.tfvars.example)) and run `make deploy`.

> [!WARNING]
> **Billable resources.** Provisions one always-on Cloud Run instance, Cloud SQL (`enable_cloud_sql`), Cloud NAT, and per-user GCE VMs ([Your computer: when it runs](computer.md#when-it-runs)) with 50 GB disks (~$5/mo), plus model token usage. Run `make destroy` (`PURGE=1` to delete user home disks) to tear everything down.

## 2. Use it

Open the service URL, sign in, and tap **Live**:

1. Connect Google and GitHub in **Settings → Apps**.
2. Try *"What's on my calendar today?"*, *"Fix the failing test in voice-agent-playground."*, or *"Plan me a three-day trip to Lisbon next month."*

Work runs in multi-turn threads grouped on Home (`Needs you`, `Running`, `Recent`) and under projects in the menu (☰). Gated actions pause at `awaiting_approval`, code branches push only on `publish_thread`, and finished deliverables save to Google Drive. See [Your computer](computer.md).

> [!NOTE]
> Browsers require `https://` or `localhost` for microphone access.

## Develop locally

```bash
make dev
```

Runs the UI on <http://127.0.0.1:3000> and API on :8000 using `.env` and the local `host` provider ([Your computer: where it runs](computer.md#where-it-runs)). `make docker` runs the production container on <http://127.0.0.1:8080>.

## Deploy details

```bash
make deploy            # terraform apply, then agents-cli deploy
make deploy DRY_RUN=1  # preview both steps
make computer-image    # build a new computer image and print its tag
make destroy           # delete computers, then terraform resources
```

See [`terraform/README.md`](../terraform/README.md) and the [security model](security-model.md). Set `enable_cloud_sql = false` in `terraform/terraform.tfvars` for throwaway single-user demos (uses ephemeral container SQLite).

## Run as a coding agent

Never block on interactive prompts (`isatty()` is often true in agent shells). Pass `NONINTERACTIVE=1` (or `--no-input` to `scripts/onboard.py` and `scripts/deploy.py`; on by default when `CI` is set):

```bash
make onboard PROJECT=<id> NONINTERACTIVE=1 DRY_RUN=1
make onboard PROJECT=<id> NONINTERACTIVE=1
make deploy DRY_RUN=1
make deploy AUTO_APPROVE=1 NONINTERACTIVE=1
make doctor
```

Non-interactive runs skip GitHub App creation unless its keys are passed ([Connected apps](connected-apps.md#set-up-once-in-make-onboard)).

## Tests

| Command | What it checks |
|---|---|
| `make test` | Offline unit, runnability, and web tests using [`tests/fake_agent_cli.py`](../tests/fake_agent_cli.py) |
| `uv run pytest -m live tests/live` | One real Gemini Live turn (requires GCP) |
| `make lint` | ruff (0.16.8), codespell, ty, mypy, Biome 2.5.10 (pinned binary in `.cache/`), and web typecheck (`make format` applies fixes) |
| `npm --prefix web test` | Web unit tests (vitest) |
| `make eval`, `make eval-sandbox` | System 1 voice evals and System 2 docker computer evals ([Evals](evals.md)) |

## Troubleshooting

| Symptom | Fix |
|---|---|
| Model `404 Not Found` | Check `LIVE_MODEL_LOCATION` (`us-central1`) for `LIVE_MODEL` and `GOOGLE_CLOUD_LOCATION` (`global`) for text models; never change model names to work around location errors. |
| `make dev` address in use | Stop the process on :8000/:3000 or set `API_PORT=` / `WEB_PORT=`. |
| Claude Code web search blocked | Allow `publishers/anthropic/models/<model>:web_search` on `constraints/vertexai.allowedPartnerModelFeatures` for your project (`<model>` is `ANTHROPIC_MODEL`, default `claude-opus-5-5`). |
| Computer or SSH issues | See [Your computer: troubleshooting](computer.md#troubleshooting). |
| Connected app issues | See [Connected apps: troubleshooting](connected-apps.md#troubleshooting). |
| `make destroy` subnet `resourceInUseByAnotherResource` | Direct VPC egress releases `serverless-ipv4-*` addresses 1–2 hours after service deletion; re-run `make destroy` later. |

## Where to go next

- [Your computer](computer.md).
- [Connected apps](connected-apps.md).
- [Architecture](architecture.md).
- [Security model](security-model.md).
- [Configuration](configuration.md).
