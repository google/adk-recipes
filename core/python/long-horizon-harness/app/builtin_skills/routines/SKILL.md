---
name: routines
description: Use for a recurring unattended task (cron; pull data, open a PR, post a digest), or to test/dry-run a routine first. Explains authoring and testing via the routine tool.
---

# Scheduling routines

A **routine** is a recurring task that runs **unattended** on a cron schedule, in
a **fresh sandbox isolated from the user's workspace**, holding **only the secrets
the routine declares**. Use it for "every morning, summarize X and report" /
"weekly, bump deps and open a PR" — work that should happen without the user
present. There is no one-off reminder tool: it cannot ask the user anything
mid-run, so it is the wrong fit for a plain "remind me at 9pm" ping.

## The isolation model (why this is safe)

- The routine runs in its OWN sandbox (`lhart-<id>`), never the user's — so it
  cannot see their workspace, other projects, or undeclared secrets.
- It receives ONLY the secret env vars listed in `secrets:` — that list IS the
  blast-radius boundary. Declare the minimum the task needs (check the "Available
  secret env vars" line for the names).
- It is **headless** (nobody to approve mid-run): shell commands run normally in
  the isolated sandbox — `git clone`, `pip install`, build, `commit`/`push` all
  work, since the sandbox is the blast radius. Secret-exfiltration and
  known-destructive commands are still refused, and any *non-shell* action that
  would need approval is auto-denied — design the task to avoid those, or do an
  irreversible external step as a draft for the user to confirm later.
- Outputs come back as artifacts in the routine's session — it cannot write into
  the user's workspace.

## How to author one

Call `routine(action="create", name=..., schedule=..., task=..., secrets=[...])`:
- `schedule`: a 5-field cron expression (`"0 8 * * *"` = daily 08:00 UTC).
- `task`: a self-contained instruction — the routine has no memory of this chat
  and no access to the user's files, so state everything it needs.
- `secrets`: the exact secret env names the task requires (e.g.
  `["GITHUB_PAT"]`), or omit for none.

## Test it before scheduling

ALWAYS dry-run a routine before you create it:

```
routine(action="test", name=..., task=..., secrets=[...])
```

This runs the task ONCE right now under the routine's real isolation — its own
`lhart-` sandbox, headless (shell runs; non-shell approvals denied), only the declared `secrets` —
and blocks until it finishes, returning `{success, status, output, ...}`. Read the
`output`: if the task didn't work (missing secret, needed an approval, wrong
assumption), fix `task`/`secrets` and test again. A test schedules nothing (no
schedule row, no chat session) — but it does provision the routine's real
`lhart-` sandbox, which the scheduled routine then reuses, so pass the same `name`
you'll `create` with. Then `create`.

## Scheduling it

The tool **asks the user to confirm** before scheduling. On the first call it
returns `status: "awaiting_user_response"` — stop, let the user decide, and do
NOT claim the routine is scheduled until you get `success: true` with an `id`.

Use `routine(action="list")` to show scheduled routines and
`routine(action="cancel", id=...)` to remove one (the user can also type
`/routines` / `/routines remove <id>`).
