---
name: coding-workflow
description: How to work in a code repository under ~/code. Use for any thread that diagnoses, fixes, changes, reviews, or tests code.
---

# Working on code

- Read the repository's instructions (`AGENTS.md`, `CLAUDE.md`, `CONTRIBUTING.md`, `README.md`) and check `git status` first.
- **Diagnose without changing:** "Why is this failing", "figure out", "look into", "review", and "explain" ask for analysis, not edits. Reproduce the issue, identify the file, line, and introducing change, and propose the fix in your response without modifying files.
- **Fixing:**
  - Reproduce first with a failing test or command, then fix the root cause. Never skip, delete, or weaken a test to pass.
  - Make the minimal change matching surrounding style; add or update tests for changed behavior.
  - Report meaningful findings during the run with `update_user` on `voice_agent`.
  - For large, underspecified changes, write a brief proposed plan or clarifying question in your response and end your turn.
- **Verifying:** Run existing tests, linters, and type checkers, and report only checks you actually ran.
- **Git & GitHub:**
  - The server commits your working directory on the thread's branch when the turn ends. Do not commit, rebase, or switch branches unless asked.
  - Never push, open a pull request, or merge on your own; tell the user the branch is ready to publish.
  - Use `github__*` tools on `voice_agent` to read issues, PRs, reviews, and CI runs (gated writes pause via `request_permission`).
- **Finishing:** Summarize what changed, how you verified it, and anything left undone in a few plain sentences without listing file paths.
