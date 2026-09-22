# Permission model — the interactive ask-layer

**What this doc is.** The soft, user-facing *ask*-layer that pauses
*consequential* (not *dangerous*) tool calls — for engineers debugging why a tool
did or didn't prompt, or authoring `.lha/permissions.jsonl` rules.

## In this doc

- **What never asks** — read-only + self-confirming tools that short-circuit before any rule.
- **The layered rule set + precedence** — defaults → `.lha/permissions.jsonl` → session grants, with deny-wins / last-match-wins.
- **Rule schema** — the gemini-shaped JSONL fields + a worked example.
- **Default posture** — shell runs by default; the scary-op (`command_safety`) prompt set; divergences from gemini-cli.
- **The four approval outcomes** — `proceed_once` / `proceed_always` / `proceed_always_and_save` / `cancel`.
- **`/permissions`** — the slash command (and why it is *not* `/grant`).
- **Relationship to the security guards** — soft ask vs hard deny; the retired legacy confirm-tier.
- **Headless / unattended runs** — how routine runs collapse the prompt (and what stays fail-closed).
- **Troubleshooting** — debug-by-symptom: keeps asking, never asks, headless denies.
- **Where to go next** — sibling subsystem docs.

Long Horizon's security guards (`exfil_guard`, `policies_guard`)
are a **hard-deny** floor: they block a tool call outright when it carries secret
material or violates a policy, and the model cannot talk its way past them. But
most tool calls aren't *dangerous* — they're just *consequential*: writing a file
is fine, `git push` or `bq rm` is the kind of thing a user wants to see before it
happens.

The permission gate is the **soft** layer for exactly that case. It is a
user-facing *ask* layer — a tool call it doesn't recognize as safe is paused and
the user is shown a four-button approval card. Their answer can run it once,
remember it for the session, or persist it forever. It sits **below** the
security guards: a call must clear the hard-deny floor first, and only then does
the permission gate decide whether to ask.

The gate is `permission_guard` (`horizon/guardrails/permission_guard.py`), a
`before_tool_callback` that runs **last** in the before-tool chain:

```
before_tool_log_callback → exfil_guard → policies_guard → permission_guard
```

Running last is the contract: the security guards have already had their say, so
anything reaching `permission_guard` is, from the security model's view, allowed.

If anything here disagrees with the code, trust the code — these are the files:
`horizon/guardrails/permission_guard.py` (the gate),
`horizon/guardrails/permission_rules.py` (rule model + `.lha/permissions.jsonl`
store + matcher), `horizon/guardrails/command_classify.py` (shell-command
parsing), `horizon/commands/__init__.py` (`/permissions`).

---

## What never asks

Two tool classes short-circuit before any rule is consulted:

- **Read-only tools** — `read`, `search_files`, `load_skill`, `preload_memory`. No side
  effects, no prompt. `memory(action='search', ...)` (the former standalone
  `session_search`) gets the same bypass — action-aware, since `memory`'s default
  `action='add'` is a write and stays on the normal rule path.
- **Self-confirming tools** — `clarify`. These run their
  own human-in-the-loop flow, so a second prompt would be redundant.

Everything else is resolved against the rule set below.

---

## The layered rule set + precedence

A decision comes from three layers, listed **low → high** precedence:

1. **Built-in defaults** (`DEFAULT_RULES` in `permission_rules.py`) — the shipped
   posture (see the table below).
2. **`.lha/permissions.jsonl`** — the persisted, per-user overlay. One JSON rule
   per line. It lives in the user's sandbox, so the guard reads (and the backend
   writes) it through the environment interface — the durable in-sandbox file, not an
   ephemeral host path; reads are cached per sandbox and reloaded after a write.
   (Under the local-dev backend it's the host fs, hot-reloaded by mtime.) Written
   by the backend on the user's behalf (see the four outcomes) — **the agent
   itself cannot write `.lha/`**.
3. **Session grants** (`session.state["permission_grants"]`) — rules recorded by
   an "allow for this session" approval. Gone when the session ends.

The layers are concatenated into one ordered list and resolved with two rules:

- **Deny wins.** If *any* matching rule says `deny`, the call is denied — a later
  `allow` cannot override an earlier `deny`.
- **Otherwise last-match-wins.** Among the remaining matches, the last one in the
  list (highest layer, latest line) decides. A session grant beats a persisted
  rule beats a default.
- **No match ⇒ `ask_user`.** The implicit default is to ask.

The three outcomes:

- `allow` → the tool runs silently.
- `deny` (user-configured) → the call is blocked with the rule's `denyMessage`
  (or a generic message). This is the *user's* deny, distinct from the security
  guards' hard deny.
- `ask_user` → the user is prompted with the four-button card.

---

## Rule schema (`.lha/permissions.jsonl`)

The schema borrows gemini-cli's field names. Each line is one JSON object:

| Field | Type | Meaning |
|---|---|---|
| `toolName` | string \| array | Tool(s) the rule matches. `*` = any tool. **Required.** |
| `commandPrefix` | string \| array | For `bash`/`process`: matches when the command **starts with** this value at a token boundary (so `"bq"` matches `bq rm …` and `bq ls`, while `"bq rm"` matches only the `rm` subcommand). This is how subcommand granularity works. A flagless prefix also matches **flag-tolerantly** — `"bq query"` covers `bq --project_id=X query …`, so a grant doesn't hinge on where the flags sit. Only self-contained `--flag=value` tokens are skipped; a **bare** flag stops the scan, since it may consume the next token as its value and nothing here knows which CLI does that (so a namespace named `get` in `kubectl -n get delete …` can't impersonate the subcommand). `sudo` is never unwrapped, so a plain `"npm install"` allow does not cover `sudo npm install …`. A prefix that names a flag (`"git push --force"`) matches literally only, so it can't widen onto the plain command it was written to single out. |
| `commandRegex` | string | Regex searched against the command string (bash/process). |
| `argsPattern` | string | Regex searched against the JSON-serialized tool args. |
| `decision` | `allow` \| `deny` \| `ask_user` | What to do on a match. **Required.** |
| `denyMessage` | string | Message shown when a `deny` rule blocks the call. |
| `subagent` | string | Restrict the rule to calls made by this sub-agent (best-effort — matched against the calling agent's name). |

A rule with no `decision` or no `toolName` is dropped; a malformed JSON line is
skipped with a warning.

### Worked example

```jsonl
{"toolName": "web_research", "decision": "allow"}
{"toolName": "bash", "commandPrefix": "bq ls", "decision": "allow"}
{"toolName": "bash", "commandPrefix": "bq rm", "decision": "deny", "denyMessage": "Deleting BigQuery resources is off-limits in this workspace."}
{"toolName": "*", "argsPattern": "prod", "decision": "ask_user"}
```

Read top-to-bottom (last-match-wins, deny-wins):

1. `web_research` runs without asking.
2. `bq ls …` runs without asking; other `bq` subcommands still hit the default
   shell `ask` posture.
3. `bq rm …` is always denied with the custom message — even though line 2 allows
   a sibling subcommand, deny-wins makes this final.
4. Any tool whose serialized args mention `prod` is escalated back to a prompt.

---

## Default posture

Shell runs by default; only genuinely-scary shell ops prompt. What the shipped
defaults do before any overlay or grant:

| Tool group | Tools | Default |
|---|---|---|
| Sandbox-FS writes | `write`, `edit`, `artifact` | **allow** |
| Shell | `bash` / `process` | **allow** — scary ops gated in `_shell_decision` (below) |
| Opened benign tools | `memory` | **allow** |
| Read-only / self-confirming / subagent | `read`, `search_files`, `web_research`, `clarify`, `subagent` | pass (no prompt) |
| Everything else | incl. `routine` (schedules unattended runs) | **ask** (via the `*` catch-all) |

A shell command **prompts** only when a segment is a genuinely-scary op
(`command_safety.classify()` returns `ask`) or contains **command substitution**
(`$(…)` / backticks / `<(…)` / `>(…)`, the anti-obfuscation net). Substitution
detection is **quote-aware**, matching what the shell actually expands: single
quotes suppress everything (a backtick-quoted BigQuery table ref —
``bq query 'SELECT … FROM `proj.ds.tbl`'`` — is literal text, not a nested
command), double quotes still expand `$(…)`/backticks but not `<(…)`/`>(…)`, and
ANSI-C `$'…'` processes backslash escapes (`$'\''` is one literal quote, not a
quote toggle). Unbalanced quoting is unanalyzable and **fails closed**.
The scary set:

- **Destructive deletes:** `rm -rf .` / `rm -rf *` (cwd/glob), `find … -delete`/`-exec`,
  `mv … /dev/null`, and cloud/infra deletes `bq rm`, `gcloud … delete`,
  `gsutil rm`, `gcloud storage rm`, `kubectl delete`, `terraform destroy`,
  `docker rm`/`rmi`/`system prune`, `gws … delete`.
- **History / force git:** `git push --force`/`-f`/`--force-with-lease`,
  `git push --delete`/`--mirror`, `git reset --hard`, `git clean -f`,
  `git filter-branch`/`filter-repo`.
- **Privilege / code injection:** `sudo`/`su`, pipe into an interpreter
  (`… | sh|bash|zsh|python|python3|perl|ruby|node|php`).
- **System-root perms:** recursive `chmod`/`chown` (`-R`) on `/`, `/etc`, `$HOME`, …

So `ls -la`, `git commit`, `git push origin main`, `npm install`, `rm file.txt`,
`rm -rf build/`, `bq mk`, `kubectl apply`, redirection (`echo x > out`) all run
silently, while `git push --force`, `bq rm`, `find . -delete`, `sudo apt …`, and
`curl … | bash` ask first. Mutating `bq query` DML (`DROP`/`DELETE`) and
`gws … +send` now run (the verb-level classifier doesn't parse SQL/semantics).

**Grants/overlays stick.** "Approve for this session / always" on a scary op
writes a grant; the `command_safety` ask demotion is **skipped** for an explicit
grant/overlay allow (source `grant`/`overlay`), so the same-shape op no longer
re-prompts. The catastrophic floor (`command_safety` **deny** + the policy seed)
is enforced in `policies_guard` (Layer C, before this gate) and a grant can never
reach it.

**Chained commands are split per segment.** `ls && bq rm …` is decomposed on
`&&`/`||`/`|`/`;` (and a single `bash -c '…'` / `sh -c "…"` wrapper is unwrapped
first), so a benign segment can't smuggle a gated one — every segment is
classified on its own. **Redirection** (`>` / `>>`) no longer prompts on its own
(dangerous device/`.lha` redirects are hard-denied at Layer C).

### Intentional divergences from gemini-cli

We deliberately do **not** support these gemini fields — they don't fit this
repo's model:

- `priority` / numeric tiers — we use **last-match-wins** ordering instead.
- `modes` / approval-modes — there is one uniform ask posture.
- `mcpName` / `toolAnnotations` — no MCP server surface in scope.

---

## The four approval outcomes

When the gate asks, the payload carries `question` + `choices` — the same generic
shape GE renders for `clarify`, plus a `kind: "permission"` hint the web card uses
for styling. Both the web card and Gemini Enterprise render the `choices` (the
trailing one is Decline) and send the picked one back as `{choice: <label-or-index>}`,
which `_resolve_outcome` maps to one of the four outcomes below on resume (a legacy
`{outcome: …}` key is still tolerated for rollout skew). The four outcomes follow
gemini's `ToolConfirmationOutcome` vocabulary:

| Outcome | Effect |
|---|---|
| `proceed_once` | Run this call once. Remember nothing. |
| `proceed_always` | Allow for the rest of **this session** — records a rule into `session.state["permission_grants"]`. |
| `proceed_always_and_save` | Allow **always** — the backend appends the rule to `.lha/permissions.jsonl` (via the environment interface, on the user's behalf). |
| `cancel` | Decline. The tool call is blocked. |

The recorded rule is scoped to what was asked: for a shell command it carries the
auto-derived `commandPrefix` (binary + subcommand, e.g. `bq rm`); for any other
tool it's a tool-wide `allow`. Self-contained `--flag=value` tokens between the
binary and the subcommand are skipped when deriving it, so
`gcloud --project=p compute instances delete …` scopes to `gcloud compute` — not
the whole `gcloud` binary, which would carry every other `gcloud … delete` with
it. A **bare** flag ends the scan and the prefix stays the binary
(`kubectl -n default delete …` → `kubectl`): scoping it to `kubectl default` would
read as nonsense on the approval card while silently authorizing every kubectl
call in that namespace. Honest-broad beats confidently-wrong — the derivation and
the matcher walk tokens identically (`command_classify.significant_tokens`), so a
derived prefix always matches the command it came from. A `sudo` wrapper is kept
in the prefix rather than unwrapped (`sudo python3 -c …` → `sudo python3`) so the
card names the escalation and the grant can't leak onto the unprivileged form.

---

## `/permissions`

The `/permissions` slash command (`horizon/commands/__init__.py`) reports the
active session grants and the count of persisted rules:

```
/permissions          # list session grants + count of persisted .lha/permissions.jsonl rules
/permissions clear     # drop all session grants (persisted rules untouched)
```

`/permissions` is **not** `/grant`. `/grant` records a one-shot bypass of the
**security** hard-deny guards (exfil/policies) — a different, harder layer.
`/permissions` only touches this soft ask-layer. They use separate session-state
stores (`permission_grants` vs `_policy_grants`); don't conflate them.

---

## Relationship to the security guards

Two layers, two jobs:

| | Hard-deny security guards | Permission gate (this doc) |
|---|---|---|
| Files | `exfil_guard`, `policies_guard` | `permission_guard` |
| Question | "Is this *dangerous*?" (secret exfil, policy break) | "Is this *consequential* enough to confirm?" |
| Posture | Hard deny — not overridable through the ask flow | Soft ask — user can allow once / session / forever |
| Order | Run first | Runs last, only on calls the guards already permitted |

A call must clear the security floor *before* the permission gate sees it; a
`proceed_always_and_save` cannot reopen something exfil/policies blocked. See
[`docs/security-model.md`](security-model.md) for the hard-deny layer.

### Migrated from: the legacy policies confirm-tier

`policies.jsonl` once had a second, softer tier (`requires_confirmation` /
`requires_confirmation_regex`) that surfaced a per-command prompt. That tier has
been **retired** — this permission layer supersedes it. To force a prompt on, or
block, a specific command, add an `ask_user` or `deny` rule to
`.lha/permissions.jsonl`:

```jsonl
{"toolName": "bash", "commandPrefix": "git push --force", "decision": "ask_user"}
{"toolName": "bash", "commandPrefix": "gcloud projects delete", "decision": "deny"}
```

`.lha/policies.jsonl` keeps only its hard-block (`destructive_*`) rules.

---

## Headless / unattended runs

The ask flow assumes a human is present to answer. In **unattended** sessions
there is nobody to click a button, so an `ask_user` cannot pause for input.

**Routine runs** resolve this via `set_headless_mode(True)`
(`horizon/guardrails/permission_guard.py`): an `ask_user` on a **shell** command
(bash / process write) is **allowed** — it runs in the routine's own isolated
`lhart-` sandbox, which is the blast radius — while a **non-shell** `ask_user`
becomes a deny (`headless_denied`). The earlier guards in the chain still apply
unconditionally (exfil, egress, destructive-`policies_guard`, explicit `deny`
rules), so this only collapses the interactive prompt. See
[`docs/routines.md`](routines.md).

Other unattended paths (scheduler-fired routines, the dream-review pass, A2A
turns initiated by another agent) do **not** set headless mode and still share the
uniform ask posture. A broader per-mode auto path (default `ask`) is planned but
not yet wired.

---

## Troubleshooting

Debug by symptom. Each row points at the code that owns the behavior.

| Symptom | Where to look | Why |
|---|---|---|
| A tool keeps prompting every time | `.lha/permissions.jsonl` (`append_persisted_rule`) / session grants (`permission_grants`, `write_session_grants`) | "Yes, once" (`proceed_once`) records nothing; pick "allow this session" (`proceed_always`) or "Always allow" (`proceed_always_and_save`) to persist a rule. |
| A persisted "always allow" still prompts | `resolve_decision` (deny-wins, then last-match-wins); `has_command_substitution` + `command_safety` in `_shell_decision` | A later `ask_user`/`deny` rule (or a `*` `argsPattern`) overrides. Genuine command substitution (`$(...)`) always forces a prompt, even under a grant — but only when the shell would really expand it (quoted-literal backticks don't count); a `command_safety` "ask" op (force-push, `bq rm`, …) is demoted unless the matching allow is a grant/overlay (those override it). |
| Nothing ever asks — everything runs silently | `read_approval_mode` (`approval_mode == "yolo"`, set by `/yolo`) | YOLO auto-approves the ask-layer; it does **not** bypass the exfil/egress/policies hard-deny floor. |
| A tool you expected to gate runs without asking | `READ_ONLY_TOOLS` / `SELF_CONFIRMING_TOOLS` / `SUBAGENT_TOOLS` short-circuits in `permission_guard` | Read-only tools, `clarify`, and `subagent` are exempt by design (the child runs its own guard chain). |
| A chained command's benign half runs but the gated half is blocked | `_shell_decision` → `split_segments` / `strip_wrapper` (splits `&&`/`||`/`\|`/`;`, unwraps `bash -c`) | Each segment resolves its own rule so a benign segment can't smuggle a gated one. |
| A `deny` rule shows a generic message | `permission_guard` deny branch reads `deny_rule.deny_message` | Set `denyMessage` on the rule to customize the block text. |
| A routine call is auto-denied with `headless_denied` | `is_headless()` / `set_headless_mode` (routine fire path) | With no user to prompt, only a **shell** `ask_user` is allowed (it runs in the isolated `lhart-` sandbox); a non-shell `ask_user` fails closed. |

---

## Where to go next

- [`docs/security-model.md`](security-model.md) — the hard-deny floor (exfil/policies) this soft layer sits below.
- [`docs/routines.md`](routines.md) — how headless mode collapses this ask-layer for unattended runs.
- [`docs/architecture.md`](architecture.md) — the before-tool callback chain in context.
- [`../AGENTS.md`](../AGENTS.md) — the callback-wiring table + the `approval_mode` / `permission_grants` state keys.
