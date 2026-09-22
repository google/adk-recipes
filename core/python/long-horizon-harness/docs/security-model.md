# Security model — the layered adoption ladder

Long Horizon trusts the LLM within guardrails: the agent can do anything its tools
allow, and the boundaries are enforced at the tool and sandbox level, not by
asking the model to police itself. That makes adoption a question of *which
layers you turn on* — each one mounts new surface and widens the attack surface
you accept in exchange.

This doc is the layer-by-layer ladder. It covers what stays on at every layer,
the identity contract you choose with `LHA_AUTH_MODE`, the routes the app mounts
and what each exposes, the always-on guard chain, and an L1–L6 adoption ladder
with the trade-off each step accepts.

**Who this is for:** anyone deciding how much of Horizon to turn on — a
developer embedding the agent, an operator wiring a deploy, or a reviewer
modelling the attack surface. Read [The routes](#the-routes-start-here)
first if you just want endpoints; read [Threat model](#threat-model) if you're
drawing a security boundary.

If anything here disagrees with the code, trust the code — these are the files:
`horizon/auth/identity.py` (auth), `horizon/fast_api_app.py` (route mounting),
`horizon/secrets/inject.py` (secret injection), `horizon/agent.py` (always-on
plugins/callbacks).

## In this doc

- [The routes (start here)](#the-routes-start-here) — what the app mounts and what each exposes.
- [Always-on invariants](#always-on-invariants) — the floor you can't toggle off.
- [Two layers: hard deny vs. soft ask](#two-layers-hard-deny-vs-soft-ask) — Layer A–D guard chain + grants.
- [Identity: `LHA_AUTH_MODE`](#identity-lha_auth_mode-read-this-first) — who a request is (read this first).
- [What the routes expose](#what-the-routes-expose) — the credential-injecting and unattended-run routes.
- [Adoption ladder (L1–L6)](#adoption-ladder-l1l6) — each rung + what you accept by enabling it.
- [Quick reference](#quick-reference) — token/layer → env var.
- [Security triage](#security-triage) — symptom → which layer → why.
- [Threat model](#threat-model) — components, trust boundaries, threats.
- [Where to go next](#where-to-go-next) — related docs.

---

## The routes (start here)

The served app (`horizon.fast_api_app:app`) mounts **every** router — there's no
preset knob. To ship a subset, delete the `attach_*` calls you don't want in
`horizon/fast_api_app.py`. The security question isn't *which* routes mount but
*what each one exposes*:

| Route group | Endpoints | What it exposes |
|---|---|---|
| core — A2A + agent card | JSON-RPC, `/.well-known/agent-card.json` | drive the agent |
| core — sessions/state/tasks | `/lha/sessions`, `/lha/state`, `/lha/contexts/{id}/tasks` | per-user chat history + state |
| memories | `/lha/memories` | cross-session memory listing |
| uploads | `/lha/uploads`, `/lha/workspace/*` | read/write the user's workspace |
| sandbox | `/lha/sandbox/warm`, `/status` | warm/status of the per-user sandbox |
| processes | `GET`/`DELETE /lha/processes` | list, and kill, the user's running sandbox processes |
| feedback | `/feedback` | user feedback sink (your Cloud Logging) |
| secrets | `/lha/secrets*` | store/manage the per-user API keys injected into commands |
| oauth | `/lha/gcp/*` | Connect-Google flow → tokens injected into commands |
| scheduler | `/lha/routines`, `/scheduler/*` | unattended runs (routines, dream-review, snapshot) |

> **Credential injection is independent of the routes.** Stored secrets and OAuth
> tokens are injected into sandbox commands by `secret_env()` **whenever they
> exist** — deleting the `/lha/secrets` or `/lha/gcp` route removes the *management*
> API, not the injection. To disable injection entirely, don't store the
> credentials (or edit `secret_env()`). The runtime boundaries (exfil guard,
> per-user isolation, routine secret-scoping) are what actually contain a
> credential once it's in the environment.

The rest of this doc explains what each route *costs* you — read on if you're
deciding where to draw the security boundary, not just which endpoints you want.

---

## Always-on invariants

These are present at **every** layer — they ride the agent and the FastAPI
surface regardless of which routes mount, even at L1 with no HTTP at all (the callback-chain
ones run inside the `Runner`; `IdentityMiddleware` only exists once you mount
FastAPI):

| Invariant | Where | What it does |
|---|---|---|
| `exfil_guard` (Layer A) | `before_tool_callback` (`horizon/guardrails/exfil_guard.py`) | Blocks outbound tool calls that carry secret material or upload a credential file, and routes uploads to non-allowlisted hosts through a one-time `/grant`. Always on, every agent. |
| `IterationBudgetPlugin` | `App(plugins=…)` (`horizon/conversation/iteration_budget_plugin.py`) | Caps tool calls per iteration so a runaway turn can't spend unbounded tokens. |
| `GuardrailsPlugin` | `App(plugins=…)` (`horizon/guardrails/guardrails_plugin.py`) | No-progress, repeated-failure, and halt guards sharing one `halt_reason`; short-circuits a stuck turn. |
| `IdentityMiddleware` | `_build_app` (`horizon/auth/identity.py`) | Resolves the request's `user_id` once per request per `LHA_AUTH_MODE`, before any route runs. Added unconditionally whenever the FastAPI surface is built. |

You cannot toggle these off. They are the floor.

---

## Two layers: hard deny vs. soft ask

The guards above (`exfil_guard`, `policies_guard`) are a
**hard-deny** layer (Layer C) — they block *dangerous* tool calls (secret exfil,
a bad egress host, a policy break) and the model cannot override them through
any in-band flow. Sitting **below** them is a second, **soft** layer (Layer D):
the **permission gate** (`permission_guard`, a `before_tool_callback` that runs
*last* in the chain). It asks "is this call *consequential* enough to confirm?"
— and on a call the hard-deny layer already permitted, it pauses for an
interactive four-button approval (run once / this session / always-save /
decline). A call must clear the hard-deny floor first; the ask-layer never
reopens something the security guards blocked. Full detail in
[`docs/permission-model.md`](permission-model.md).

### Command-safety argv classification (demotable verdicts)

Before the policy overlay/seed rules run, **`command_safety.py`** lexes shell
commands into argv tokens (quote- and operator-aware via stdlib `shlex`) and
inspects structure instead of pattern-matching raw strings. It returns a
**verdict**: `"deny"` (catastrophic — always blocks), `"ask"` (risky — blocks
on child/headless chains via `ask_is_deny=True`, **demotes to an interactive
approval** on the root chain), or `None` (no opinion).

- **"deny"** — `rm -rf /`, `rm -rf /etc`, `rm -rf $HOME`, `rm -rf /*`, etc.
  (recursive force-delete of system/home roots or their subdirectories).
- **"ask"** — the genuinely-scary shell ops: destructive deletes (`rm -rf .`/`*`,
  `find . -delete`, `mv … /dev/null`, and cloud/infra deletes `bq rm`,
  `gcloud … delete`, `kubectl delete`, `terraform destroy`, `docker rm`/`rmi`/
  `prune`, `gsutil rm`, `gws … delete`); force/history git (`git push --force`/
  `--delete`/`--mirror`, `git reset --hard`, `git clean -f`, `git filter-branch`);
  `sudo`/`su`; pipe into an interpreter (`… | sh|bash|python|perl|ruby|node|php`);
  recursive `chmod`/`chown` on a system/home root. (Ordinary `git commit`,
  `git push`, `npm install`, `rm file.txt`, `rm -rf build/`, recursive chmod on a
  local path — **no opinion**, they run.)

The permission seed (`permission_rules.py`) ships **shell allow** (`bash`/
`process`), so `command_safety` is the gate that turns an ordinary shell allow
into a prompt. A `"ask"` verdict is **demotable by an explicit grant/overlay**:
the `_shell_decision` demotion is skipped when the matched allow's source is
`grant`/`overlay`, so "approve for this session/always" sticks. `"deny"` and the
policy seed are not demotable (enforced in `policies_guard`, Layer C).

On an `"ask"` verdict, the **root agent** sees an interactive approval card.
A **blocking `delegate` child** *resurfaces* the approval to the live user (see
below). **Background `agent` / routine (headless)** children
(`ask_is_deny=True` in `policies_guard`) treat it as a hard deny (no regression
in unattended contexts). This replaces the fragile
substring/regex rules that shipped in older seeds — the new seed
(`default_policies.jsonl`) only carries literal catastrophic commands +
credential reads.

### Child permission resurfacing (blocking `delegate`)

A `delegate` child runs in its own runner, so historically an `ask_user` inside
it became a hard deny. Now a **blocking `delegate`** drives a **durable,
resumable** child (reusing the parent's session service) and surfaces the
child's risky ops to the user: the child guard
(`horizon/subagents/child_guard.py`) resolves the full permission ruleset and,
when a bubble is available (`resurface_context`), pauses the child via
`request_confirmation`; `delegate` re-raises that on the **parent turn** tagged
`[delegated: <name>]` and, on approval, resumes the child **in place** (ADK
resumability guarantees prior completed ops are not re-run). On approval a
session grant is written so the child's later same-shape ops auto-allow.
Because the child self-gates each risky op, `delegate` is **exempt from the
spawn-time prompt** (`SUBAGENT_TOOLS` in `permission_guard`). Background `agent`
is exempt too: spawning grants the child nothing it couldn't already do (its
headless guard hard-denies risky ops and exfil/egress hard-block), so the
spawn-time prompt was pure friction.

**Constraint:** ADK allows one interactive confirmation per tool call, so a
single `delegate` surfaces **one** approval; a second, *different* approval in
the same run is denied with guidance (split the task / pre-grant). Background
`agent` cannot resurface (no live turn): its risky ops hard-deny headlessly and
surface in the child's reported result rather than as an interactive prompt.

### Approval modes (per-session, root agent only)

Session state key `approval_mode` (default `"default"` | `"yolo"`) toggles via
`/yolo`. YOLO mode auto-approves the Layer-D interactive ask ONLY; it does NOT
bypass exfil/egress/Layer-C deny rules. The model never sees the mode — it's a
UI/backend shortcut.

### Tool-narrowing enforcement (overlay/grant rules)

Permission rules in `.lha/permissions.jsonl` or granted via the interactive
approval card may include a `commandPrefix`, `commandRegex`, or `argsPattern` to
narrow blanket `allow` rules. **Overlay and grant rules** (source = `"overlay"`
or `"grant"`) that target `bash` or `process` but carry no such narrowing
field are **rejected** at load time — you cannot grant a blanket "always allow
bash" from the overlay or a session approval; only the default seed may
carry that. This prevents accidental over-granting.

### Regex-safety guard (tenant-authored patterns only)

Tenant-authored regexes in `.lha/{permissions,policies,exfil}.jsonl` are
validated (`horizon/guardrails/_regex_safety.py`) before compiling: length cap
(1,000 chars) + nested-quantifier pattern detection. Malformed regexes are
skipped with a warning. **Trusted defaults and seed rules are NOT gated** —
only user-authored overlay/grant patterns.

---

## Identity: `LHA_AUTH_MODE` (read this first)

Every authenticated route derives its `user_id` from `IdentityMiddleware`, which
resolves identity by `LHA_AUTH_MODE`. This is the single most security-relevant
setting — it decides *who* a request is.

| Mode | Header consumed | Verification | `user_id` source | Intended deployment |
|---|---|---|---|---|
| `dev` (default) | — (none) | **None** | `LHA_DEV_USER_ID` (default `dev@local`) | Local development only |
| `iap` | `X-Goog-IAP-JWT-Assertion` or `X-LHA-IAP-Assertion` (proxy-forwarded); else `Authorization: Bearer` (GE) | IAP JWT verified vs `LHA_IAP_AUDIENCE` (Google public keys); or OAuth access token verified vs `LHA_GCP_OAUTH_CLIENT_ID` via `tokeninfo` | Verified `email` | Cloud Run behind an IAP proxy and/or Gemini Enterprise |
| `trusted_header` | `X-LHA-User-Id` | None on the header — trust is delegated to Cloud Run IAM (only the web-frontend SA may invoke) | Header value, verbatim | Backend behind a separate web/IAP frontend |

> [!CAUTION]
> **`dev` is NO AUTH.** Every request collapses to a single identity
> (`LHA_DEV_USER_ID`) with no header, no verification, no per-user isolation at
> the HTTP boundary. It is safe locally and a full cross-user breach if a
> deployment forgets to set the mode.
>
> A backstop catches the worst case: `dev` mode **refuses to run when
> `K_SERVICE` is set** (Cloud Run injects `K_SERVICE`), raising
> `DevAuthInProductionError` → HTTP 500. This fails closed instead of silently
> serving every user as one identity. **Do not disable it.** For any deployed
> environment set `LHA_AUTH_MODE=iap` or `trusted_header`.

`iap` requires `LHA_IAP_AUDIENCE` — the Cloud Run audience
`/projects/PROJECT_NUMBER/locations/REGION/services/SERVICE_NAME`; a missing
audience is a 500, an unknown `LHA_AUTH_MODE` value is a 500, a missing/invalid
header is a 401.

**Two credential shapes in `iap` mode (no plaintext-trusted identity).** A
backend that sits behind a web/IAP proxy never receives the native
`X-Goog-IAP-JWT-Assertion` — Google strips `X-Goog-*` headers inbound to a
non-IAP service. The `lha-web` proxy therefore verifies the IAP JWT itself and
**forwards it under the custom header `X-LHA-IAP-Assertion`**, which the backend
re-verifies (signature + audience) — strictly stronger than the old
`trusted_header` plaintext re-assertion. Separately, **Gemini Enterprise**
invokes `/a2a` with the Discovery Engine SA in `X-Serverless-Authorization`
(Cloud Run edge IAM) and the **end-user's OAuth access token in
`Authorization: Bearer`**; with no IAP JWT present, the backend verifies that
token via Google `tokeninfo`, requiring `aud == LHA_GCP_OAUTH_CLIENT_ID` and a
verified email (`horizon/auth/oauth_verify.py`). The same token is overlaid as
`CLOUDSDK_AUTH_ACCESS_TOKEN` for that turn so the sandbox acts as the user
(delegated Google access). GE registration wires this via
`authorizationConfig.agentAuthorization` (a Discovery Engine **authorization**
resource holding the OAuth client + scopes); the A2A agent card requires no
`securityScheme`.

**Unauthenticated paths** (skip identity resolution entirely): `/`,
`/.well-known/agent-card.json`, and the prefixes `/_gcp_iap/`, `/openapi`,
`/docs`, `/redoc`, `/scheduler/`. `OPTIONS` preflight always passes. The
`/scheduler/*` routes are public to `IdentityMiddleware` because they verify
Cloud Scheduler's own OIDC token and iterate stored users themselves rather than
operating on a request-bound user.

---

## What the routes expose

All routers mount; three carry more than an HTTP endpoint and are the ones to
reason about (delete their `attach_*` call in `horizon/fast_api_app.py` to drop
the route — but see the injection caveat).

### Credential-injecting routes (`secrets`, `oauth`)

| Route | Endpoints | What it manages |
|---|---|---|
| secrets | `/lha/secrets` (GET / PUT / DELETE `{name}`, POST `/secrets/import`) | the per-user API keys `secret_env()` injects into sandbox commands |
| oauth | `/lha/gcp/*` (`/connect`, `/callback`, `/status`, `/disconnect`) | the Connect-Google flow storing the 5 Google OAuth tokens (`CLOUDSDK_AUTH_ACCESS_TOKEN`, `CLOUDSDK_AUTH_TOKEN_EXPIRES_AT`, `GOOGLE_WORKSPACE_CLI_TOKEN`, `GOOGLE_WORKSPACE_TOKEN_EXPIRES_AT`, `GOOGLE_WORKSPACE_SCOPES_META`) `secret_env()` injects |

`secret_env()` (`horizon/secrets/inject.py`) is the single injection point — it
returns every stored secret for the request's user (routines narrow this to their
declared names). **Injection happens whenever a credential is stored, regardless
of whether these routes are mounted** — the routes are only the management API. To
disable injection, don't store the credentials (or edit `secret_env()`). The model
never sees a value, only the name; what a command does with a token once it's in
the environment is outside the boundary (see the threat model).

### Unattended-run routes (`scheduler`)

| Route | Endpoints | What it enables |
|---|---|---|
| scheduler | `/lha/routines` (list/delete) + `/scheduler/*` (`routine-tick`, `dream-review`, `snapshot`) | turns that run with **no human present** — routines firing as real chats, the nightly dream-review, the snapshot job |

The `/scheduler/*` endpoints bypass `IdentityMiddleware` and are gated by Cloud
Scheduler's own OIDC token (`LHA_SCHEDULER_SA` / `LHA_SCHEDULER_AUDIENCE`); they
iterate stored users themselves. A fired turn wields the full toolset and acts as
the owner — see the L3 rung and TB7 in the threat model.

### The rest

`memories`, `uploads`, `sandbox`, `feedback` are plain HTTP surface (memory
listing, workspace read/write, sandbox warm/status, feedback sink) with no
data-plane weight. Core (A2A + agent card, `/lha/sessions`, `/lha/state`,
`/lha/contexts/{id}/tasks`) is what makes it an agent at all.

---

## Adoption ladder (L1–L6)

Each rung adds capability and accepts new attack surface. Climb only as far as
your use case needs.

### L1 — bare agent, no HTTP
`horizon.fast_api_app.build_runner()`. You drive the `Runner` directly (CLI /
batch / embedding in another framework).
- **Adds:** the agent loop with all the always-on invariants (exfil guard,
  iteration budget, guardrails). No network surface at all.
- **You accept:** whatever your host process can do — there is no identity
  boundary because there is no HTTP. The agent runs as you.

### L2 — FastAPI surface
`uvicorn horizon.fast_api_app:app`. Mounts A2A + all `/lha/*` + `/scheduler/*`.
Pick `LHA_AUTH_MODE` here.
- **Adds:** the HTTP API and per-request identity. Every route mounts (trim by
  deleting `attach_*` calls — see [The routes](#the-routes-start-here)).
- **You accept:** the auth contract you chose. `dev` means no auth — fine
  locally, never deployed. Stored secrets/OAuth tokens inject into commands
  whenever present (see [What the routes expose](#what-the-routes-expose)).

### L3 — scheduler / unattended execution
The `/scheduler/*` routes always mount; wire Cloud Scheduler OIDC (`LHA_SCHEDULER_SA` /
`LHA_SCHEDULER_AUDIENCE`) and install `lha[scheduler]` for durable storage. Cloud
Scheduler calls `/scheduler/routine-tick`, `/scheduler/dream-review`, `/scheduler/snapshot`.
- **Adds:** routines that fire as real chats, the nightly dream-review, and the
  snapshot job — turns that run with **no human watching**.
- **You accept:** unattended execution. A routine turn has the agent's full
  toolset and acts as the routine's owner. The `/scheduler/*` routes bypass
  `IdentityMiddleware` and are instead gated by Cloud Scheduler's OIDC token —
  make sure that verification (`LHA_SCHEDULER_SA` / `LHA_SCHEDULER_AUDIENCE`) is
  configured, since these endpoints can trigger agent runs.

### L4 — sandbox backend
`LHA_ENVIRONMENT_BACKEND=sandbox` (or `sandbox="sandbox"` in `create`). Tools run
in a per-user Sandbox container instead of on the host.
- **Adds:** per-user isolation — one user's tools can't reach another's
  workspace; the host machine is no longer the blast radius. Outbound internet
  is **hermetic when deployed**: the sandbox is provisioned from an internet-off
  Vertex template, a kernel/network-level wall (not a heuristic).
  `LHA_SANDBOX_INTERNET_ACCESS` is the only knob — unset it defaults to **off on
  Cloud Run** (`K_SERVICE` set) and **on in local dev**, so `make dev` works out
  of the box while deployments stay default-deny. Vertex bakes egress into the
  sandbox at create time, so there is no live toggle and no in-session override:
  a change takes effect on the next sandbox provision. Egress is all-or-nothing
  (the SDK's `egress_control_config` exposes only `internet_access`; no host
  allowlist). The same default governs both the **interactive user sandbox** and
  **routine** sandboxes (isolated `lhart-*`, secret-scoped): routines are
  unattended (no human to catch anomalies), so they inherit it rather than being
  open by default. A routine that needs the network is enabled deliberately, and
  its declared-secrets scope plus Layer A remain the boundary.
- **You accept:** once egress is enabled, open outbound internet from the
  sandbox. Layer A (`exfil_guard`) remains the exfiltration boundary (secret
  material, credential reads, metadata-server access, and upload-shaped commands
  to non-allowlisted hosts) whether or not egress is on.

### L5 — web UI + Express/IAP proxy
The Vite SPA served by the `web/server/` Express proxy, behind IAP, talking to
the backend with `LHA_AUTH_MODE=iap`.
- **Adds:** browser auth (IAP validates the user) and a re-verified
  identity-forwarding contract. Google strips the native
  `X-Goog-IAP-JWT-Assertion` inbound to a non-IAP backend, so the proxy forwards
  the (already-verified) IAP JWT under the custom header `X-LHA-IAP-Assertion`,
  which the backend re-verifies itself (signature + audience vs
  `LHA_IAP_AUDIENCE`). No plaintext identity header is trusted.
- **You accept:** the proxy is in the trusted computing base for *transport* — it
  mints the backend ID token and forwards the assertion — but the backend
  independently re-verifies the Google-signed JWT, so a forged identity needs a
  forged JWT, not just a set header. Cloud Run IAM still restricts backend
  invocation to the web frontend SA (plus the scheduler SA and A2A peers).
  `trusted_header` (verbatim `X-LHA-User-Id`) remains an available mode for a
  frontend that fully owns identity, but the reference deploy does **not** use it
  — `iap` is strictly stronger than a plaintext re-assertion.

### L6 — terraform reference deploy
`terraform/` — two Cloud Run services (`lha` backend SA-invoker-only, `lha-web`
IAP-fronted proxy), Cloud SQL, Cloud Scheduler, IAM, secrets.
- **Adds:** the full reference topology with the L3–L5 contracts wired:
  backend `LHA_AUTH_MODE=iap` + `LHA_IAP_AUDIENCE`, `iap_enabled=false` on the
  backend (IAM-gated, invoked only by the web/scheduler SAs + A2A peers),
  `iap_enabled=true` on `lha-web`, scheduler OIDC.
- **You accept:** the operational surface of a production deploy. Every route is
  mounted (it demos secrets/oauth/scheduler); trim `horizon/fast_api_app.py` for a
  tighter footprint.

---

## Quick reference

| Token / layer | Tier | Env / knob |
|---|---|---|
| Identity | — (always) | `LHA_AUTH_MODE` (`dev` / `iap` / `trusted_header`), `LHA_DEV_USER_ID`, `LHA_IAP_AUDIENCE` |
| Routes | — | all mount; trim by deleting `attach_*` in `horizon/fast_api_app.py` |
| `secrets` | route + injection | `/lha/secrets`; injection always-on when a key is stored |
| `oauth` | route + injection | `/lha/gcp/*`; OAuth client via `LHA_GCP_OAUTH_*` |
| `scheduler` | unattended | `/scheduler/*`; OIDC via `LHA_SCHEDULER_SA` / `LHA_SCHEDULER_AUDIENCE` |
| `memories` / `uploads` / `feedback` / `sandbox` | route only | always mounted |
| Sandbox backend | runtime | `LHA_ENVIRONMENT_BACKEND` (`local` / `sandbox`) |


---

## Security triage

When a call is blocked, a route 404s, or identity looks wrong, start here. The
guard layers run in order (A → C → D) in `before_tool_callback`; the
first one to object is the one in the error.

| Symptom | Where to look | Why |
|---|---|---|
| Tool blocked: `exfiltration guard: …` | Layer A — `exfil_guard` (`horizon/guardrails/exfil_guard.py`) | Args carry secret material, or a shell command reads a credential / hits the GCP metadata server / uploads to a non-allowlisted host. A hard block needs an exact `/grant`; an upload to an unknown host needs `/grant` or an `allow_hosts` overlay. |
| Tool blocked: `blocked by policy` / `blocked by command_safety` | Layer C — `policies_guard` (`horizon/guardrails/policies.py` + `command_safety.py`) | A seed/overlay policy matched, or `command_safety.classify()` returned a `deny` verdict (e.g. `rm -rf /`). On a child/headless chain an `ask` verdict also hard-denies (`ask_is_deny`). |
| Tool blocked: `denied by permission rule`, or it pauses for a four-button approval card | Layer D — `permission_guard` (`horizon/guardrails/permission_guard.py`) | A permission rule said `deny`; otherwise the call cleared the hard-deny floor but is consequential (`ask` verdict / no allow rule) and prompts. Approve, or set `/yolo` for the session. |
| `401`/`403` from the sandbox shim | Per-user JWT + routing token at the Sandbox LB (`horizon/sandbox/lifecycle.py`, `environment.py`) | Bearer JWT or `X-Sandbox-Routing-Token` expired (env re-mints next turn), or the caller's ADC lacks `roles/iam.serviceAccountTokenCreator`. |
| Secret not injected into a command | `secret_env()` (`horizon/secrets/inject.py`) | The credential isn't stored for this user, the local backend resolved no `owner`, or a routine scope filtered it to the manifest's declared names only. |
| Wrong / shared user identity | `LHA_AUTH_MODE` (`horizon/auth/identity.py`) | `dev` collapses every request to `LHA_DEV_USER_ID`. Set `iap` or `trusted_header` for any deploy. |
| `500` `LHA_AUTH_MODE=dev refused…` | `DevAuthInProductionError` (`horizon/auth/identity.py`) | `dev` mode on Cloud Run (`K_SERVICE` is set) — fail-closed backstop. Set a real auth mode. |
| `401` `invalid IAP JWT` / `invalid OAuth bearer` | `iap` verification (`identity.py`, `auth/oauth_verify.py`) | Wrong `LHA_IAP_AUDIENCE`, or a GE bearer whose `aud != LHA_GCP_OAUTH_CLIENT_ID`. |
| Headless run auto-denied (`headless_denied`) | Headless mode (`permission_guard.py:set_headless_mode`) | A non-shell op needed approval with no user present (routine fire path). Redesign the routine to avoid it. |
| Route `404` that should exist | Route mounting (`horizon/fast_api_app.py`) | Its `attach_*` call was removed. Confirm with `{r.path for r in app.routes}`. |

---

## Threat model

> **Disclaimer:** This is a practical, developer-facing model of where Horizon
> places trust and where its boundaries are — not an authoritative security
> audit. It is deliberately honest that several controls are *soft* or
> *heuristic* (the exfil guard matches command shapes, the permission ask-layer is demotable). Validate against
> the cited code before relying on any row, and prefer the hard boundaries
> (per-user identity, sandbox isolation) over the defense-in-depth heuristics.

Scope: the agent + FastAPI surface in this repo (`horizon/`). Out of scope: the
LLM's own behavior (jailbreaks), Agent Platform Sessions / Sandboxes / Cloud Run / IAP
internals, and anything an operator misconfigures away (e.g. `dev` auth in
prod, which the `K_SERVICE` backstop turns into a fail-closed 500).

### Components

| ID | Component | Trust level | Default | Entry point |
|---|---|---|---|---|
| C1 | Identity middleware (per-request `user_id`) | framework | on with FastAPI | `horizon/auth/identity.py` (`IdentityMiddleware`, `resolve_user_id`) |
| C2 | Layer A — exfil guard | framework | always on | `horizon/guardrails/exfil_guard.py` (+ `exfil_config.py`) |
| C3 | Layer C — policies guard + argv classifier | framework | always on | `horizon/guardrails/policies.py`, `command_safety.py` |
| C5 | Layer D — permission ask-gate | framework | always on | `horizon/guardrails/permission_guard.py`, `permission_rules.py` |
| C6 | Halt / budget plugins | framework | always on | `horizon/guardrails/guardrails_plugin.py`, `horizon/conversation/iteration_budget_plugin.py` |
| C7 | Sandbox per-user JWT isolation | framework | on when `backend=sandbox` | `horizon/sandbox/lifecycle.py` (`mint_sandbox_token`), `environment.py` |
| C8 | Per-user secret store + injection point | framework | on when a key is stored | `horizon/secrets/store.py`, `inject.py` (`secret_env`) |
| C9 | A2A converter (URL redaction + fake-link strip) | framework | on (A2A path) | `horizon/a2a/executor.py`, `horizon/context/artifact_url_redaction.py` |
| C10 | Memory namespace (per-user scope) | framework | always on | `horizon/memory/user_profile.py`, `infrastructure/memory_config.py` (`profile_scope`) |
| C11 | Headless / routine run isolation | framework | on (scheduler) | `horizon/guardrails/permission_guard.py` (`set_headless_mode`), `secrets/inject.py` (`scoped_secret_env`) |

### Trust boundaries

| ID | Boundary | Controlled inside | NOT controlled (outside) |
|---|---|---|---|
| TB1 | HTTP request → identity | `user_id` resolution per `LHA_AUTH_MODE`; JWT/bearer re-verification; `dev` fail-closed on Cloud Run | The header/token *content* (dev mode verifies nothing and collapses to one identity) |
| TB2 | Model output → tool execution | The A–D guard chain: secret exfil, bad egress, policy/argv deny, interactive ask | The model's reasoning and what the user chooses to approve |
| TB3 | Sandbox shell → external network | Always-on `exfil_guard` (secret/credential/upload shapes) | Arbitrary outbound once a command evades the heuristics **and egress is enabled** (hermetic by default when deployed — see L4); `exfil_guard` is the exfiltration boundary either way |
| TB4 | Web / tool content → agent context | Artifact-URL redaction in the model's view; stripping fabricated `attachment:`/`sandbox:` links | Fetched web/search/tool *content*, treated as data the model may follow (prompt injection) |
| TB5 | Secret store → sandbox env | Per-user `owner` scoping + routine scope filter in `secret_env()` | What a command does with a token once it is in the environment |
| TB6 | User A ↔ User B isolation | Per-user sandbox JWT, per-user memory scope, per-user secret `owner` | Auth misconfiguration (`dev` in prod) collapses all identities into one |
| TB7 | Unattended (headless/routine) run → toolset | Cloud Scheduler OIDC on `/scheduler/*`; isolated `lhart-` sandbox; scoped secrets; non-shell asks denied | The turn still wields the full toolset and acts as the owner with no human watching |

### Threats

| ID | Threat | Boundary | Severity | Mitigation | Code reference |
|---|---|---|---|---|---|
| H1 | Prompt injection in fetched web/tool content steers the model into a harmful tool call | TB4 | Medium | The resulting tool call still passes the A–D guard chain; content itself is **not** scanned (honest limit) | `permission_guard.py`, `exfil_guard.py` |
| H2 | Secret exfiltration via shell (`cat .env \| curl`, metadata-server read) | TB3 | High | `exfil_guard` hard-blocks secret material, credential-read-plus-network shapes, and metadata reads; **heuristic** over command shapes | `horizon/guardrails/exfil_guard.py`, `exfil_config.py` |
| H3 | Data upload to a non-allowlisted host | TB3 | Medium | `exfil_guard` surfaces a `/grant` confirmation | `exfil_guard.py` |
| H4 | Destructive shell command (`rm -rf /`, `git push --force`) | TB2 | High | `command_safety.classify()` returns `deny` (hard-block) or `ask` (interactive approval; hard-deny when headless) | `horizon/guardrails/command_safety.py`, `permission_guard.py` |
| H5 | Cross-user data / workspace access | TB6 | High | Per-user sandbox JWT, per-user memory scope, per-user secret owner; `dev`-in-prod collapse blocked by `K_SERVICE` backstop | `auth/identity.py` (`DevAuthInProductionError`), `sandbox/lifecycle.py`, `memory/user_profile.py` |
| H6 | Forged identity at the HTTP boundary | TB1 | High | `iap` re-verifies the Google-signed JWT (signature + audience) or the GE bearer via `tokeninfo`; `dev` = no verification (local only) | `auth/identity.py` (`_verify_iap_jwt`), `auth/oauth_verify.py` |
| H7 | Credentialed artifact (signed) URL pasted into the model's reply | TB4 | Medium | `redact_artifact_urls_callback` swaps the URL for a placeholder in the model's view; converter strips fabricated artifact links | `context/artifact_url_redaction.py`, `a2a/executor.py` (`_strip_fake_artifact_links`) |
| H8 | Over-broad secret injection into the sandbox | TB5 | Medium | `secret_env()` scopes to the request's user; routines filter to manifest-declared names; interactive turns inject the user's full stored surface | `horizon/secrets/inject.py` |
| H9 | Unattended routine run abuses the full toolset | TB7 | Medium | `/scheduler/*` OIDC-gated; routine runs in an isolated `lhart-` sandbox with scoped secrets; non-shell approvals auto-deny | `permission_guard.py` (`set_headless_mode`), `secrets/inject.py` (`scoped_secret_env`) |
| H10 | Permission over-grant via overlay (blanket "always allow bash") | TB2 | Low | Overlay/grant rules targeting `bash`/`process` with no narrowing field are rejected at load; tenant regexes validated | `permission_rules.py`, `_regex_safety.py` |
| H11 | Stale/expired sandbox token replayed | TB6 | Low | Bearer JWT + routing token each carry a TTL and are re-minted/rotated; the LB returns `401`/`403` on expiry/missing creds | `sandbox/lifecycle.py` (`mint_sandbox_token`, `fetch_routing_token`), `environment.py` |

---

## Where to go next

- [Permission model](permission-model.md) — Layer D ask-gate, grants, and child resurfacing in depth.
- [Architecture overview](architecture.md) — how the callback chains, serving layer, and sandbox fit together.
- [../AGENTS.md](../AGENTS.md) — the per-layer auth/security map + repo conventions.
