# Connected apps

Threads can work in a user's Google account (Calendar, Gmail, Drive, Docs, Sheets, Slides) and on GitHub. Setup has two steps: whoever deploys registers the deployment once in `make onboard`, and each user connects their own account via OAuth in Settings. Google Search and Google Maps require no account.

In this doc:

- [Set up (once, in `make onboard`)](#set-up-once-in-make-onboard)
- [Connect (each user, in the app)](#connect-each-user-in-the-app)
- [Being connected is the permission](#being-connected-is-the-permission)
- [Use Google's hosted MCP servers instead](#use-googles-hosted-mcp-servers-instead)
- [Troubleshooting](#troubleshooting)

## Set up (once, in `make onboard`)

`make onboard` runs [`onboard.py`](../scripts/onboard.py) → `register_apps()` before Terraform. Either app can be skipped (`--skip-google`, `--skip-github`, `SKIP_GOOGLE=1`, `SKIP_GITHUB=1`) or passed non-interactively:

```bash
make onboard PROJECT=<id> GOOGLE_OAUTH_CLIENT_ID=... GOOGLE_OAUTH_CLIENT_SECRET=... SKIP_GITHUB=1
make onboard GITHUB_ORG=my-org
```

- **Google:** create a **Web application** OAuth client in the deployment's GCP project with redirect URIs `<APP_URL>/api/v1/auth/google_workspace/callback` and `http://127.0.0.1:8000/api/v1/auth/google_workspace/callback`. Terraform enables the Workspace and MCP APIs (`enable_workspace_apis`), and built-in [`workspace_mcp/`](../workspace_mcp/) stdio servers work without Workspace Developer Preview enrollment.
- **GitHub:** `make onboard` creates the GitHub App via GitHub's manifest flow ([`github_app.py`](../scripts/provisioning/github_app.py)) with Contents, Pull requests, and Issues R/W and Metadata read.

Keys are saved to `.env` ([Configuration](configuration.md#connected-apps)) and copied to Secret Manager by `make deploy`.

## Connect (each user, in the app)

In **Settings → Apps**:

- **Connect / Disconnect:** signs in via OAuth popup and stores tokens per user (`.env` locally, Secret Manager when deployed).
- **Grant access** (Google): requests incremental scopes when new scopes are added.
- **Choose repos** (GitHub): updates the repos installed on the GitHub App, which become `code/<repo>` projects ([Your computer: add a repo](computer.md#add-a-repo)).
- **Saving to Drive:** with **Settings → Save files to Drive** on, threads upload deliverables to `ADK Voice Agent/<project>` via `save_to_drive`.

## Being connected is the permission

Connected apps have no per-service toggles; external or destructive actions pause at `awaiting_approval` ([Security model: permission gates](security-model.md#permission-gates)). If a request needs an unconnected app, the voice opens its Connect page via `navigate` and continues once connected ([Security model: connected apps per user](security-model.md#connected-apps-per-user)).

## Use Google's hosted MCP servers instead

Once your OAuth client's project is enrolled in the [Workspace Developer Preview Program](https://developers.google.com/workspace/preview), you can point entries in [`.mcp.json`](../.mcp.json) at Google's hosted endpoints while keeping the server keys (`gmail`, `google_calendar`, `google_drive`, `google_docs`, `google_sheets`, `google_slides`):

```json
"gmail": {"type": "http", "url": "https://gmailmcp.googleapis.com/mcp/v1"}
```

## Troubleshooting

| Symptom | Fix |
|---|---|
| App not listed in Settings → Apps | Run `make onboard` to set `GOOGLE_OAUTH_CLIENT_ID` / `GOOGLE_OAUTH_CLIENT_SECRET` or `GITHUB_CLIENT_ID` / `GITHUB_CLIENT_SECRET` / `GITHUB_APP_SLUG`. |
| Google `redirect_uri_mismatch` | Add `<APP_URL>/api/v1/auth/google_workspace/callback` and `http://127.0.0.1:8000/api/v1/auth/google_workspace/callback` to the OAuth client. |
| "requires a quota project" or API not enabled | Enable Workspace APIs on the OAuth client's project (`gcloud services enable calendarmcp.googleapis.com gmailmcp.googleapis.com drivemcp.googleapis.com docsmcp.googleapis.com sheetsmcp.googleapis.com slidesmcp.googleapis.com calendar-json.googleapis.com gmail.googleapis.com drive.googleapis.com docs.googleapis.com sheets.googleapis.com slides.googleapis.com --project=<project>`) and reconnect Google in Settings → Apps. |
| Workspace Developer Preview error | Enroll in the [Developer Preview Program](https://developers.google.com/workspace/preview) or revert `.mcp.json` to `{"command": "python", "args": ["-m", "workspace_mcp.gmail"]}`. |
| Drive or Google call needs permission | Tap **Grant access** on Google in Settings → Apps. |
| "could not read Username for 'https://github.com'" | Connect GitHub and select the private repo under **Choose repos** ([Security model: GitHub](security-model.md#github)). |

## Where to go next

- [Security model: connected apps per user](security-model.md#connected-apps-per-user).
- [Architecture: connected apps and auth](architecture.md#connected-apps-and-auth).
- [Extending: add an app](extending.md#add-an-app).
- [Configuration: connected apps](configuration.md#connected-apps).
