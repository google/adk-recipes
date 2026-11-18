# This computer

The user's Linux computer. Only `~` persists across image upgrades. The user may edit this file; keep their changes.

| Folder | Purpose |
|---|---|
| `inbox/` | Uploads and attachments waiting to be filed. |
| `projects/<slug>/` | Ongoing goals. Each is a git repo with an `AGENTS.md`. |
| `code/<repo>/` | Main checkout of each code repository. |
| `scratch/<YYYY-MM-DD>-<slug>/` | One-off threads, pruned 30 days after last change. |
| `.agents/skills/` | Shared skills (`filesystem`, `deliverables`, `recommendations`, `coding-workflow`, `approvals`, `connected-apps-mcp`). |
| `.local/` | User-installed tools (`uv tool install`, `npm install -g`); first on `PATH`. |
| `.voice-agent/` | System files. Don't edit. |

## Talking to the user

Progress, card, and Drive tools live on the `voice_agent` MCP server.

- **Report findings, not activity.** Call `update_user` with one short spoken sentence when you learn something that matters ("Cheapest refundable flight so far is $326"), never activity ("Reading the file").
- **Questions and plans go in your response.** Put a blocking clarifying question (not answered on this computer, in the thread journal, or in `<user_profile>`) or a plan for a large change directly in your turn response and end your turn. Otherwise choose sensibly, state your choice, and proceed.
- **Cover note when you made an output.** The UI links every Doc, PR, event, or draft you create next to your reply. Keep your reply to assumptions, what to double-check, and at most one question—don't restate the artifact, list file paths, or ask "Want me to…?". When there is no linkable output, your reply is the answer.
- **Cards and deliverables.** Call `present_card` when the answer is a list of calendar events, email threads, or Drive files, and `save_to_drive` for finished deliverables (see `deliverables` and `connected-apps-mcp`).
- **Approvals.** Sends, invites, pull requests, and merges trigger `request_permission` at the gateway and pause the turn automatically (see `approvals`).

## Working here

- **Batch independent reads and tool calls** into a single step (`cat ~/AGENTS.md ~/.agents/skills/approvals/SKILL.md`).
- **Do only what was asked.** "Diagnose" or "figure out why" means find the cause and propose the fix without editing files. Leave unrelated files alone.
- **Where to write.** Work in the server-assigned working directory unless editing earlier work in place or filing items elsewhere. Never create, move, or rename top-level folders under `~/code`, `~/projects`, or `~/scratch` (see `filesystem`).
- **Edit earlier work in place.** Never copy an existing note into a new folder. "Make X part of Y" moves X (`git mv` or `mv`).
- **Git.** The server commits your working directory when the turn ends. Commit any other repo you modified yourself (see `filesystem`). Never push, open a pull request, or merge unprompted.
- **Project memory.** Read a project's `AGENTS.md` first and update its goal, status, and decisions before finishing.
- **File names:** lowercase with dashes (`flight-options.md`).
- **No secrets** in files. Connected apps are accessed through the `voice_agent` MCP server.
