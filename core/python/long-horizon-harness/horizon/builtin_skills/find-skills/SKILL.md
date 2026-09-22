---
name: find-skills
description: Discover/install NEW agent skills from external sources (skills.sh). Use for capabilities you lack, e.g. "find a skill for X". Do NOT use to list skills you already have; see `<available_skills>`.
---
# Find skills

## When to use this skill

- The user asks "find a skill for X", "is there a skill that does X"
- The user asks "how do I do X" where X is a common task with a likely existing skill
- The user wants to extend Horizon's capabilities with a new skill

Before searching externally, check the `<available_skills>` block in your
context — the user may already have a relevant skill installed.

## Step 1 — check the leaderboard

Before running any CLI command, check [skills.sh](https://skills.sh/) to see
whether a well-known skill already covers the domain. The leaderboard ranks by
total installs, surfacing the most popular and battle-tested options.

## Step 2 — search for skills

If the leaderboard doesn't cover the user's need, search via the Skills CLI:

```
bash(command="npx --yes skills find <query>")
```

Examples:

| User says | Search |
|---|---|
| "make my React app faster" | `npx --yes skills find react performance` |
| "help with PR reviews" | `npx --yes skills find pr review` |
| "create a changelog" | `npx --yes skills find changelog` |

If `npx` is unavailable the command will fail — fall back to browsing
skills.sh manually and pasting the SKILL.md content.

## Step 3 — verify quality before recommending

Never recommend a skill based solely on search results.

- **Install count** — prefer 1 000+ installs; treat anything under 100 with skepticism
- **Source reputation** — official organisations (`vercel-labs`, `anthropics`,
  `microsoft`) are more trustworthy than unknown authors
- **GitHub stars** — check the source repo; a skill from a repo with <100 stars
  is a yellow flag

## Step 4 — present the option to the user

```
I found a skill that might help: "react-best-practices" from Vercel Engineering
provides React and Next.js performance guidelines (185K installs).

Shall I install it?
```

Only proceed to installation after the user confirms.

## Step 5 — install

### Option A — write SKILL.md directly (always works)

If you have the skill content (pasted by the user, retrieved from skills.sh,
or constructed from the search result):

```
write(".agents/skills/<name>/SKILL.md", content)
load_skill(action="reload")
```

`write` creates the directory if it does not exist. `load_skill(action="reload")`
refreshes the `<available_skills>` catalog (plus extensions and manifest) on
the next turn.

### Option B — install via npx (requires Node.js)

First check whether npx is available:

```
bash(command="which npx")
```

If it is, install the skill. Select a single skill from a multi-skill repo with
the `@<skill>` suffix (omit it to take the whole repo):

```
bash(command="npx --yes skills add <owner/repo>@<skill-name> -y")
```

The Skills CLI stages the skill (with its `references/`, `assets/`, and
`scripts/` subdirs) under `.agents/skills/<skill-name>/` — exactly where Horizon
loads skills from — so no move is needed. Just refresh:

```
load_skill(action="reload")
```

The staged directory name MUST match the `name:` field in the SKILL.md
frontmatter — `_load_skill_from_dir` validates that and skips mismatched
skills.

## Step 6 — confirm activation

After `load_skill(action="reload")` returns (look for the new skill in
`loaded`), tell the user:

```
The "<name>" skill is now installed. It will appear in your
<available_skills> catalog on the next turn.
```

## When no skills are found

1. Acknowledge that no existing skill was found
2. Offer to help directly using general capabilities
3. Offer to write a custom skill for them:

```
bash(command="npx --yes skills init my-skill-name")
```

Or skip the CLI and author one manually — write a SKILL.md following the
`policy` or `workspace` skills as examples.

## Common skill categories

| Category | Search terms |
|---|---|
| Web development | react, nextjs, typescript, css, tailwind |
| Testing | testing, jest, playwright, e2e |
| DevOps | deploy, docker, kubernetes, ci-cd |
| Documentation | docs, readme, changelog, api-docs |
| Code quality | review, lint, refactor, best-practices |
| Design | ui, ux, design-system, accessibility |
