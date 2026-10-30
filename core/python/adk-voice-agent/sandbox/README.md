# The computer image

Each user gets a computer: a Linux box running this image, with the user's
persistent volume mounted at `/home/agent`. System 1 talks to the daemon in it
([`server.py`](server.py)) over HTTP ([`app/workers/environment.py`](../app/workers/environment.py)),
and coding harnesses run inside it as the `agent` user.

## Layout

The image installs **nothing under `/home/agent`**, so any home works with any
image version, and an image bump never touches the user's files.

| Path | What |
|---|---|
| `/home/agent` | The user's volume: `HOME`, the daemon's workspace (`SANDBOX_WORKSPACE`), and the only persistent state. |
| `/opt/venv` | The daemon's Python (fastapi, uvicorn, httpx, google-auth, requests). |
| `/usr/local/bin` | `claude`, `mcp-remote` (npm globals), `agy`, `gh`, `uv`. Plus Node 22, `git`, `rg`, `jq`, `tini`, `sudo` from the system. |
| `/opt/computer/home/` | The home skeleton ([`home/`](home/)): `AGENTS.md` (the guide every agent reads: layout and conventions), `CLAUDE.md` (`@AGENTS.md`), `inbox/`, `projects/`, `code/`, `scratch/`. |
| `/opt/computer/skills/` | Starter skills: [`skills/`](skills/). |
| `/opt/computer/entrypoint.sh` | First boot and every boot ([`entrypoint.sh`](entrypoint.sh)). |

`PATH` is `~/.local/bin`, then `/opt/venv/bin`, then the system
(`app.workers.environment.SANDBOX_PATH_PREPEND`). Version pins are `ARG`s at the
top of the [`Dockerfile`](Dockerfile); downloads are checked against pinned
checksums before they run.

## Boot

The entrypoint runs as root under `tini`, and is idempotent.

1. Gives `/home/agent` to `agent` if needed, and installs the skeleton (below).
2. Installs the starter skills into `~/.agents/skills/` the same way, and links
   `~/.claude/skills` and `~/.gemini/config/skills` to it.
3. Merges the harness globals, keeping the user's own entries:
   `~/.gemini/antigravity-cli/settings.json` (`gcp.project` and
   `gcp.location` from `GOOGLE_CLOUD_PROJECT` / `GOOGLE_CLOUD_LOCATION`, and
   `command(*)` allowed) and `~/.gemini/config/mcp_config.json` (one `voice_agent`
   server: `mcp-remote` with the URL and token from each run's environment).
4. Clones `DOTFILES_REPO` into `~/.dotfiles` if it's set and missing, then runs
   `~/.dotfiles/install.sh` (if present) as `agent` on every boot.
5. Drops to `agent` and execs the daemon on port 8080 (or the command you pass,
   e.g. `docker run -it IMAGE bash`).

Nothing it writes holds a token. MCP credentials reach each harness process only
through its environment.

### Updating shipped files

The skeleton and starter skills are treated like a package's config files. Each
boot records the sha256 of every shipped file it installs in
`~/.voice-agent/seeded.json`. When a new image ships a different version, the file
is replaced only if it still has the recorded hash, i.e. the user hasn't touched
it. A file the user edited, replaced with a link, or deleted stays as they left it.

## Instructions and skills

Agents learn how to work on the computer from files, not from the thread prompt,
which only states the thread's facts (`app/threads/prompts.py` → `system_prompt()`):
its project, working directory, branch, and where the guide and skills are.

| File | What it says |
|---|---|
| [`home/AGENTS.md`](home/AGENTS.md) | What this computer is, the layout, where things go, project `AGENTS.md` files, git as the undo, no secrets in the home. |
| [`skills/filesystem`](skills/filesystem/SKILL.md) | Finding earlier work, keeping a project's `AGENTS.md`, filing the inbox, promoting scratch. |
| [`skills/deliverables`](skills/deliverables/SKILL.md) | Writing a document deliverable (Markdown, `#` title, under 14 KB; saved to Drive with `save_to_drive`). |
| [`skills/recommendations`](skills/recommendations/SKILL.md) | Fresh, preference-aware recommendations that don't repeat earlier ones. |
| [`skills/coding-workflow`](skills/coding-workflow/SKILL.md) | Diagnose without changing files; reproduce, fix the root cause, verify. The server commits; nothing is pushed unless the user asks (the voice agent's `publish_thread`). |
| [`skills/approvals`](skills/approvals/SKILL.md) | A gated tool call (`request_permission`): stop and end the turn; retry the same call once approved. |
| [`skills/connected-apps-mcp`](skills/connected-apps-mcp/SKILL.md) | The `voice_agent` gateway: connected apps, the thread journal, memory, grounded search, `update_user`, `present_card`, and `save_to_drive`; untrusted data. |

Claude Code finds `~/CLAUDE.md` by walking up from the working directory, and its
skills in `~/.claude/skills`. The thread prompt also names `~/AGENTS.md` and
`~/.agents/skills`, for harnesses that read them lazily (agy) and for the `host`
provider, where harnesses run with the developer's own `HOME`.

Each thread's harness files (its context, Claude's `--settings`, `mcp.json`) are
written to `~/.voice-agent/threads/<thread_id>/`, never into a project folder. agy's
per-thread project file lives in `~/.gemini/config/projects/` and is removed when the
thread's iteration ends.

## Endpoints (`server.py`)

| Endpoint | Purpose |
|---|---|
| `GET /healthz` | Reachability probe. |
| `POST /exec` | Runs one shell command under `/bin/sh` in the workspace with `env` merged over the server's environment. Body: `{"command": str, "env": {str: str}, "timeout": float}`. Response: `{"stdout": str, "stderr": str, "exit_code": int, "timed_out": bool}`. |
| `POST /runs/start` | Starts a harness run directly in the daemon and writes its run directory under `<workspace>/.voice-agent/runs/<run_id>`. A harness CLI that isn't on `PATH` fails the run with "not installed in the computer image". |
| `GET /runs/{run_id}/poll?cursor=<int>` | Returns new complete stdout lines, byte cursor, stderr tail, and exit status directly from the daemon without spawning a shell subprocess. |
| `POST /runs/{run_id}/cancel` | Cleanly terminates the run's process group (`SIGTERM` -> `SIGKILL`). |
| `GET /files/list?path=<rel>` | `{"entries": [{"name", "type": "file" \| "dir", "size", "mtime"}]}` (`mtime` is ISO 8601). Symlinks that lead out of the root are not listed. |
| `GET /files/read?path=<rel>&max_bytes=<int>` | `{"content": str, "truncated": bool}`, UTF-8 with undecodable bytes replaced (default limit 1 MiB). |
| `PUT /files/write` | Body `{"path": rel, "content": str, "mode": "0600"?}` → `{"ok": true}`. Creates parent directories and replaces the file atomically; without `mode`, an existing file keeps its mode and a new one gets `0644`. |
| `GET /activity` | `{"active_runs": int, "last_activity": iso8601}`: runs still going, and the last run start, run output, `/exec`, file write, or relayed MCP call. While someone is logged in (a terminal session of the `agent` user, such as `make ssh`'s shell), `last_activity` is now, so the idle stop waits until they log out. |
| `ANY /mcp-relay/{path}` | The MCP relay, below. |

File endpoints take paths relative to the `root` query parameter (default
`SANDBOX_WORKSPACE`), which must itself lie inside `SANDBOX_WORKSPACE`. Absolute
paths, `..`, and paths that resolve through symlinks out of the root get `400`; a
missing path gets `404`.

## The MCP relay

Harnesses reach System 1's MCP bridge at `http://127.0.0.1:8080/mcp-relay/api/v1/mcp`.
The daemon normalizes the path, refuses anything outside `/api/v1/mcp`, and forwards
each request to `${COMPUTER_MCP_UPSTREAM}/api/v1/mcp`, streaming the
request and response bodies (JSON or SSE), and passes the harness's
`Authorization: Bearer <thread token>` through unchanged. When
`COMPUTER_MCP_AUDIENCE` is set (System 1 behind IAP), it adds
`Proxy-Authorization: Bearer <JWT>`: a JWT with that audience, signed by the
computer's service account through the IAM Credentials `signJwt` API (the
account's email and an access token come from the metadata server, and the account
holds Token Creator on itself), as IAP's
[service account JWT](https://cloud.google.com/iap/docs/authentication-howto#authenticate_with_a_service_account_jwt)
flow describes. It is cached until 5 minutes before it expires. So harnesses never
hold the credential that gets past IAP.

The relay answers loopback clients only (`403` otherwise), and skips the daemon's
caller check, because System 1 checks the thread token.

## Caller auth

With `COMPUTER_REQUIRE_AUTH=1`, every request except `/healthz` and the relay must
carry `Authorization: Bearer <Google ID token>` whose audience is
`COMPUTER_AUTH_AUDIENCE` and whose verified email is in the comma-separated
`COMPUTER_ALLOWED_CALLERS`; anything else gets `401`. Auth is off by default, for
a daemon on your own machine.

## Customizing a computer

- **Dotfiles.** Set `DOTFILES_REPO` to a git URL. Its `install.sh` runs on every
  boot, so make it idempotent.
- **Your own tools** go in `~/.local`, which persists: `uv tool install ...`,
  `npm install -g ...` (the image sets `NPM_CONFIG_PREFIX=~/.local`), or binaries in
  `~/.local/bin`. They come first on `PATH`, so they can also shadow the image's.
- **`sudo`** works without a password, but whatever it installs (`apt-get install`)
  lives in the container, not the volume, and is gone when the computer is
  recreated (for example after an image bump). Put lasting setup in your dotfiles.
- **Skills** live in `~/.agents/skills/`. Edit a starter skill freely: once you
  have, the image no longer updates it. Add your own skills there too.
- **`~/AGENTS.md`** is yours as well: add your preferences and conventions. Once you
  edit it, the image keeps your version.
- **The image itself.** Change the `ARG` pins or add packages in the
  [`Dockerfile`](Dockerfile), then rebuild.

## Build and try it locally

```bash
docker build -t adk-voice-agent-computer:dev sandbox/
docker run -d --name computer -p 18080:8080 -v adk-computer-dev:/home/agent adk-voice-agent-computer:dev
curl localhost:18080/healthz
docker exec -it -u agent computer bash
```
