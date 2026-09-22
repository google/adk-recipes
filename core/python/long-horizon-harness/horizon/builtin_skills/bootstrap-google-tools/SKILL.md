---
name: bootstrap-google-tools
description: Install/auth CLIs the sandbox lacks - `gws` (Drive, Gmail, Sheets, Calendar), `gcloud`, `agents-cli`, `mcp-cli` (MCP servers). Use on "command not found" or before GCP/Workspace/MCP work.
---
# Install & authenticate CLIs in the sandbox

The sandbox ships lean — Node, `gws`, `gcloud`, `agents-cli`, and `mcp-cli` are
**not** prebaked. This skill is the verified install + auth recipe for each.
The tools are **independent**: install only the one the task needs. This is a
menu, not a sequence — there's no 1-2-3 to run in order.

## Persistence model — binaries in `~/.local`, credentials in `/workspace`

Two different durability rules, because the sandbox has two boundaries:

- **Across normal sessions** the sandbox reattaches, so *everything* (`$HOME`
  included) persists. Install once, reuse next session.
- **Across a runtime-image upgrade** the sandbox is re-provisioned and **only
  `/workspace` is migrated** — and that migration is a zip that **drops symlinks
  and strips executable bits** (and is best-effort). So it can't carry installed
  CLIs at all; it's only safe for plain data files.

That dictates the split:

| What | Where | On upgrade |
|---|---|---|
| CLI binaries | `~/.local` (bin dir `~/.local/bin` is on `PATH`) | gone — reinstalled (cheap, automatic via the `command -v` check) |
| Credentials / config | `/workspace/lha/config/` (`gws/`, `gcloud/`, `mcp_servers.json`) | **migrated** — logins survive |

So: install binaries the normal way, but point each tool's **config dir** at
`/workspace/lha/config` (via its own env var, shown per tool) so the expensive
part — the OAuth client + tokens — survives upgrades. Don't put binaries on
`/workspace`: they can't migrate anyway, and bloating the migration zip risks
the credentials that *can*. `~/.local/bin` is already on `PATH` (runtime image),
and the `bash` shell is non-login (no `~/.profile`), so that image `PATH` is
what makes installs resolve.

## Pick what you need

| Task | Tool | Needs first |
|---|---|---|
| Google Workspace — Drive / Gmail / Sheets / Calendar / Chat | `gws` | Node |
| Raw GCP, or creating a `gws` OAuth client by hand | `gcloud` | — |
| Call another agent deployed remotely (A2A / ADK) | `agents-cli` | — (`uv` is present) |
| Use tools exposed by an MCP server | `mcp-cli` | — (standalone binary) |

Each tool's **usage** lives in its own skill (`google-workspace`, mcp-cli's
shipped skill, the `google-agents-cli-*` set). This skill only gets a tool
installed and authed, then hands off.

## Before installing: check first

Within a session (and across sessions without an upgrade) a tool may already be
present — check before installing, and install only what's missing:

```
bash(command="command -v gws")   # or gcloud / agents-cli / mcp-cli / node
```

## Node + npm  *(prerequisite for `gws`)*

The sandbox is Debian x86_64 with `curl` and `tar` but **no `xz`**. So you must
download the `.tar.gz` build, **not** the `.tar.xz`. Pick a current LTS version:

```
bash(command='mkdir -p ~/.local/bin && cd ~ && V=v22.x.x && \
  curl -fsSLo node.tgz "https://nodejs.org/dist/$V/node-$V-linux-x64.tar.gz" && \
  tar -xzf node.tgz -C ~/.local && rm node.tgz && \
  ln -sf ~/.local/node-$V-linux-x64/bin/node ~/.local/node-$V-linux-x64/bin/npm ~/.local/node-$V-linux-x64/bin/npx ~/.local/bin/')
```

Replace `v22.x.x` with the real current LTS. The `ln -sf … ~/.local/bin/` step
puts `node`/`npm`/`npx` on `PATH`. Then verify:

```
bash(command="node --version && npm --version")
```

- Use `.tar.gz` — `.tar.xz` will fail to extract (`xz` is absent).

## `gws` — Google Workspace CLI

Install the npm global into `~/.local` (needs Node on `PATH` — see above) with
`--prefix`, so the binary lands at `~/.local/bin/gws`:

```
bash(command='npm install -g --prefix ~/.local @googleworkspace/cli && gws --version')
```

Expect `0.22.x`. `gws` self-identifies as "not an officially supported Google
product."

**Keep `gws` credentials on `/workspace`.** By default `gws` reads/writes
`~/.config/gws` — which is **not** migrated on a runtime upgrade. Point it at
`/workspace` and use the file keyring (no OS keyring headless) **on every `gws`
command** so the OAuth client, token, and encryption key survive:

```
export GOOGLE_WORKSPACE_CLI_CONFIG_DIR=/workspace/lha/config/gws
export GOOGLE_WORKSPACE_CLI_KEYRING_BACKEND=file
```

**First, always: probe the pre-injected token.** When the user has used
**"Connect Workspace"** in the web UI, the `GOOGLE_WORKSPACE_CLI_TOKEN` secret is
auto-injected into every `bash` command — `gws`'s **highest-priority** auth
source. Don't inspect the environment or ask; just run a cheap read against the
surface you need and see if it works:

```
bash(command="export GOOGLE_WORKSPACE_CLI_CONFIG_DIR=/workspace/lha/config/gws GOOGLE_WORKSPACE_CLI_KEYRING_BACKEND=file && gws gmail messages list --params '{\"maxResults\": 1}'")
```

If it returns data, you're done — **no OAuth client, no `gws auth login`, no
loopback bridge**. It's a ~1h token (no refresh); the user re-clicks Connect
when it lapses. The token only carries the **surfaces + access level**
(read-only by default) the user picked — a `gws` call outside those scopes fails
(`403`/scope error), which means "reconnect with more surfaces or read-write,"
**not** that the token is missing. **Don't trust `gws auth status`** — with the
env token it reports `auth_method: none` / `credential_source: token_env_var`
even while reads succeed, so a real read is the only reliable check. Only if the
probe `401`s (truly no token) do you fall through to the OAuth-client options
below.

**Otherwise, point `gws` at an OAuth client** — `gws` ships none of its own. Pick one:

1. Drop a `client_secret.json` into `/workspace/lha/config/gws/` (must be a
   **Desktop app** OAuth client — see the `google-workspace` skill).
2. Set `GOOGLE_WORKSPACE_CLI_CLIENT_ID` + `GOOGLE_WORKSPACE_CLI_CLIENT_SECRET`.
3. Service account: `GOOGLE_APPLICATION_CREDENTIALS=/workspace/lha/config/gws/key.json`
   — for fully unattended automation (no interactive login).
4. `gws auth setup` — provisions a project + OAuth client, but is a full-screen
   TUI that **can't be driven headless**; prefer option 1 in the sandbox.

`gws auth login` is loopback-browser only, but **does complete headless** — you
bridge the OAuth redirect by hand (start it backgrounded, relay the auth URL to
the user, then `curl` their pasted `localhost:<port>/?code=...` redirect back to
the waiting listener). The **`google-workspace`** skill has the full auth recipe
(Desktop-app client type, Internal audience, the loopback bridge) plus command
shapes (Drive, Docs, Sheets, Gmail, Calendar, Chat). For fully unattended jobs
with no user present to paste the redirect, use the service-account option.

**Under a routine, the Google/gcloud token is present only if you declared its
secret name (`GOOGLE_WORKSPACE_CLI_TOKEN` / `CLOUDSDK_AUTH_ACCESS_TOKEN`) in the
routine's `secrets:`.**

**Once `gws` is set up, strongly suggest installing Google's official `gws`
skills.** They live in the `googleworkspace/cli` repo and go deeper than the
builtin `google-workspace` skill — one per surface. Install the ones the user
needs (`gws-shared` is the common base the others build on):

```
bash(command="npx --yes skills add googleworkspace/cli@gws-shared -y")
bash(command="npx --yes skills add googleworkspace/cli@gws-gmail -y")
# also: gws-drive, gws-docs, gws-docs-write, gws-sheets, gws-calendar,
#       gws-events, gws-chat-send
```

The Skills CLI stages each under `.agents/skills/<skill>/`, which Horizon
auto-discovers — just `load_skill(action="reload")`, no move needed (see the
`find-skills` skill for details). (`npx --yes skills find gws` lists the
current set with install counts.)

## `gcloud`

Install under `~/.local` (slim — don't add extra components) and symlink the
entrypoints onto `PATH`:

```
bash(command='mkdir -p ~/.local/bin && cd ~/.local && \
  curl -fsSLo gcloud.tgz "https://dl.google.com/dl/cloudsdk/channels/rapid/downloads/google-cloud-cli-linux-x86_64.tar.gz" && \
  tar -xzf gcloud.tgz && rm gcloud.tgz && \
  ./google-cloud-sdk/install.sh --quiet --usage-reporting=false --path-update=false && \
  ln -sf ~/.local/google-cloud-sdk/bin/gcloud ~/.local/google-cloud-sdk/bin/gsutil ~/.local/google-cloud-sdk/bin/bq ~/.local/bin/')
```

**No durable gcloud credentials are kept in the sandbox.** GCP access is via the
short-lived **token-as-secret** path below, which persists nothing — no refresh
token, no ADC file, no key. (You may set `CLOUDSDK_CONFIG=/workspace/lha/config/gcloud`
as a scratch config dir, but there is no login state to persist across upgrades.)

Verify:

```
bash(command="gcloud --version")
```

### Security: GCP creds is a high-trust combination

Logging in deposits credentials **the agent** can use, in a sandbox with open
outbound internet egress. An injected agent could read the user's GCP data,
`bq extract` / `gsutil cp` it to a foreign bucket, mint a service-account key,
or grant external IAM — all over `*.googleapis.com`. Layer A (exfil_guard) still
gates credential/secret exfil and uploads to non-allowlisted hosts, and still
blocks the GCP metadata server; it has no GCP-action awareness (it can't see
what a `gcloud` call does). **The real control is credential minimization:**

- **Use the short-lived token-as-secret path only** (below). We keep **no durable
  credential** (refresh token / ADC file / SA key) in the sandbox, so a single
  injection can't become standing access that outlives the ~1 h token.
- Mint the token with the **narrowest scopes** the task needs; `cloud-platform`
  (everything the user's IAM allows) is a broad, high-trust default — not a free one.
- Confirm the user actually wants the agent acting as them on GCP before adding the
  token.

### Authenticate — short-lived access token as a secret (the only GCP path)

The lowest-trust, least-friction path, and the **only** one we use (works for corp /
org-restricted accounts too): the user mints a **short-lived access token on their own
machine** (already gcloud-authed there, so the org's device/CAA policy is
satisfied) and hands it to the agent as a **secret**. Nothing durable lands in the
sandbox, and the token self-expires in ~1 hour.

1. The user runs locally, in their own terminal (authed as the right account):
   ```
   gcloud auth print-access-token
   ```
2. The user saves it as a secret **named `CLOUDSDK_AUTH_ACCESS_TOKEN`** in the
   `/lha/secrets` UI — never pasted into chat (it's a live credential).
3. Secrets are **auto-injected as env vars into every `bash` command**, and
   gcloud reads `CLOUDSDK_AUTH_ACCESS_TOKEN` from the environment — so the agent
   just runs commands, no login / ADC / OAuth client at all:
   ```
   bash(command="export CLOUDSDK_CONFIG=/workspace/lha/config/gcloud && gcloud projects list")
   ```
   `gsutil` and `bq` read the same env token. **Don't name the token in a
   `curl`/`wget`** (`-H "Authorization: Bearer $CLOUDSDK_AUTH_ACCESS_TOKEN"`): the
   exfil guard hard-blocks a network command that references a secret env var (by
   design — it stops the agent exfiltrating the token). Use the gcloud-family CLIs,
   which pick it up from the environment without naming it; a raw REST call that
   must carry the token needs a `/grant`.

Why this is the default:
- **Short-lived (~1 h):** a single injection can't become standing access; on
  expiry the user re-mints and updates the secret.
- **Nothing durable in the sandbox** — no refresh token on `/workspace` to reuse.
- **Corp-clean:** consent already happened through the user's org-approved local
  gcloud — no `Account restricted`, no custom OAuth client, no remote-bootstrap.
- It still acts as the user for that window (short lifetime bounds misuse, doesn't
  eliminate it) and carries the user's login scopes. For a **long unattended job**
  the token expires (~1 h) and must be **re-minted and the secret updated** — by
  design we keep no durable credential in the sandbox to fall back on.

## `agents-cli` — call a remote agent (A2A / ADK)

To hand a task to **another agent deployed remotely** (Cloud Run, Vertex Agent
Runtime, or any A2A endpoint), shell out to `agents-cli` — no in-process wiring
needed. This is the outbound counterpart to in-process `subagent()`: the remote
agent is its own service with its own state; you just send a prompt over HTTP.

Install (one-time; `uv` is already in the sandbox — it installs to `~/.local/bin`):

```
bash(command="command -v agents-cli || uv tool install google-agents-cli")
bash(command="agents-cli --version")
```

(Install pulls from wherever `google-agents-cli` is published — if the sandbox
can't reach that index, that's the same egress dependency as the Node/gcloud
downloads.)

Invoke:

```
bash(command='agents-cli run "summarize this repo" --url https://my-agent-xxxx.run.app --mode a2a')
```

- `--mode a2a` for the A2A protocol; `--mode adk` for ADK SSE (`/run_sse`, or
  `:streamQuery` on Agent Runtime). `--mode` is **required** with `--url`.
- `--app-name <name>` to target a specific agent at that endpoint.
- `--session-id <id>` to continue a conversation; `-v` for full JSON events.
- stdout is the remote agent's final response — that *is* your result (blocking).
  For a long-running remote task, run it with `process(action='spawn', command=...)`
  and poll via the `process` tool (fire-and-forget).

**Auth — usually automatic.** `agents-cli` auto-detects Google credentials from
the sandbox identity: an **ID token** (audience = service URL) for Cloud Run, an
**access token** for Vertex AI / Agent Runtime. For this to yield a usable token,
the sandbox/Cloud Run service account needs the right IAM on the target
(`roles/run.invoker` for Cloud Run; Vertex perms for Agent Runtime) — a 401/403
is almost always missing IAM, not a CLI problem.

**User-delegated auth** (call *as the user*): pass the token explicitly, which
overrides auto-detect:

```
bash(command='agents-cli run "..." --url https://... --mode a2a -H "Authorization: Bearer $USER_TOKEN"')
```

Source the token from the secrets / headless-auth subsystem (never inline a raw
secret into chat).

`--url` is a thin client — it loads no local skills (the remote agent's skills
are its own concern). If you need `agents-cli`'s **broader toolchain** (scaffold,
deploy, eval, observability, publish, workflow), install those skills on demand
via the **`find-skills`** skill.

## `mcp-cli` — use tools exposed by an MCP server

To call tools served by an **MCP server** (GitHub, filesystem, databases,
third-party APIs) from the sandbox. This is the *client* side — using external
MCP servers via `bash`, not running one. (Auth is **static token only** —
inject the server's bearer via `${VAR}` headers, below; there is no OAuth/consent
flow. For GCP, use the token-as-secret path in the `gcloud` section, not MCP.)

Install — `install.sh` downloads a **checksum-verified, self-contained prebuilt
binary** (`mcp-cli-linux-x64`, ~100 MB) to `~/.local/bin/mcp-cli`. It's compiled
with Bun but needs **no Bun/Node** at install or runtime — it's standalone.

**Don't pipe-to-bash.** `curl … | bash` is blocked by the tool policy (piping
remote content to a shell). Download the script, then run it from a file:

```
bash(command="command -v mcp-cli || (curl -fsSLo /tmp/mcp_install.sh https://raw.githubusercontent.com/philschmid/mcp-cli/main/install.sh && bash /tmp/mcp_install.sh)")
bash(command="mcp-cli --version")
```

(Optionally `read('/tmp/mcp_install.sh')` first — it just fetches the latest
release binary and verifies its SHA256.) It installs to `~/.local/bin`.

**Configure servers — this is the real onboarding work.** `mcp-cli` is
config-driven: it reads `mcp_servers.json`. Keep it on `/workspace` (config is a
data file — it migrates) and point `mcp-cli` at it with `MCP_CONFIG_PATH` (its
highest-priority resolver); the default `~/.config/mcp/…` is not migrated:

```
export MCP_CONFIG_PATH=/workspace/lha/config/mcp_servers.json
```

Write that file (Claude-Desktop / Gemini / VS-Code compatible):

```json
{
  "mcpServers": {
    "filesystem": {
      "command": "npx",
      "args": ["-y", "@modelcontextprotocol/server-filesystem", "/workspace"]
    },
    "remote-api": {
      "url": "https://mcp.example.com",
      "headers": { "Authorization": "Bearer ${MCP_TOKEN}" }
    }
  }
}
```

- **stdio** server → `command` + `args` the sandbox can spawn (install the server
  too, e.g. via `npx`/`uvx`).
- **HTTP** server → `url` (+ `headers`).
- **Auth:** `${VAR}` in `headers` is substituted from the environment at config
  load (missing var errors unless `MCP_STRICT_ENV=false`). Source that token from
  the **secrets subsystem** (env-injection) — never inline a raw secret.

Use (or install mcp-cli's **own usage skill** via `find-skills` for the full
reference):

```
bash(command="mcp-cli")                                              # list servers + tools
bash(command="mcp-cli info filesystem read_file")                    # tool schema
bash(command="mcp-cli call filesystem read_file '{\"path\": \"./README.md\"}'")
```

Use `call` to invoke and `info` to inspect — `mcp-cli <server> <tool>` directly
is ambiguous and errors.

## Failure cheat-sheet

| Symptom | Cause / fix |
|---|---|
| any tool "command not found" after a runtime upgrade | binaries live in `$HOME` and aren't migrated — reinstall per this skill (the `command -v` check does this) |
| `node`/`gcloud: command not found` after install | the `ln -sf … ~/.local/bin/` symlink step was skipped; re-run it (`~/.local/bin` is on PATH, the versioned/SDK bin dir isn't) |
| `tar: ... cannot exec xz` | downloaded `.tar.xz`; use the `.tar.gz` build instead |
| `gws` asks to log in again after an upgrade | its credentials were written under `$HOME` (not migrated); set `GOOGLE_WORKSPACE_CLI_CONFIG_DIR` to the `/workspace/lha/config/*` path on **every** call (gcloud keeps no persisted login — re-add the token secret) |
| `gws` login token not saved / no keyring | set `GOOGLE_WORKSPACE_CLI_KEYRING_BACKEND=file` (alongside the config-dir export) |
| `gws auth status` says `auth_method: none` but reads work | expected when `GOOGLE_WORKSPACE_CLI_TOKEN` is the source — `auth status` doesn't reflect the env token. Confirm with a real read, not `auth status`; don't start a login dance |
| `gws auth login` hangs on the listener | expected headless — bridge the redirect (see `google-workspace` skill) or use a service account if no user is present |
| `403 access_denied "Account restricted"` | Context-Aware Access on the OAuth client — don't fight it. Use the **token-as-secret** path: user runs `gcloud auth print-access-token` locally and stores it as the secret `CLOUDSDK_AUTH_ACCESS_TOKEN` |
| network command blocked: "reads a credential path or secret env var" | Don't name `$CLOUDSDK_AUTH_ACCESS_TOKEN` (or any `*_TOKEN`/`*_KEY`) in a `curl`/`wget` — the exfil guard hard-blocks it. Let `gcloud`/`gsutil`/`bq` read the token from the env instead; a raw REST call needs a `/grant` |
| Unattended job has no GCP token | the `CLOUDSDK_AUTH_ACCESS_TOKEN` secret is missing or expired; surface "needs a fresh token" and stop — don't loop |
| `agents-cli: command not found` | `uv tool install google-agents-cli` (uv is already in the sandbox) |
| remote `agents-cli run --url` 401/403 | missing IAM on the target (`roles/run.invoker` / Vertex perms), not a CLI bug |
| `--mode is required when using --url` | add `--mode a2a` (or `--mode adk`) to the remote call |
| `mcp-cli: command not found` | download `install.sh` then `bash` the file (don't `\| bash` — policy blocks it); installs to `~/.local/bin` |
| mcp-cli `AMBIGUOUS_COMMAND` | use `mcp-cli call`/`info <server> <tool>`, not `mcp-cli <server> <tool>` |
| mcp-cli `${VAR}` missing at config load | export the token (from the secrets subsystem) or set `MCP_STRICT_ENV=false` |
