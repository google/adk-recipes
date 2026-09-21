---
name: scaffold-python-recipe
description: >
  This skill should be used when the user wants to "create a new Python ADK recipe",
  "scaffold a new Python recipe", "generate a new Python recipe in contrib",
  "add a new Python recipe to the adk-recipes repository", or "create a Python adk recipe".
  It utilizes an automated script to copy template files and resolve basic placeholders.
metadata:
  author: Google
  license: Apache-2.0
  version: 1.0.0
---

# Scaffolding a New Python ADK Recipe

Use this skill to scaffold a new Python ADK recipe inside this repository using the automated script at `scripts/scaffold.py`.

---

## Rules

1. **No Manual Boilerplate Writing**: Always use `scripts/scaffold.py` to create the recipe. Never write the boilerplate files manually.
2. **Do Not Modify After Scaffolding**: Once the script runs successfully, do not make any further changes to the recipe in this turn.
3. **Use the Correct Terminology (Recipe)**: Always refer to what is being created as a **recipe**, never as a "project". Ensure all output messages, instructions, and explanations to the user use "recipe" exclusively.

---

## Inputs

You **must** have both pieces of information below before running the script. If either is missing or was not explicitly provided by the user, **ask for it** — do not assume or use a default.

### 1. Output Directory (Required — must ask if not provided)

The user must choose one of these valid locations:
- `contrib/python/` — community recipes; preferred for new Python recipes
- `core/python/` — curated recipes
- `contrib/` — legacy flat layout, still accepted; prefer `contrib/python/`

If the user has not specified which directory, ask them to choose. Do not proceed until a valid choice is confirmed.

This skill **cannot** scaffold a vertical skill (`skills/<vertical>/<solution>/`). Those have a different shape — `SKILL.md`, `EVAL.yaml`, `scripts/`, `assets/`, `references/`, `tests/unit/` — and no template ships for them yet. If the user asks for one, tell them so rather than scaffolding into the wrong place.

### 2. Recipe Name (Required — must ask if not provided)

The recipe name must satisfy **all** of the following rules:
- Contains only lowercase letters and hyphens (`a-z`, `-`)
- Is 30 characters or less
- Does not start or end with a hyphen

If the user provided a name, validate it against these rules before proceeding. If it is invalid, explain which rule it violates and ask for a corrected name. Do not proceed until a valid name is confirmed.

---

## Run

Execute the scaffold script:
```bash
python3 .agents/skills/scaffold-python-recipe/scripts/scaffold.py --name <RECIPE_NAME> --output-dir <OUTPUT_DIRECTORY>
```
*The script accepts exactly two flags: `--name` (required) and `--output-dir` (required). Do not pass any other flags.*

---
## Respond

Once the script succeeds, inform the user the recipe is ready and highlight the following required next steps:

1. **Update `manifest.yaml`**: Fill in the `description`, `ownership.team`, and `ownership.poc` fields (placeholders are marked with `TODO:`). This file is validated by CI and must not contain placeholder values. Use the `generate-manifest` skill if you want it populated automatically from the source code.
2. **Update `README.md`**: Replace the generic title and description with details specific to this recipe (ownership/POC live in `manifest.yaml`, above — not in the README).

Then, provide these quick-start commands:
```bash
cd <OUTPUT_DIRECTORY>/<RECIPE_NAME>
uv sync                  # install dependencies
uv run pytest            # run the test suite
uv run adk run app       # run the agent interactively
```
Do not make any further changes. End your turn.
