# Sandbox lifecycle

**What this doc is.** How Long Horizon maps sandbox provisioning, reattach, and
snapshot/restore onto ADK's app/user/session model — for engineers debugging why
a workspace did or didn't survive across sessions, restarts, or upgrades.

## In this doc

- **Persistence model** (blockquote, below the title) — reattach vs snapshot/restore, and version-scoped identity with version-agnostic reattach.
- **Diagram 1 — Identity hierarchy** — where the sandbox attaches: scope is (process, user), not per-session.
- **Diagram 2 — Single-process lifecycle** — cache miss/hit, reattach, and what process exit does (and doesn't) do.
- **Diagram 3 — Snapshot / restore** — the Phase C nightly snapshot + restore-on-session-start path (off in code; Terraform turns it on when you deploy).
- **Diagram 4 — What survives what** — the survival matrix across RUNNING vs reaped sandboxes.
- **Environment variables** — the `LHA_SNAPSHOT_*` / `LHA_SANDBOX_TTL` / `LHA_RUNTIME_MIN_VERSION` knobs.
- **Troubleshooting** — debug-by-symptom: not reattaching, files lost, force-upgrades.
- **Where to go next** — sibling subsystem docs.

How Horizon's sandbox provisioning maps onto ADK's app / user / session model.
Verified against `app/sandbox/`, `app/conversation/session_start.py`, and
`app/agent.py`.

> **Persistence model (what's actually implemented).** There is no durable
> `/workspace` backing store on the host — files live inside the running Vertex
> sandbox container. They survive across sessions two ways:
>
> 1. **Reattach (always on).** `close()` only shuts the local HTTP client; the
>    platform-side sandbox stays RUNNING, and the next session for the same user
>    reattaches to it (`find_latest_user_sandbox`, `app/sandbox/lifecycle.py`).
>    This holds until the sandbox's TTL/idle teardown. See Diagram 2.
> 2. **Snapshot / restore (Phase C, gated by `LHA_SNAPSHOT_ENABLED` — off in
>    code, but `terraform/cloud_run.tf` sets it to `1`, so a deployed stack has
>    it on).** To survive the teardown a daily Cloud Scheduler job snapshots
>    each active user's full `$HOME`, and a session with no RUNNING sandbox
>    restores from the latest snapshot before provisioning blank. This is a
>    **real implemented path** — `app/scheduler/snapshot_endpoint.py` +
>    `snapshot_and_prune_user` / `restore_sandbox_from_snapshot` in
>    `app/sandbox/lifecycle.py` — **not** a host-local atexit/`index.json`
>    design. See Diagram 3.
>
> **Version-scoped identity, version-agnostic reattach (implemented).** The
> per-user sandbox `display_name` encodes the runtime image tag
> (`lha-<user>-<tag>`, `app/sandbox/lifecycle.py:sandbox_display_name`), but
> reattach (`find_latest_user_sandbox`) is **version-agnostic**: it picks the
> user's most-recent RUNNING sandbox regardless of image version, so a backend
> rollout keeps installed CLIs (they live in `$HOME`/`~/.local`, outside the
> migrated `/workspace`). Upgrades are **explicit**: `/sandbox-upgrade`
> (`upgrade_user_sandbox` → `find_prior_user_sandbox`) provisions a fresh
> current-image sandbox and **migrates `/workspace`** into it — zip-download from
> the prior (`GET /files/zip`) → server-side extract on the new one
> (`POST /files/zip`) — then deletes the prior. The same migration runs
> automatically only for a sandbox below `LHA_RUNTIME_MIN_VERSION`
> (`version_below_floor`, off by default). Migration is best-effort: on any
> failure the prior sandbox is left intact for TTL cleanup so a bug can't lose
> files (`session_start._migrate_workspace`).

## Diagram 1 — Identity hierarchy and where the sandbox attaches

```
ADK identity model (per ADK conventions: app/user/session)
┌──────────────────────────────────────────────────────────────────┐
│ App(name="app")  ← one per process; defined in app/agent.py      │
│                                                                  │
│   ├── User(user_id="alice")  ← from auth middleware              │
│   │     ├── Session(id=ctx-1)  ← ADK events list                 │
│   │     ├── Session(id=ctx-2)                                    │
│   │     └── Session(id=ctx-3)                                    │
│   │                                                              │
│   │     ════════════════════════════════════════                 │
│   │     ║ ONE Sandbox shared by all of alice's    ║              │
│   │     ║ sessions in this process                ║              │
│   │     ║ (cache key = (backend, user_id))        ║              │
│   │     ════════════════════════════════════════                 │
│   │                                                              │
│   └── User(user_id="bob")                                        │
│         ├── Session(id=ctx-4)                                    │
│         └── Session(id=ctx-5)                                    │
│                                                                  │
│         ════════════════════════════════════════                 │
│         ║ ONE separate Sandbox for bob          ║                │
│         ════════════════════════════════════════                 │
└──────────────────────────────────────────────────────────────────┘

Key insight: sandbox scope is (process, user). NOT per-session.
Two sessions for alice in the same process see the same /workspace.
```

## Diagram 2 — Single-process lifecycle

```
                       ┌────────────────────────┐
                       │   PROCESS BIRTH        │
                       └────────────┬───────────┘
                                    │
              Module import time:
                 atexit.register(_close_envs_at_exit)
                                    │
              FastAPI lifespan: Runner built, _env_cache = {}
                                    │
            ┌───────────────────────┼───────────────────────┐
            ▼                       ▼                       ▼
   alice's first turn       bob's first turn        alice's second turn
   miss → reattach /        miss → reattach /       HIT → reuse env_A
   restore / provision A    restore / provision B   (no new provision)
            │                       │                       │
            ▼                       ▼                       ▼
   env_cache[alice]=A       env_cache[bob]=B        ContextVar.set(A)
            │                       │                       │
            └───────────┬───────────┴───────────┬───────────┘
                        │                       │
                        ▼                       ▼
                ... many more turns, sessions, users ...
                                    │
                                    ▼
                  ┌──────────────────────────────────────┐
                  │ PROCESS EXIT (atexit)                │
                  │ _close_envs_at_exit(): for env in    │
                  │   cache → env.close_sync()           │
                  │ → shuts the local HTTP client only.  │
                  │   The platform-side sandbox stays    │
                  │   RUNNING for the next process to    │
                  │   reattach. NO snapshot here.        │
                  └──────────────────┬───────────────────┘
                                     │
                                     ▼
                            PROCESS DEATH
```

> Process exit does **not** snapshot. `_close_envs_at_exit`
> (`app/conversation/session_start.py`) only closes the local HTTP client so
> sockets don't leak; the sandbox is reattached by the next process (any
> instance — discovery is Agent Platform's authoritative list, not a host-local file).
> There is no signal-handler snapshot flush and no `~/.lha/snapshots/index.json`.
> Durable survival across a sandbox teardown is the snapshot/restore job below.

## Diagram 3 — Snapshot / restore (Phase C)

Gated by `LHA_SNAPSHOT_ENABLED` (`snapshots_enabled()`). When unset, both halves
below no-op and the only persistence is reattach-until-TTL (Diagram 2). Unset is
the *code* default — a deployed stack has it set: `terraform/cloud_run.tf` puts
`LHA_SNAPSHOT_ENABLED = "1"` on the backend and `terraform/cloud_scheduler.tf`
creates the daily job unconditionally.

```
  ── nightly ─────────────────────────────────────────────────────────
  Cloud Scheduler  ──POST /scheduler/snapshot──▶  snapshot_endpoint.py
   (terraform: "37 3 * * *")      body {user_ids: [], app_name}
                                          │
              empty user_ids → list_active_users(lookback)
                                          │  per user:
                                          ▼
              snapshot_and_prune_user(...)   (sandbox/lifecycle.py)
                ├─ snapshot the RUNNING sandbox → lha-<user>-<ver>-snap
                │    (captures full $HOME; ttl = LHA_SNAPSHOT_TTL, default 30d)
                └─ prune to newest LHA_SNAPSHOT_KEEP (default 2)

  ── next session, no RUNNING sandbox to reattach ─────────────────────
  on_session_start  ──▶  _build_sandbox_environment (session_start.py)
                                          │
            find_latest_user_sandbox == None  (TTL/idle teardown, or first run)
                                          │
                       _restore_from_snapshot_or_none(...)
                ├─ find_latest_user_snapshot(user)
                ├─ restore_sandbox_from_snapshot(...)  ← runs the image the
                │     snapshot was taken on (stay-on-your-version)
                └─ on failure: provision a blank sandbox instead
```

Discovery is via Agent Platform's authoritative snapshot list
(`client.agent_engines.sandboxes.snapshots.list`), filtered by the
`lha-<user>-` display-name prefix — no host-local index, so every Cloud Run
instance agrees on the latest snapshot. The restore path is skipped on the
sub-floor force-upgrade path (it wants a fresh current-image sandbox) and on
routine (`lhart-`) sandboxes (per-routine, no snapshot/restore).

## Diagram 4 — What survives what

```
                     SCENARIO
   ┌────────────────────────────────┬──────────────────────────────┐
   │ Sandbox still RUNNING          │ Sandbox reaped (TTL / idle)  │
   ├────────────────────────────────┼──────────────────────────────┤
   │ Next session reattaches        │ LHA_SNAPSHOT_ENABLED set:    │
   │ (version-agnostic).            │   restore from latest        │
   │ /workspace + $HOME intact.     │   snapshot (full $HOME).     │
   │ Process restart is irrelevant  │ Unset:                       │
   │ — discovery is Agent Platform's list,  │   no snapshot → fresh blank  │
   │ not a host file.               │   sandbox, /workspace empty. │
   └────────────────────────────────┴──────────────────────────────┘

   Snapshot freshness is bounded by the daily job: at most ~24h of work
   is lost if a sandbox is reaped between snapshots. Snapshots themselves
   expire at LHA_SNAPSHOT_TTL (default 30d) and are capped at
   LHA_SNAPSHOT_KEEP (default 2) per user.
```

## Environment variables (snapshot / restore)

| Var | Default | Effect |
|---|---|---|
| `LHA_SNAPSHOT_ENABLED` | unset (off) locally; `1` on Cloud Run (set by `terraform/cloud_run.tf`) | Master switch for Phase C — gates both the daily snapshot job and restore-on-session-start (`snapshots_enabled()`). |
| `LHA_SNAPSHOT_TTL` | `30d` | TTL stamped on each snapshot. |
| `LHA_SNAPSHOT_KEEP` | `2` | Snapshots retained per user; older ones pruned each run. |
| `LHA_SANDBOX_TTL` | `14d` | TTL on the live sandbox (and on a restored one). |
| `LHA_RUNTIME_MIN_VERSION` | unset | Force-upgrade floor: a sandbox below it is migrated to the current image at session start instead of reattached. |

Verified against `app/scheduler/snapshot_endpoint.py`,
`app/sandbox/lifecycle.py`, `app/conversation/session_start.py`, and
`terraform/cloud_scheduler.tf`.

## Troubleshooting

Debug by symptom. Each row points at the code that owns the behavior.

| Symptom | Where to look | Why |
|---|---|---|
| New session gets a fresh blank `/workspace` instead of the previous one | `find_latest_user_sandbox` (version-agnostic, `lha-<user>-` prefix match) | Reattach picks the user's most-recent RUNNING sandbox regardless of image version; if none is RUNNING (TTL/idle teardown, or first run) it provisions blank (or restores a snapshot, if enabled). |
| Installed CLIs disappear after a backend rollout | `version_below_floor` + `/sandbox-upgrade` (`upgrade_user_sandbox`) migrating only `/workspace` | Reattach is version-agnostic so CLIs in `$HOME`/`~/.local` survive a rollout; an *explicit* upgrade re-provisions and the zip migration (`POST /files/zip`) carries `/workspace` data only (drops binaries/exec bits). |
| Files gone after a sandbox was reaped | `LHA_SNAPSHOT_ENABLED` (off in code, `1` on a Terraform-deployed backend); `snapshot_and_prune_user` / `restore_sandbox_from_snapshot` | Without Phase C there is no durable backing store — reattach only survives until the sandbox's TTL/idle teardown. |
| Snapshot exists but restore never happens | `snapshots_enabled()`, `find_latest_user_snapshot`; restore skipped on the force-upgrade path and on routine (`lhart-`) sandboxes | The master switch is off, or the path intentionally skips restore (it wants a fresh current-image / per-routine sandbox). |
| Sandbox force-upgrades on every session | `LHA_RUNTIME_MIN_VERSION` + `version_below_floor` | A sandbox below the floor is migrated to the current image instead of reattached; an unparseable token/floor never forces an upgrade. |
| A routine sees the user's workspace (or vice versa) | env cache key `("routine", routine_id)` vs `(backend, user_id)`; `find_routine_sandbox` (`lhart-` prefix) | Sandbox scope is (process, user); routine sandboxes use a disjoint `lhart-` prefix and a separate cache key, so neither discovery can resolve the other. |
| A restarted process can't find an existing sandbox | discovery via Agent Platform's authoritative `sandboxes.list`, not a host file | Every Cloud Run instance reads the same authoritative list, so process restart is irrelevant to reattach. |

---

## Where to go next

- [`docs/routines.md`](routines.md) — the `lhart-` routine sandboxes, a deliberately disjoint sibling of the user sandbox.
- [`docs/memory.md`](memory.md) — the cross-session story for *facts*, the analog of snapshots for the workspace.
- [`docs/architecture.md`](architecture.md) — the environment interface + sandbox in the big picture.
- [`../AGENTS.md`](../AGENTS.md) — "Sandbox runtime image" + the `LHA_SANDBOX_*` / `LHA_SNAPSHOT_*` / `LHA_RUNTIME_*` knobs.
