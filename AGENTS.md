# Agent Guidelines for adk-recipes
All agents must follow the guidelines below without being reminded.

## General
- Use the term **recipe** instead of **sample** everywhere — responses,
  code comments, commit messages, PR descriptions, docs.
- Two different things are called "skills"; keep the terms straight:
  - **Vertical skills** — recipes under `skills/<vertical>/<solution>/`
    (e.g. `skills/retail/store-ops/`). Shipped to users.
  - **Repo skills** — AI coding-assistant helpers under `.agents/skills/`
    (e.g. `prepare-python-recipe`). Used to build this repo.
- Recipes live under `core/` (curated), `contrib/` (community), or
  `skills/` (vertical skills). Repo skills are a completely separate
  concept. Do NOT mix `.agents/skills/` changes and recipe/vertical-skill
  changes in the same PR. If a tool run modifies files outside your task's
  scope (e.g. a repo-wide ruff sweep touching an unrelated recipe), revert
  those and mention them so the user can decide.
- When your task is scoped to a specific recipe or skill, stay inside its
  directory. If you notice similar issues elsewhere, flag them in your
  response instead of opportunistically fixing them.

## Git
- Never run `git commit`, `git push`, `git rebase`, `git merge`,
  `gh pr create`, `gh pr merge`, or any other command that mutates git history
  or the remote, unless the user's **most recent message** contains an
  **explicit** request to do that specific action (e.g. "commit this",
  "push it", "create a PR", "open a PR"). Even a request scoped to the exact
  paths you have just modified needs the explicit verb.
- If, after doing the requested work, there are uncommitted changes and you
  believe they should be committed, **ask first**. Show the diff summary,
  suggest a commit message, and wait for an explicit "yes, commit" or
  "yes, push".
- Safe read-only git commands are always fine: `git status`, `git diff`,
  `git log`, `git show`, `git branch`, `gh pr view`, `gh pr list`.
- Reverting your own uncommitted work (`git checkout <path>` on a working-tree
  change you made in this session) is also fine — it undoes, it doesn't
  mutate history.

## Models
- Do NOT use `gemini-2.0-flash` or `gemini-2.5-flash` — both are deprecated. Use `gemini-3.5-flash` instead.

## Python
- Python recipes go under `contrib/python/` or `core/python/`. Vertical
  skills go under `skills/<vertical>/<solution>/` (e.g.
  `skills/retail/store-ops/`) — that middle folder is a **vertical**, not a
  language, and it is mandatory. A skill's language comes from
  `manifest.language`.
- Minimum python version: 3.11
- Package manager: Use `uv`, not `pip`
- Formatter/linter: `ruff` — line length 80, double quotes. Config lives
  in the root `pyproject.toml`; never create a standalone `ruff.toml` or
  `.ruff.toml` inside a recipe.
- Test runner: `pytest`.
- Do not rely on `.env` being present for the unit tests. However,
  `.env.example` files are present for each recipe and the unit tests can
  rely on those.
