---
name: google-workspace
description: Read/write Google Drive, Docs, Sheets, Gmail, Calendar, Chat, Tasks, Slides via the `gws` CLI. Needs an OAuth client or service account configured first. Use for any Google Workspace task.
---
# Google Workspace via `gws`

`gws` (install: `npm install -g @googleworkspace/cli`; this skill targets
**0.22.x**) speaks every Workspace API the user is likely to ask about —
Drive, Docs, Sheets, Gmail, Calendar, Chat, Tasks, Slides, Keep, Forms, Apps
Script, Meet. It is a community CLI ("not an
officially supported Google product"). It wraps the REST APIs so you don't
hand-roll `curl`, but it does **not** bundle credentials: you must configure
an OAuth client or service account first, then authenticate.

## When to use this skill

Any user request that names a Google Workspace surface:
- "read this Drive file", "list my Drive folder", "share this doc"
- "create a Google Doc with…", "update this sheet", "append a row"
- "send an email", "draft a Gmail reply", "check my inbox for…"
- "what's on my calendar", "schedule a meeting"
- "post to this Chat space"

Do NOT shell out to `curl` against the raw Workspace REST APIs — `gws`
already wraps them and handles pagination, retries, and JSON parsing.

## Auth

### First, always: try the pre-injected token

Before any OAuth client, service account, or login dance, check for
a **pre-injected access token**. When the user has used **"Connect Workspace"**
in the web UI, the `GOOGLE_WORKSPACE_CLI_TOKEN` secret is auto-injected into
every `bash` command and is `gws`'s **highest-priority** auth source — no
OAuth client, no login, no loopback bridge. **Don't ask, don't inspect the
environment, just probe with a cheap read against the surface you need:**

```
bash(command="export GOOGLE_WORKSPACE_CLI_CONFIG_DIR=/workspace/lha/config/gws GOOGLE_WORKSPACE_CLI_KEYRING_BACKEND=file && gws gmail messages list --params '{\"maxResults\": 1}'")
```

- If it returns data, you're done — the token works. Proceed with the task.
- If it **`403`s / scope error**, the token is valid but the user didn't grant
  this surface (or only read-only). The "Connect Workspace" flow is per-surface
  (drive / gmail / calendar / sheets / docs / chat / tasks / slides / keep /
  script / meet / forms) × read-only|read-write,
  **read-only by default** — ask the user to reconnect with the surface (or
  read-write) you need; don't start a login dance.
- If it **`401`s / "not logged in"**, the token is truly missing or lapsed (it's
  a ~1h token, no refresh) — *then* fall back to the OAuth-client login below, or
  ask the user to re-click "Connect Workspace".

**Do NOT trust `gws auth status` here.** With the env token it reports
`auth_method: none` / `credential_source: token_env_var` even though reads
succeed — a real read is the only reliable check. Skipping the probe and
jumping to `gws auth login` is the mistake to avoid: it drags the user through
a browser-redirect bridge that the already-present token makes unnecessary.

### Otherwise: configure an OAuth client

`gws` has **no embedded OAuth client**. `gws auth login` fails until one of
these is in place:

1. **`client_secret.json`** — drop an OAuth client secret file into the gws
   config dir. **Preferred in the sandbox** — see "Creating the OAuth client"
   below. Keep that dir on `/workspace` (next section) so it survives.
2. **Env vars** — set `GOOGLE_WORKSPACE_CLI_CLIENT_ID` and
   `GOOGLE_WORKSPACE_CLI_CLIENT_SECRET`.
3. **Service account** — set `GOOGLE_APPLICATION_CREDENTIALS=/path/to/key.json`.
   No `gws auth login` step is needed in this mode; calls authenticate
   directly with the key (use for fully unattended automation, no user present).
4. **`gws auth setup`** — provisions a GCP project + OAuth client for you, but
   renders a **full-screen interactive TUI** that can't be driven from
   `bash` (no keyboard input). Don't use it headless — create the
   `client_secret.json` by hand (option 1) instead.

### Creating the OAuth client (one-time)

When building the OAuth client yourself (via `gcloud` or the Cloud console):

- Choose the **"Desktop app"** client type, **not** "Web application". `gws
  auth login` listens on a random loopback port; a Web-app client rejects that
  with `Error 400: redirect_uri_mismatch`. Desktop-app clients accept loopback.
- For a corporate Workspace org, set the consent-screen audience to
  **Internal**. An internal-audience client clears the Context-Aware Access /
  "Account restricted" block that the default gcloud OAuth client trips on.
- The `client_secret.json`'s `project_id` must be a project the authenticating
  account may use — otherwise calls fail with the quota-project 403 (see
  Failure handling).

### Config dir + keyring (sandbox gotchas)

Set both on **every** `gws` command — each `bash` call is a fresh non-login
shell, so exports don't carry over between calls:

```
export GOOGLE_WORKSPACE_CLI_CONFIG_DIR=/workspace/lha/config/gws
export GOOGLE_WORKSPACE_CLI_KEYRING_BACKEND=file
```

- `GOOGLE_WORKSPACE_CLI_CONFIG_DIR` — relocates the config dir (which holds
  `client_secret.json`, the token, and the encryption key) onto `/workspace`.
  The default `~/.config/gws` is not migrated on runtime upgrades, so without
  this you'd redo the browser login after the next upgrade.
- `GOOGLE_WORKSPACE_CLI_KEYRING_BACKEND=file` — there's no OS keyring headless;
  the file backend writes the key into the config dir (hence also on
  `/workspace`).

### Authenticating

With an OAuth client configured, authenticate with:

```
bash(command="gws auth login --readonly")  # read-only scopes (start here)
bash(command="gws auth login")             # all default scopes
bash(command="gws auth login -s drive,gmail,sheets")  # limit the picker
bash(command="gws auth login --scopes <comma,separated,scopes>")
```

**Start with read-only scopes** (`--readonly`) — least privilege. Add write
scopes only when the task actually needs to write, by re-running `gws auth
login --scopes <…>` for the specific surface; Google's incremental consent
merges the new scope with what's already granted, so you never over-grant
up front.

`gws auth login` is **loopback-browser only** — it opens a browser and waits on
a local listener for the OAuth redirect. There is NO device-code /
paste-a-code flag.

**Completing the loopback flow headless.** It *does* work in the sandbox — you
bridge the redirect by hand. The browser opens on the *user's* machine, but the
listener is inside the container, so you relay the redirect URL across. The URL
appears in one turn and the user pastes back in a later turn, so use the
cross-turn process pipeline (`process(action='spawn', ...)` + `process`):

```
process(action='spawn', command="export GOOGLE_WORKSPACE_CLI_CONFIG_DIR=/workspace/lha/config/gws GOOGLE_WORKSPACE_CLI_KEYRING_BACKEND=file && gws auth login")
# read the printed auth URL, surface it VERBATIM to the user, then wait
process(action='poll', session_id=<id>)
```

1. The user opens the auth URL, consents, and their browser lands on a
   `http://localhost:<port>/?code=...&scope=...` page that **won't load** (the
   listener is in the sandbox, not on their machine). Ask them to copy that
   **full redirect URL** from the address bar and paste it back.
2. `curl` that exact URL from inside the sandbox to hand the code to the waiting
   listener, which completes the exchange and writes the token:
   ```
   bash(command="curl -s 'http://localhost:<port>/?code=...&scope=...'")
   ```
   The backgrounded `gws auth login` then finishes.

(`localhost` is on Layer A's allowlist, so this `curl` isn't gated.) For
**fully unattended** jobs where no user is present to paste the redirect, use a
service account instead — the loopback bridge needs a live user.

Check state any time with `bash(command="gws auth status")`.

## Command form

```
gws <service> <resource> [sub-resource] <method> [flags]
```

Query parameters are passed as **JSON** via `--params`; request bodies via
`--json`. Wrap both in single quotes so the shell preserves inner double
quotes:

```
gws drive files list --params '{"pageSize": 5, "q": "trashed=false"}'
gws drive files create --json '{"name": "notes.txt"}' --upload notes.txt
```

Several common operations also have ergonomic **helper commands** (prefixed
`+`) that take plain flags instead of JSON — e.g. `gws sheets +read`,
`gws sheets +append`, `gws gmail +send`, `gws drive +upload`.

Useful global flags: `--format json|table|yaml|csv` (json default),
`--dry-run` (validate without hitting the API), `--page-all` (auto-paginate
to NDJSON), `-o/--output <path>` (save binary responses),
`--sanitize <template>` (screen responses through Model Armor).

### Discovery — don't guess params

```
bash(command="gws drive --help")                 # resources + methods
bash(command="gws schema drive.files.list")      # params, types, defaults
```

`gws schema <service>.<resource>.<method>` prints the exact param shape for a
method. Run it (or `gws <service> --help`) whenever you're unsure of a flag —
it's authoritative for the installed version.

### Prefer the official per-surface skills for depth

The tables below are a quick reference, not the full surface. `gws` ships
**maintained per-surface skills upstream** — `gws-drive`, `gws-gmail`,
`gws-sheets`, `gws-calendar`, `gws-chat`, `gws-people`, `gws-slides`,
`gws-tasks`, plus curated recipes — that cover the less-common methods this file
doesn't. For anything beyond the common operations, install the relevant skill
into the workspace and read it (note the path the command prints):

```
bash(command="npx -y skills add https://github.com/googleworkspace/cli/tree/main/skills/gws-drive")
```

Swap `gws-drive` for the surface you need. These are version-pinned docs, so when
a skill and the installed binary disagree, `gws schema <service>.<resource>.<method>`
wins — it reflects the gws actually on PATH.

## Common operations

| You want to… | Command |
|---|---|
| List Drive files | `gws drive files list --params '{"q": "trashed=false", "pageSize": 20}'` |
| Get Drive file metadata | `gws drive files get --params '{"fileId": "ID", "fields": "id,name,mimeType"}'` |
| Download file contents | `gws drive files get --params '{"fileId": "ID", "alt": "media"}' -o out.bin` |
| Export a Doc/Sheet | `gws drive files export --params '{"fileId": "ID", "mimeType": "text/plain"}' -o out.txt` |
| Read a Sheet range | `gws sheets +read --spreadsheet ID --range "Sheet1!A1:D10"` |
| Append a Sheet row | `gws sheets +append --spreadsheet ID --range Sheet1 --json '{"values": [["a","b"]]}'` |
| List Gmail messages | `gws gmail messages list --params '{"q": "from:alice@example.com newer_than:7d", "maxResults": 10}'` |
| Get a Gmail message | `gws gmail messages get --params '{"id": "MSG_ID", "format": "full"}'` |
| Send mail | `gws gmail +send --to alice@example.com --subject 'Hi' --body 'Hello!'` |
| List Calendar events | `gws calendar events list --params '{"calendarId": "primary", "maxResults": 10}'` |
| List Tasks task lists | `gws tasks tasklists list` |
| List tasks in a list | `gws tasks tasks list --params '{"tasklist": "TASKLIST_ID"}'` |
| Add a task | `gws tasks tasks insert --params '{"tasklist": "TASKLIST_ID"}' --json '{"title": "Buy milk"}'` |
| Read a presentation | `gws slides presentations get --params '{"presentationId": "ID"}'` |
| List Keep notes | `gws keep notes list` |
| Get a Form + its responses | `gws forms forms get --params '{"formId": "ID"}'` · `gws forms forms responses list --params '{"formId": "ID"}'` |

Confirm the exact params with `gws schema …` before write/delete calls — the
shapes above are the common cases, not an exhaustive contract.

**Keep caveat:** the Google Keep API is restricted to Workspace Enterprise
domains via service-account domain-wide delegation — an ordinary
Connect-Workspace access token will likely `403` on `gws keep …` regardless of
the granted scope. If it does, tell the user it's an API restriction, not a
missing grant.

**Under a routine, the Google token is present only if you declared its secret
name (`GOOGLE_WORKSPACE_CLI_TOKEN`) in the routine's `secrets:`.**

### Google Chat — unread state & sender names

Two Chat limitations are scope-bound, so check before promising the user a
result:

- **"Unread messages" needs an extra scope, but `gws` has a native command for
  it.** The default read scopes (`chat.spaces.readonly` +
  `chat.messages.readonly`) let you list spaces and read messages but do **not**
  expose per-space unread state — read-position needs
  `chat.users.readstate.readonly`. With that scope, **don't hand-roll REST**:
  `gws` wraps the read-state endpoint as
  `gws chat users spaces getSpaceReadState`. Compute unread per space yourself —
  there's no single "list unread" call:
  1. List the spaces you care about (DMs/group chats sort by `lastActiveTime`):
     ```
     gws chat spaces list --params '{"pageSize": 1000}'
     ```
  2. For each space, read your last-read mark and its latest message, then
     compare:
     ```
     gws chat users spaces getSpaceReadState --params '{"name": "users/me/spaces/<space>/spaceReadState"}'
     gws chat spaces messages list --params '{"parent": "spaces/<space>", "pageSize": 1, "orderBy": "create_time DESC"}'
     ```
     A space is **unread** when the latest message's `createTime` is strictly
     newer than the read state's `lastReadTime` **and** that message's
     `sender.name` (a `users/<id>`) isn't you. (`orderBy` is `create_time DESC` —
     snake_case field + `ASC`/`DESC`, not `createTime desc`.)
  - Resolving **your own** `users/<id>` to skip self-authored messages is the one
    snag: `people/me` 403s under the Connect-Workspace token, so pass your id in
    explicitly (read it off any message you sent, or `gws people people get`
    once) rather than relying on `me`.
  - Resolve the remaining sender ids to names with `gws people people getBatchGet`
    (see the People note above).
  - **Without the read-state scope** you can't get true unread — be upfront, then
    either re-auth to add it
    (`gws auth login --scopes https://www.googleapis.com/auth/chat.users.readstate.readonly`;
    incremental consent merges it) or fall back to **recency as a proxy** (list
    spaces by `lastActiveTime`, pull each space's latest messages). Say which.
- **Senders come back as user IDs, not names.** Chat returns each sender as
  `users/<numeric-id>`; the Chat read scopes don't resolve those to display
  names. The People API does (`gws` wraps it as `gws people …`), and "Connect
  Workspace" now offers the grant as a separate **Directory** surface (scope
  `directory.readonly` — read-only regardless of the read-write toggle). To turn
  IDs into names:
  - If a `gws people` read 403s, the user hasn't ticked **Directory** — ask them
    to reconnect with it.
  - **Resolve in batch — a Chat thread has many distinct senders, so collect all
    the `<numeric-id>`s from `users/<numeric-id>` and resolve them in one
    `getBatchGet` call, not one `get` per ID** (the People resource id is the
    `<numeric-id>`):
    ```
    gws people people getBatchGet --params '{"resourceNames": ["people/<id1>", "people/<id2>"], "personFields": "names,emailAddresses"}'
    ```
    Read each name off `responses[].person.names[0].displayName`. Use single-ID
    `gws people people get --params '{"resourceName": "people/<numeric-id>", "personFields": "names"}'`
    only for a one-off lookup. If a sender can't be seen as a domain member, fall
    back to `gws people people searchDirectoryPeople --params '{"query": "<name-or-email>", "readMask": "names,emailAddresses"}'`.
  - Confirm exact param shapes with `gws schema people.people.get` before relying
    on them.

## Failure handling

- `No OAuth client configured` → set up credentials first (see Auth: drop a
  `client_secret.json`, set the `GOOGLE_WORKSPACE_CLI_CLIENT_*` env vars, or use
  a service account).
- `gws auth login` hangs on the listener → expected headless; bridge the
  loopback by relaying the redirect URL and `curl`-ing it back in (see Auth →
  "Completing the loopback flow headless"). Only fall back to a service account
  when no user is present to paste the redirect.
- Login token doesn't persist / "no keyring" error, or asked to log in again
  after a runtime upgrade → set both `GOOGLE_WORKSPACE_CLI_CONFIG_DIR=/workspace/lha/config/gws`
  and `GOOGLE_WORKSPACE_CLI_KEYRING_BACKEND=file` on every `gws` call (config +
  key then live on `/workspace`, which persists; `~/.config/gws` does not).
- `Error 400: redirect_uri_mismatch` on login → the OAuth client is a
  "Web application" type; recreate it as **Desktop app** (loopback ports).
- `access_denied` / "Account restricted" on consent → Context-Aware Access on
  the OAuth client. Use an **Internal**-audience client (see Auth → "Creating
  the OAuth client").
- 401 / `not logged in` → if `GOOGLE_WORKSPACE_CLI_TOKEN` is the auth source,
  the ~1h token has lapsed — ask the user to re-click "Connect Workspace" (it
  has no refresh). Otherwise the OAuth login expired; re-run `gws auth login`.
- `gws auth status` shows `auth_method: none` but reads work → expected when
  the env token (`GOOGLE_WORKSPACE_CLI_TOKEN`) is the source; `auth status`
  doesn't reflect it. Confirm with a real read, not `auth status`.
- **403, scope too narrow** → the granted scopes don't cover the call. If the
  auth source is the `GOOGLE_WORKSPACE_CLI_TOKEN` env token, the "Connect
  Workspace" grant was per-surface and read-only by default — ask the user to
  reconnect including the surface (or read-write) you need. On the OAuth-login
  path instead, re-auth with the needed scope: `gws auth login --scopes <…>`
  (Google's incremental consent merges it with what was already granted).
- **403, `serviceUsageConsumer` / quota project** → distinct from a scope 403:
  the `client_secret.json`'s `project_id` is a project the account can't bill
  to. Point the client at a project the account may use (the account needs
  `roles/serviceusage.serviceUsageConsumer` on it).
- `gws: command not found` → not installed. `npm install -g @googleworkspace/cli`.
- Unsure of a flag or param → `gws <service> --help` or
  `gws schema <service>.<resource>.<method>`. Don't guess.

<!-- find-skills:tested:false -->
