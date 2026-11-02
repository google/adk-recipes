# Your computer

Each user gets a personal Linux box with a persistent home directory where coding agents work across threads.

In this doc:

- [How your home is laid out](#how-your-home-is-laid-out)
- [When it runs](#when-it-runs)
- [Open a shell](#open-a-shell)
- [Add a repo](#add-a-repo)
- [Decide what agents may do without asking](#decide-what-agents-may-do-without-asking)
- [Give agents the tools your repo needs](#give-agents-the-tools-your-repo-needs)
- [Check it works](#check-it-works)
- [Where it runs](#where-it-runs)
- [Limits to know](#limits-to-know)
- [Troubleshooting](#troubleshooting)

## How your home is laid out

| Folder | What goes there | Where it comes from |
|---|---|---|
| `code/<repo>` | main checkout of each repo | cloned on first use |
| `projects/<name>` | ongoing goals (`projects/sell-the-car`) | created when named |
| `scratch/<date>-<name>` | one-off threads | created per thread; pruned after 30 days unless promoted via `name_project` |
| `inbox/` | uploads and files to sort | yours |

Every folder is a git repo with one commit per turn (`thread <id>: <title>`). Code threads use branch `agent/<thread_id>`, pushed to `origin` only on `publish_thread`. Concurrent writers get a git worktree merged back on completion ([Architecture: the filesystem](architecture.md#the-filesystem)).

## When it runs

Managed by [`reaper.py`](../app/workers/providers/reaper.py) for `docker` and `gce` ([Configuration: where coding threads run](configuration.md#where-coding-threads-run)):

- **Created on first use** (1–2 minutes on first boot) and **pre-warmed when a voice session opens**.
- **Warm hours:** 08:00–18:00 on weekdays in your timezone by default (`COMPUTER_WARM_HOURS`, `COMPUTER_WARM_DAYS`, `COMPUTER_TIMEZONE`; `off` disables).
- **Idle stop:** stopped after `COMPUTER_IDLE_STOP_MIN` (20 min) of inactivity outside warm hours, keeping the disk.
- **Upgrades:** recreated with the same home disk when the deployment's image template changes.

| Part | When billed | Rough GCE cost |
|---|---|---|
| VM (`e2-standard-2`) | while running | ~$0.07/hr (~$15/mo with warm hours) |
| Home disk (50 GB) | always | ~$5/mo |
| Model tokens | per use | primary cost |

## Open a shell

```bash
make ssh       # gcloud compute ssh --tunnel-through-iap into the container
make doctor    # runs scripts/doctor.sh: tools, daemon, Vertex AI, MCP relay, free disk
```

[`scripts/doctor.sh`](../scripts/doctor.sh) checks `claude`, `agy`, `agents-cli`, `gh`, `git`, `node`, `uv`, `mcp-remote`, daemon health, Vertex AI auth, IAP relay, and disk space. For **VS Code Remote-SSH**, run `gcloud compute config-ssh --project=<project>` for `<service>-u-<hash>` and enter the container with `sudo docker exec -it -u agent computer bash -l` (`/home/agent`).

## Add a repo

Repos come from two merged sources:

- **Your GitHub App installation:** connect GitHub in **Settings → Apps** and use **Choose repos** ([Connected apps](connected-apps.md)).
- **The workspaces manifest:** [`config/workspaces.yaml`](../config/workspaces.yaml) (deployment-wide) and gitignored `config/workspaces.local.yaml`:

  ```yaml
  github_user: "me"
  repositories:
    - name: "my-service"
      git_url: "https://github.com/me/my-service.git"
      upstream_url: "https://github.com/my-org/my-service.git" # optional fork upstream
      branch: "main"
  ```

## Decide what agents may do without asking

| Agents do this freely | Agents ask you first (`mcp_gates.ask`) |
|---|---|
| Edit files, build, test, and commit on the thread's branch | Send an email (`gmail.send*`) |
| Install tools in `~/.local` or with `sudo` | Create or modify calendar events (`google_calendar.create*`, `update*`, `delete*`, etc.) |
| Read mail, calendar, Drive, Slides, and GitHub | Delete or re-share Drive files (`google_drive.delete*`, `trash*`, `permissions*`, `update_metadata*`) |
| Edit Docs and Sheets in place (ungated by design; revision history kept) | Mutate GitHub repos/PRs/issues (`github.create_pull_request*`, `merge*`, `delete*`, `push_files*`, etc.) or publish a branch |

Add custom `mcp_gates.ask` or `permissions.deny` rules in `~/.adk-voice-agent/policy.yaml` ([`config/policy.yaml`](../config/policy.yaml), [Security model: permission gates](security-model.md#permission-gates)).

## Give agents the tools your repo needs

[`sandbox/Dockerfile`](../sandbox/Dockerfile) ships Node.js, `uv`, `git`, `rg`, `jq`, `gh`, `claude`, and `agy`. To persist extra tools:

- **Dotfiles (`DOTFILES_REPO`):** cloned into `~/.dotfiles`; `install.sh` runs idempotently on every boot.
- **`~/.local` and `~/.agents/skills/`:** persist on your home disk (`~/AGENTS.md` holds your conventions).
- **Deployment-wide packages:** add to [`sandbox/Dockerfile`](../sandbox/Dockerfile) ([Extending: customize the computer image](extending.md#customize-the-computer-image)).

### Example: build ADK agents on your computer

Add `agents-cli` and its skills from `~/.dotfiles/install.sh`:

```bash
if ! command -v agents-cli >/dev/null; then
  uv tool install google-agents-cli
fi
skills="$(find ~/.local/share/uv/tools/google-agents-cli -type d \
  -path '*/google/agents/cli/skills/data' | head -n1)"
[ -n "$skills" ] && cp -Rn "$skills"/. ~/.agents/skills/
```

## Check it works

1. Run `make doctor` ([Open a shell](#open-a-shell)).
2. Ask by voice: *"Ask Claude how to run the tests in my-service, and to run them."*
3. Start a real request; questions or plans return as normal turn answers.

## Where it runs

Selected by `COMPUTER_PROVIDER` ([Architecture: where code runs](architecture.md#where-code-runs)):

- `gce`: per-user Compute Engine VM + persistent disk (production default).
- `host`: local directory under `VOICE_AGENT_WORKSPACE_ROOT` (`make dev` default; no isolation).
- `docker`: local container + volume per user (`docker build -t adk-voice-agent-computer:dev sandbox/`).
- `static`: daemon at `COMPUTER_URL_TEMPLATE` or `COMPUTER_URL`.

## Limits to know

- **Repo test secrets:** only Vertex AI ADC is provided ([what reaches the computer](security-model.md#what-reaches-the-computer)).
- **Full clones:** repos clone without sparse checkout.
- **Shared disk per user:** concurrent worktrees do not share `.venv` or `node_modules`.

## Troubleshooting

| Symptom | Fix |
|---|---|
| "Starting your computer…" takes 1–2 minutes | The VM is booting; warm hours pre-start it on workdays. |
| `make ssh` says instance doesn't exist or permission denied | Open a voice session or start a thread first so your VM and IAM bindings are created; run `terraform -chdir=terraform init` if terraform state isn't initialized. |
| `make ssh` / `make doctor` says computer is stopped | Open the app to start it (recreating from any newer image template first). |
| `ConnectTimeout` to `:8080` while VM is `RUNNING` | First-boot disk setup or image pull is still running, or the container failed. `running_environment()` probes `/healthz` (2 s connect) and reports `"starting"` until ready; the reaper stops VMs unreachable past `COMPUTER_IDLE_STOP_MIN`. Inspect via `gcloud compute instances get-serial-port-output <vm> --zone=<zone> \| grep -i computer`, `make ssh` → `journalctl -u computer`, or `gcloud run services describe <service> --region=<region> --format='yaml(spec.template.metadata.annotations)'`. |
| `/mcp-relay` answers `503` | Ensure `APP_URL` is set (`make deploy` sets `COMPUTER_MCP_UPSTREAM` from it). |
| "could not read Username for 'https://github.com'" | Connect GitHub in Settings → Apps and select the private repo under **Choose repos** ([Connected apps](connected-apps.md)). |

## Where to go next

- [Connected apps](connected-apps.md).
- [Architecture: where code runs](architecture.md#where-code-runs).
- [Security model: what reaches the computer](security-model.md#what-reaches-the-computer).
- [Extending: add a computer provider](extending.md#add-a-computer-provider).
