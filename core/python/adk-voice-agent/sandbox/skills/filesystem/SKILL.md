---
name: filesystem
description: Finding and filing things in the home. Use when a thread builds on earlier work (a note, a draft, a past thread), or files, moves, or tidies files.
---

# Finding and filing things

- **Find earlier work:**
  1. List folders: `ls ~/projects ~/code` and `ls -t ~/scratch | head -20`.
  2. Search text: `rg -il "<words>" ~/projects ~/scratch ~/inbox`.
  3. Check git history: `git log --oneline` (`thread <id>: <title>`).
  4. Query the thread journal: `list_threads` and `read_thread` on `voice_agent` (`project` and `paths` are relative to `~`).
- **Edit in place:** When updating an earlier note, edit it in its original folder even if given a new working directory; leave the new working directory empty and state which file you updated.
- **Move, don't copy:** "Make X part of Y", "put X in Y", and "file X" move X (`git mv` within a repo, `mv` across folders) so the original is gone. When filing from `~/inbox`, rename after the subject (`mv ~/inbox/IMG_2041.pdf ~/projects/sell-the-car/title-scan.pdf`), leave unmatched items in place, and never delete from `~/inbox` unless asked.
- **Commit changes outside your working directory:** The server commits only your assigned working directory. Commit any other repo you modified, staging only your files (`git -C ~/projects/<slug> add <files> && git -C ~/projects/<slug> commit -m "<what>"`; after a cross-repo move, commit the removal with `git add -A <old path>` too).
- **Project `AGENTS.md`:** Rewrite (don't append) to keep it current. If a project folder lacks `AGENTS.md`, create one:
  ```markdown
  # <Goal in one line>

  ## Status
  One short, speakable sentence on where things stand and what's next.

  ## Decisions
  - <Decision>: <why> (<YYYY-MM-DD>)

  ## Constraints
  Budget, deadlines, people, and preferences for this goal.
  ```
- **Top-level folders are server-owned:** Never create, move, or rename folders directly under `~/code`, `~/projects`, or `~/scratch`, or suggest doing so (the server promotes `~/scratch` to `~/projects`). Never touch `~/.voice-agent/`, `.worktrees/`, or `.git/`.
