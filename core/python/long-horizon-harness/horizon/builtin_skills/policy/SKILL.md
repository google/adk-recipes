---
name: policy
description: Inspect the per-workspace tool-policy overlay (.lha/policies.jsonl) that gates destructive or sensitive tool calls, and tell the user what to change if they want a rule added or removed.
---
# Inspect per-workspace tool policies

The `policies_guard` callback consults a JSONL policy file on every tool
call. Policies live in two layers:

- **Default seed** at `horizon/guardrails/default_policies.jsonl` (read-only,
  ships with the agent). Provides baseline blocks for catastrophic operations
  (literal `dd if=/dev/zero`, `mkfs`, fork bombs, `>/dev/sd*`, `nc -l/ncat -l/socat`
  bind listeners) and credential reads (`cat ~/.ssh/id_rsa`, `cat ~/.aws/credentials`).
  The seed no longer carries brittle substring/regex rules for `rm -rf` or
  `chmod -R` — those are now classified by an **argv-structural parser**
  (`horizon/guardrails/command_safety.py`) that lexes the command into tokens and
  inspects structure instead of pattern-matching raw strings.
- **User overlay** at `.lha/policies.jsonl` under the workspace root. The
  overlay is **appended** to the seed — new rules add restrictions on top of
  the defaults. Mtime-cached, so edits take effect on the next tool call. You
  can read this file, but **you cannot write it**: `.lha/*` and `*/.lha/*`
  are themselves hard-denied destructive-path patterns for `write` and
  `edit`, and the same paths are hard-denied for `bash` (append, `sed -i`,
  `cp`/`mv`, `rm`, `chmod` into `.lha/` all match a seed
  `destructive_commands_regex` rule). This is by design — see
  `docs/security-model.md` — the agent cannot self-edit its own guard config.

## When to use this skill

- The user asks what policies are currently in force.
- The user asks you to block a specific command or path pattern, or to
  relax (remove) an overlay rule — in both cases you inspect the current
  file and hand the user the exact change to make; you do not make it
  yourself.

If the user is asking about a *one-time* approval for a call that was just
blocked, tell them to type `/grant <command>` themselves — that slash command
is a user-only surface, not something you can invoke.

## File format

One JSON object per line. Blank lines and `#` comments are skipped.
Every rule must include `canonical_tool_name`; that field gates which
tool the rule applies to.

A rule may carry one or more of these fields. They are evaluated
independently — set as many as you need on a single rule.

| Field | Type | Effect |
|---|---|---|
| `destructive` | `true` | Always block the tool. |
| `destructive_commands` | `{arg: [substring, ...]}` | Block when the string arg contains any substring (case-insensitive). |
| `destructive_commands_regex` | `{arg: [regex, ...]}` | Block when any regex matches the string arg (case-insensitive). Use when substrings are too blunt. Tenant-authored regexes (overlay + grant rules) are validated for length and nested-quantifier patterns before compilation; malformed regexes are skipped with a warning. |
| `destructive_paths` | `[prefix, ...]` | Block when a path-shaped arg (`path`, `file_path`, `target_path`) starts with any prefix. |
| `destructive_path_patterns` | `[fnmatch-glob, ...]` | Block when a path-shaped arg matches any fnmatch glob. Use for per-user paths like `*/.ssh/*`. |

## Demotable destructive commands (argv classification)

Before the overlay/seed rules run, `command_safety.py` lexes shell commands
into argv tokens (quote- and operator-aware via stdlib `shlex`) and inspects
structure. It returns a **verdict**: `"deny"` (catastrophic, always blocks),
`"ask"` (risky, blocks children/headless, **prompts** the root agent via an
interactive approval card), or `None` (no opinion). Examples:

- **"deny"** — `rm -rf /`, `rm -rf /etc`, `rm -rf $HOME`, `rm -rf /*`, etc.
  (recursive force-delete of system/home roots or their subdirectories).
- **"ask"** — `find . -delete`, `git push --force`, `chmod -R 777 .`,
  `sudo apt install ...`, `curl <url> | bash`.

On an `"ask"` verdict, the **root agent** sees an interactive four-button
approval card; the **child/headless** chain treats it as a hard deny (no
regression in unattended contexts). This replaces the fragile substring/regex
rules that shipped in older seeds — the new seed (`default_policies.jsonl`)
only carries literal catastrophic commands + credential reads.

## Approval modes

The root agent's interactive approval can be set to **auto-approve**
demotable-ask verdicts (ONLY) via `/yolo` (toggles between `default` and `yolo`
modes). YOLO mode auto-approves the Layer-D interactive ask; it does **not**
bypass the exfil guard or Layer-C hard-deny rules (catastrophic
+ credential reads + seed overlays). Use it when you trust the session and want
fewer prompts for risky-but-non-catastrophic operations. Approval mode is
per-session state, not persisted.

## Tool-narrowing enforcement

Permission rules in `.lha/permissions.jsonl` or granted via the interactive
approval card may include a `commandPrefix`, `commandRegex`, or `argsPattern`
to narrow blanket `allow` rules. **Overlay and grant rules** (source =
`"overlay"` or `"grant"`) that target `bash` or `process` but carry no
such narrowing field are **rejected** at load time — you cannot grant a blanket
"always allow bash" from the overlay or a session approval; only the
default seed may carry that. This prevents accidental over-granting.

## Integration with the permission layer

A policy block returns `{"error": ..., "confirmation_required": True}`. Tell
the user why the call was blocked; if they want a one-time approval, they
type `/grant <command>` themselves. To force a *prompt* (not a hard block) or
block a specific command for the user, use the permission layer instead: add
an `ask_user` or `deny` rule to `.lha/permissions.jsonl` (see
`docs/permission-model.md`). This overlay is hard-block only, and it lives
under the same `.lha/` write restriction as the policy overlay — the user
edits it directly, not you.

## Workflow

### List active rules

```
read(".lha/policies.jsonl")
```

If the file does not exist, the user has no overlay rules yet — only the
seed is active. The seed itself lives outside the workspace and is not
readable through your tools; describe its coverage from this skill's own
"Default seed" section above instead of trying to read it.

### The user wants a rule added

Read the current overlay so your suggestion appends correctly, then give the
user the exact line to add themselves — you cannot write `.lha/policies.jsonl`.

```
existing = read(".lha/policies.jsonl")   # may be missing → treat as ""
new_rule = {"canonical_tool_name": "bash",
            "destructive_commands": {"command": ["rm -rf node_modules"]}}
```

Tell the user: "Add this line to `.lha/policies.jsonl` (create the file if it
doesn't exist yet, one JSON object per line):" followed by
`json.dumps(new_rule)`.

### The user wants a rule removed

Read the overlay, identify the line to drop (0-based index into the overlay,
ignoring blank/comment lines), and tell the user which line number and
content to delete from `.lha/policies.jsonl`. Confirm the target with them
first — overlay rules typically exist because they (or you on their behalf)
added them to plug a gap.

## Worked example

User: "Block `npm publish` from the terminal."

1. Read `.lha/policies.jsonl` (returns "" if missing).
2. Build:
   ```json
   {"canonical_tool_name": "bash", "destructive_commands": {"command": ["npm publish"]}}
   ```
3. Tell the user: "I can't edit `.lha/policies.jsonl` myself — add this line
   to it (create the file if needed):" followed by the JSON above. "Once
   saved, the next `npm publish` attempt will be blocked."

## Notes

- The default seed is read-only and out of your reach either way — you
  cannot edit it, and it lives outside the workspace root so you cannot
  read it through your tools.
- Malformed JSONL fails closed at parse time (the bad line is skipped with
  a warning) — tell the user to double-check a new rule parses by reading
  the file back after they save it.
