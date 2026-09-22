# Routines

**What this doc is.** How Long Horizon runs recurring tasks **unattended** — isolated
sandbox, scoped secrets, headless approvals — for engineers debugging a routine
that didn't fire or working on the `routine` authoring flow.

## In this doc

- **Two artifacts per routine** — the human-readable `.lha/routines/<id>.yaml` manifest vs the runtime `RoutineRow` the fire path consumes.
- **The isolation model** — the three ContextVars: fresh `lhart-` sandbox, scoped secrets, headless approval.
- **The fire path** — `claim_due` → `_fire_routine` → shared A2A handler, end to end.
- **Authoring** — the `routine` tool's `test` / `create` (HITL-gated) / `list` / `cancel` actions + `/routines`.
- **Storage backends** — `LHA_ROUTINE_STORE` (`memory` vs `postgres`) + the croniter scheduling helpers.
- **Environment variables** — `LHA_ROUTINE_STORE` / `LHA_REMINDER_DB_URL` and the shared sandbox knobs.
- **Troubleshooting** — debug-by-symptom: didn't fire, can't see files, missing secret.
- **Where to go next** — sibling subsystem docs.

How Horizon runs a recurring task **unattended** on a cron schedule, in a fresh
sandbox isolated from the user's workspace and holding only the secrets the task
declares. A routine is for work that should happen without the user present —
"every morning, pull the metrics and post a digest", "weekly, bump deps and open
a draft PR". There is no separate one-off reminder tool: a routine cannot
prompt the user mid-run, so it is the wrong fit for a plain time-based ping.

Verified against `horizon/routines/` (`manifest.py`, `store.py`, `tools.py`,
`run_context.py`, `isolation.py`, `run_once.py`), `horizon/scheduler/`
(`routine_store.py`,
`routine_postgres_store.py`, `cron.py`, `routine_tick_endpoint.py`, `sessions.py`),
`horizon/secrets/inject.py`, `horizon/guardrails/permission_guard.py`,
`horizon/conversation/session_start.py`, and `horizon/sandbox/lifecycle.py`.

## Two artifacts per routine

A routine exists as **two** records that are written together at authoring time
and never need to be reconciled at fire time:

1. **The human-readable manifest** — `.lha/routines/<id>.yaml`, written through
   the environment interface (`active_environment().write_file`, see
   `write_routine_via_env` in `horizon/routines/manifest.py`) so it lands inside the
   user's sandbox under the sandbox backend, not just on the host. The body is
   `name` / `schedule` / `task` / `secrets` / `delivery`; the id is the file stem
   (a slug of the name). It is the readable source the user can inspect, and it
   lives in the dotted `.lha/` overlay the agent itself cannot write — the
   backend writes it on the user's behalf after approval. `parse_manifest`
   (`horizon/routines/manifest.py`) normalizes it into the frozen
   `RoutineManifest`.

2. **The runtime row** — a `RoutineRow` in the `RoutineStore`
   (`horizon/scheduler/routine_store.py`) carrying the same fields plus
   `user_id` / `app_name` / `next_fire_at` / `created_at`. This is the copy the
   **fire path consumes**. The row deliberately duplicates the manifest fields so
   the tick is self-contained: a routine fires into a fresh `lhart-<id>` sandbox
   that has no copy of the user's `.lha/`, so the fire path must never read the
   manifest. `.lha/` is the source of truth a human reads; the row is the
   source of truth the scheduler reads.

The `secrets` list is the routine's **blast-radius boundary** — a routine can
only ever hold the secret env vars it names (see the isolation model below).

## The isolation model

The fire path wraps the routine's agent turn in three ContextVars
(`horizon/scheduler/routine_tick_endpoint.py:_fire_routine`), all reset in a
`finally`. Each is a `contextvars.ContextVar` (not a process global) so
concurrent web turns on the same backend instance are unaffected.

- **Fresh isolated sandbox** — `set_routine_run(RoutineRun(routine_id, owner))`
  (`horizon/routines/run_context.py`). When a routine run is active,
  `_ensure_environment` (`horizon/conversation/session_start.py`) takes the
  routine branch: it keys the env cache on `("routine", routine_id)` instead of
  `(backend, user_id)`, and builds the routine's **own** environment via
  `_build_routine_environment`. Under the sandbox backend
  (`_build_routine_sandbox_environment`) that is the routine's own
  `lhart-<id>-<version>` sandbox, discovered/created via `find_routine_sandbox` /
  `routine_display_name` (`horizon/sandbox/lifecycle.py`). The `lhart-` prefix is
  strictly disjoint from the user's `lha-<user>-` prefix, so a routine sandbox
  can never be matched by the user-sandbox discovery and vice versa — it cannot
  see the user's workspace, other projects, installed CLIs, or any other routine.
  Routine sandboxes own the `run.owner` so secret resolution uses the owner's
  declared secrets, but get **no** workspace migration, upgrade-floor, or
  snapshot-restore (those are user-sandbox concerns). Under the local backend the
  routine gets a routine-scoped `LocalEnvironment` dir
  (`<root>/routines/<id>`), again separate from the user's `<root>/users/<id>`.

- **Scoped secrets** — `set_routine_secret_scope(routine.secrets)`
  (`horizon/secrets/inject.py`). `secret_env()` (called by `terminal` / `process`
  when injecting env into a command) reads the active scope and, when set,
  filters the resolved secret map down to only the declared names, so a routine
  receives only its `declared` secrets — never the user's full secret surface. A
  routine that declares no secrets receives none. (The Connect-Google tokens
  `GOOGLE_WORKSPACE_CLI_TOKEN` / `CLOUDSDK_AUTH_ACCESS_TOKEN` are ordinary
  per-user secrets, so they reach a routine only if it declares them.)

- **Headless approval** — `set_headless_mode(True)`
  (`horizon/guardrails/permission_guard.py`). The permission guard runs last in
  the `before_tool_callback` chain as the interactive ask-layer. With no user to
  prompt, an `ask_user` decision splits by tool: a **shell** command (terminal /
  process write) is **allowed** — it runs in the routine's own isolated `lhart-`
  sandbox, which is the blast radius, so build / test / file edits / `git commit`
  work unattended (network ops like `git clone` / `pip install` / `push` also
  need sandbox egress — hermetic when deployed; see
  `LHA_SANDBOX_INTERNET_ACCESS`)
  — while a **non-shell** `ask_user` becomes a
  **deny** (`headless_denied`), since nothing bounds its effect. This is only the
  collapse of the interactive prompt; the earlier guards in the chain still apply
  in full — `exfil_guard` blocks secret-bearing outbound commands, the egress
  guard blocks unconfirmed network, and the destructive-`policies_guard` and any
  explicit `deny` rule still hard-block, all independent of headless mode.

## The fire path

```
Cloud Scheduler ──▶ POST /scheduler/routine-tick
                       │  (verify_cloud_scheduler_token)
                       ▼
                    RoutineStore.claim_due(now)        # rows with next_fire_at <= now;
                       │                                # advances next_fire_at via croniter
                       ▼  for each due routine
                    _fire_routine(runner, handler, row)
                       │  set_routine_run / set_headless_mode / set_routine_secret_scope
                       │  create_scheduled_session(job_type="routine")
                       ▼
                    _run_routine_turn  ──▶  app.state.a2a_handler.on_message_send(...)
                       (the SAME shared A2A handler the web uses)
```

`/scheduler/routine-tick` (`horizon/scheduler/routine_tick_endpoint.py`) is
mounted by `horizon.fast_api_app` alongside the other scheduler endpoints; it is
protected by `verify_cloud_scheduler_token` like the rest of `/scheduler/*`. On each tick it
calls `store.claim_due(now)` — which returns every row whose `next_fire_at` has
passed and atomically advances each row's `next_fire_at` to the next cron
occurrence (routines recur; they are never deleted on claim). For each claimed
routine `_fire_routine` installs the three ContextVars, creates a persisted,
tagged session via `create_scheduled_session(..., job_type="routine")`
(`horizon/scheduler/sessions.py`), and runs the turn under
`user_identity_scope(routine.user_id)` through the **shared A2A handler**
(`app.state.a2a_handler` — the same `DefaultRequestHandler` the web uses). The
turn message is the routine's `task` prefixed with a headless preamble
(unattended, no questions, shell runs in the sandbox, deliver via artifact/report).
Running through the A2A handler records an A2A Task exactly like a normal chat, so
the scheduled run is renderable in the web UI's "Scheduled" folder (driving the
runner directly would leave it blank). The endpoint returns `{"fired": N}` (plus
`"failed": M` if any turn raised).

## Authoring

The user authors a routine via the agent. The `routine` tool
(`horizon/routines/tools.py`) is a first-class root-agent tool with four actions:

- **`test`** (`name`, `task`, optional `secrets`, `schedule`) — run the routine
  ONCE synchronously, right now, under its **real isolation**, and return the
  output WITHOUT scheduling anything. `run_routine_once`
  (`horizon/routines/run_once.py`) builds a throwaway `Runner` over the live
  `App` with ephemeral in-memory services and drives one turn under
  `routine_isolation` — so the run is faithful (same agent/callbacks/plugins, own
  `lhart-<slug>` sandbox, headless, only the declared secrets) but persists
  nothing. It blocks until the turn finishes or `timeout_s` (300s) elapses and
  returns `{success, status: completed|timeout|error, output, iterations,
  duration_ms}`. The agent reads `output` to verify the routine works before
  `create`. (Differs from the cron fire: ephemeral services — no A2A Task, no
  persisted session/row, artifacts saved during a test aren't openable afterward
  — and a timeout. The routine's `lhart-<slug>` sandbox is NOT ephemeral, though:
  it's keyed on the slug and reused by the scheduled routine, so a test before
  `create` warms the exact sandbox the routine will run in. Same isolation via the
  shared `routine_isolation` / `HEADLESS_PREAMBLE` in
  `horizon/routines/isolation.py`, used by both paths so they can't drift.)
- **`create`** (`name`, `schedule`, `task`, optional `secrets`, `delivery`) —
  validates the cron expression (`is_valid_cron`), then **gates on HITL**: the
  first call dispatches an ADK `request_confirmation` and returns
  `status="awaiting_user_response"`, persisting nothing. The agent must stop and
  let the user decide; it must not claim the routine is scheduled until it gets
  `success: true` with an `id`. On the user's confirmation the **backend** does
  both writes — the YAML via the env interface (`write_routine_via_env`) **and**
  `store.add(RoutineRow(...))` with `next_fire_at` computed from the schedule. A
  decline persists nothing. This is the safety contract: nothing is scheduled
  until the human approves, and approval writes both artifacts atomically.
- **`list`** — the caller's routines (id / schedule / next fire).
- **`cancel`** — remove one of the caller's routines by id (scoped to the
  caller's `user_id`).

The agent learns the capability through a thin "Routines:" pointer in the system
prompt plus the on-demand `routines` builtin skill
(`horizon/builtin_skills/routines/SKILL.md`), which carries the how-to detail so
the cached prefix stays lean.

The user can also manage routines directly with the `/routines` slash command
(`horizon/commands/__init__.py`): `/routines` lists the caller's routines and
`/routines remove <id>` cancels one.

## Storage backends

`get_routine_store()` (`horizon/scheduler/routine_store.py`) returns a process
singleton selected by **`LHA_ROUTINE_STORE`**:

- **`memory`** (default) — `InMemoryRoutineStore`, for tests and single-process
  dev. Not durable across restarts.
- **`postgres`** — `PostgresRoutineStore`
  (`horizon/scheduler/routine_postgres_store.py`), asyncpg-backed. It reuses
  **`LHA_REMINDER_DB_URL`** (a historical name, now owned solely by routines);
  unset under `postgres` raises at startup. The `routines` table is bootstrapped
  idempotently on first use, `claim_due` uses `FOR UPDATE SKIP LOCKED` so
  concurrent ticks claim disjoint rows, and every op is wrapped in
  `retry_on_disconnect` for Cloud SQL failover resilience. `secrets` is stored as
  JSON text.

Cron scheduling is `croniter`-backed (`horizon/scheduler/cron.py`):
`is_valid_cron` validates a 5-field expression and `next_cron_fire` computes the
next fire strictly after a given time, raising on an invalid expression.

## Environment variables

- **`LHA_ROUTINE_STORE`** — `memory` (default) or `postgres`. Under `postgres` it
  reuses **`LHA_REMINDER_DB_URL`** (a historical name, now owned solely by
  routines).
- The routine sandbox path (`_build_routine_sandbox_environment`) uses the same
  `LHA_RUNTIME_IMAGE` / `LHA_SANDBOX_CALLER_SA` / `LHA_ENVIRONMENT_BACKEND` as
  the user sandbox; `croniter` is a pinned runtime dependency.

## Troubleshooting

Debug by symptom. Each row points at the code that owns the behavior.

| Symptom | Where to look | Why |
|---|---|---|
| Routine never fires | `RoutineStore.claim_due` + `next_fire_at`; `LHA_ROUTINE_STORE`; Cloud Scheduler → `POST /scheduler/routine-tick` | With `memory` (default) rows are lost on restart; durable firing needs `postgres` (+ `LHA_REMINDER_DB_URL`) **and** the scheduler actually hitting the tick endpoint. |
| `routine(action="create")` returns `awaiting_user_response`, nothing scheduled | the HITL gate in `routines/tools.py` (`request_user_confirmation`) | By contract nothing is written until the user confirms — the backend then does both writes (YAML + `RoutineRow`) atomically. Not a bug. |
| Routine fires but can't see the user's files / installed CLIs | `find_routine_sandbox` / `routine_display_name` (`lhart-` prefix); env cache key `("routine", routine_id)` | A routine runs in its own isolated sandbox with no `/workspace` migration, upgrade, or snapshot — the `lhart-` prefix is disjoint from `lha-<user>-` by design. |
| Routine command fails on a missing env var / secret | `set_routine_secret_scope(routine.secrets)` + `secret_env()` (`secrets/inject.py`) | A routine only holds the secrets it **declares**; add the name to the manifest `secrets` list (its blast-radius boundary). |
| Routine turn blocked on an approval it can't answer | `set_headless_mode(True)` → `permission_guard` (`headless_denied`) | A non-shell `ask_user` fails closed with no user present; design the task to avoid non-shell approvals (shell commands run in the `lhart-` sandbox). |
| Schedule rejected / row silently dropped | `is_valid_cron` (`cron.py`, 5-field croniter); `claim_due` deletes a row whose schedule no longer parses | An invalid cron expression is rejected at `create` and an unparseable stored schedule is dropped rather than looped. |
| `LHA_ROUTINE_STORE=postgres` crashes at startup | `get_routine_store` → `routine_postgres_store.build_from_env` (`LHA_REMINDER_DB_URL`) | Unset `LHA_REMINDER_DB_URL` under `postgres` raises at startup. |

---

## Where to go next

- [`docs/sandbox-lifecycle.md`](sandbox-lifecycle.md) — the user-sandbox model the `lhart-` routine sandbox is deliberately disjoint from.
- [`docs/permission-model.md`](permission-model.md) — headless mode: how the ask-layer behaves with no user present.
- [`docs/memory.md`](memory.md) — the scheduler + Memory Bank plumbing routines share with dream-review.
- [`docs/architecture.md`](architecture.md) — the scheduler + isolation ContextVars in the big picture.
- [`../AGENTS.md`](../AGENTS.md) — the Scheduler section + `LHA_ROUTINE_STORE` / the routine-run ContextVars.
