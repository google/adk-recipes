# Repository rules — `google/adk-recipes`

**Single source of truth for repo-specific review rules.** Four consumers:

- The four **AI PR reviewers**. `_ai-pr-review-core.yml` injects everything
  between the `BEGIN`/`END REVIEWER RULES` markers verbatim into their prompt.
  They run in an empty scratch directory with no repository access, so the
  marked region is the ONLY repo knowledge they have. How a comment must be
  WORDED lives in [`review-voice.md`](./review-voice.md), injected the same way
  under its own cap.
- The **house-rules lane**, `ai-pr-review-house-rules.yml`. No model: it runs
  `check_house_rules.py` against a checkout of the PR and posts what the script
  decides. Rules it implements do not depend on a reviewer reading this file
  correctly, and it cannot invent a violation.
- The **`github-pr-review` repo skill**, run by hand. Its house-rules lane reads
  this file; `reference/lane-prompt.md` cites findings by `H` number.
- **`scripts/check_house_rules.py`**, which implements the mechanical rules in
  Python. It does not read this file — `tests/test_house_rules_drift.py` pins
  the two together, and fails if a reportable rule is neither implemented there
  nor declared in its `MODEL_JUDGED` list.

Text outside the markers reaches the skill and its tests, never the AI prompt.

## Editing this file

**Rule ids are permanent.** `H1`-`H27` predate this file and are cited in the
skill's output. Never renumber or reuse one; retire it and add `H<next>`.
`H12` and `H46` are retired. The next free id is `H49`.

**Every rule must earn its place.** Name the comment it prevents or produces.

**10-30 words per rule**, plus a source citation. A comment naming a file the
author can open is self-verifying; it is what stops a reviewer sounding
arbitrary. Citations rot — `check_recipe_pyproject.py`'s name check moved from
line 92 to 234 — so re-verify when you edit.

**CI-FAIL vs advisory is load-bearing.** Telling an author something "will fail
CI" when it will not is the fastest way to lose their trust.

**A rule some other check already fails the PR for** belongs in "Already
enforced" — unless a reviewer might plausibly *recommend* the banned thing, in
which case it goes in both.

The marked region has a byte cap, `MAX_RULES_BYTES` in
[`_ai-pr-review-core.yml`](./workflows/_ai-pr-review-core.yml). The value is
deliberately not restated here, so there is one number to change; the build log
prints the region's size on every run. Past the cap the workflow truncates, and
the tail of the region is what goes.

Changes take effect only once merged — the review workflow checks out the BASE
branch, so a PR cannot weaken the rules that judge it.

<!-- BEGIN REVIEWER RULES -->
## Repository rules

This repository hosts **recipes** — self-contained agent examples under
`core/<language>/<name>`, `contrib/<language>/<name>`, and
`skills/<vertical>/<solution>`. Its conventions differ from common practice and
override your priors. Say "recipe", never "sample" or "project".

A rule marked **CI-FAIL** blocks the build; saying so is accurate. A rule marked
**advisory** is a docs requirement CI emits as a notice or does not check at
all. Never tell an author an advisory rule will fail CI.

### Never recommend these

Suggestions that look like improvements elsewhere and are wrong here. Do not
propose them, and do not treat their absence as a defect.

1. **H26** — Never suggest adding a default to an environment read. The default
   belongs in `.env.example`, where someone cloning the recipe can find it.
   Forbidden: `os.getenv("X", "d")` · `os.environ.get("X", "d")` ·
   `os.getenv("X", default="d")` · `os.environ.setdefault("X", "d")` ·
   `os.getenv("X") or "fallback"`. — review preference
2. **H10** — Never suggest `gemini-2.0-flash` or `gemini-2.5-flash`. Both are
   deprecated; the current default is `gemini-3.5-flash`. — `AGENTS.md:40`
3. **H1** — Never suggest a `[tool.ruff]` block or a `ruff.toml` inside a recipe.
   Ruff config lives only in the root `pyproject.toml`. — `AGENTS.md:50-52`
4. **H28** — Never suggest `pip`, `requirements.txt` or `poetry`. The package
   manager is `uv`. — `AGENTS.md:49`
5. **H15** — Never suggest calling `load_dotenv()` in `agent.py`. It belongs in
   the package `__init__.py`.
   — `docs/recipe-handbook/languages/python.md:122`
6. **H17** — Never suggest a value for a `manifest.yaml` ownership placeholder.
   The failing state is deliberate and must keep failing until a human supplies
   one. — `tools/validate_manifest.py:59-60`
7. **H29** — Never suggest widening a recipe's `<2.0.0` ADK ceiling and
   re-locking. Crossing an ADK major is a manual code migration.
   — `.github/policy.yml:576-586`
8. **H30** — Never suggest a bare `uv lock` to raise an already-locked version.
   Use `uv lock --upgrade-package google-adk --python 3.11`.
   — `docs/recipe-handbook/troubleshooting.md`
9. **H31** — Never suggest a `.gitkeep` to satisfy a directory requirement. An
   empty directory already passes the check. — `tools/validate_structure.py`
10. **H32** — Never suggest WebP conversion for an image that code imports or
    references by path. That rule covers doc-only images.
    — `docs/recipe-checklist.md:41`
11. **H33** — Never invent a convention for Go, Java, Kotlin or TypeScript. Only
    the contracts below exist.
    — `docs/recipe-handbook/languages/kotlin.md:5-7`
12. **H34** — Never phrase a comment as a merge block. AI review is advisory; a
    maintainer still approves. — `.github/workflows/_ai-pr-review-core.yml`
13. **H19** — Never suggest a `manifest.yaml` key or enum value outside the
    schema. Required: `type`, `status`, `language`, `description`, `ownership`.
    `type` is `standalone` or `module`; `status` is `active` or `inactive`;
    `language` is `python`, `java`, `go`, `kotlin` or `typescript`.
    — `.github/schemas/manifest-schema.json`
14. **H11** — Never flag a model literal that is a dict or list entry, a
    subscript index, a comparison operand, or inside a docstring.
    — `.github/workflows/python-validate-recipe.yml:324`
15. **H35** — Never flag `os.getenv("HOME")` or similar pre-existing OS variables
    as needing a `.env.example` entry.
    — `.github/scripts/check_env_vars.py:126`
16. **H24** — Never flag a deletion or a rename OUT of a frozen
    `<language>/agents/` path. Migrations out are exempt.
    — `.github/policy.yml:95`
17. **H36** — Never flag a test making real API calls when it sits under an
    integration-test path. That is what those files are for.
    — `.github/workflows/python-tests.yml`
18. **H37** — Never flag `assets/`, `references/` or `tests/unit/` as missing.
    They are convention, not requirements. — `.github/policy.yml`
19. **H38** — Never flag `go.sum` as missing. A Go recipe with no third-party
    dependencies legitimately has none. — `.github/workflows/go-tests.yml`

### Report these

Findings no other check will catch, or that CI raises only as a notice. Apply the
normal filter: visible at the line you anchor to, on an added (`+`) line.

A CI-FAIL rule is listed here only when the check's own message would not tell
the author what is actually wrong. Everything else CI fails is in the next
section, to be left alone.

1. **H11** · **advisory** — A hardcoded model literal in non-test Python. Read it
   from an env var. CI emits only a `::notice`, so it ships.
   — `.github/workflows/python-validate-recipe.yml:324`
2. **H10** · **advisory** — A deprecated model id anywhere: code, README,
   docstring, `.env.example`, notebooks. One grouped comment, never one per hit.
   — `AGENTS.md:40`
3. **H26** · **advisory** — Any hardcoded default on an environment read. Report
   once, grouped, with a count. The repo's own templates violate this; never
   claim CI fails on it. — review preference
4. **H15** · **advisory** — `load_dotenv()` in a non-`__init__` module, or a
   recipe reading env vars whose `__init__.py` has none.
   — `docs/recipe-handbook/languages/python.md:122`
5. **H13** · **CI-FAIL** — An env var name that is not `UPPER_SNAKE_CASE`. Listed
   because CI only says "undeclared", never that the name is the problem, and the
   extract skill then refuses to declare it.
   — `.github/scripts/check_env_vars.py:389`
6. **H14** · **advisory** — An `.env.example` placeholder spelled anything other
   than `<TODO: update-this-value>`.
   — `.agents/skills/extract-python-environment-variables/`
7. **H39** · **advisory** — A stub value committed as if real: `your-project-id`,
   `changeme`, `example.com`, `foo`, `ADK Samples Team`.
   — `.agents/skills/extract-python-environment-variables/`
8. **H16** · **advisory** — A relative import placed after `load_dotenv()`
   without `# noqa: E402`. Only after the first non-import statement.
   — `.agents/skills/extract-python-environment-variables/`
9. **H27** · **advisory** — Files missing the Apache header when most siblings
   of the SAME extension in that recipe carry it. Never pool extensions.
   Absence alone is not a defect. One grouped comment. — repo convention
10. **H40** · **advisory** — A `manifest.language` that disagrees with the
    recipe's path. The two consumers resolve it differently, and Python
    validation is skipped entirely. — `tools/validate_structure.py`
11. **H47** · **advisory** — A vertical skill whose middle folder names a
    LANGUAGE, e.g. `skills/python/foo`. The depth is right so CI passes it, but
    that folder must be a vertical. — `tools/validate_placement.py:67`
12. **H41** · **advisory** — A solution directly under `skills/` receives no
    per-recipe Python validation at all.
    — `.github/scripts/recipe_manifests.py`
13. **H42** · **advisory** — A PR mixing `.agents/skills/` changes with recipe
    changes. Report once, anchored on a real added line. — `AGENTS.md:14`
14. **H43** · **advisory** — A committed `.env`, credential file, build output,
    `.DS_Store` or `.idea/`. Size-exempt, so nothing else reports them.
    — `.gitignore`
15. **H44** · **advisory** — A recipe directory named `bin`, `build`, `dist`,
    `vendor`, `target`, `out`, `coverage` or `.cache`. Those basenames are
    silently pruned from validation. — `.github/policy.yml:103-121`
16. **H25** · **advisory** — A runnability test asserting inside the
    `with patch(...)` block rather than after it.
    — `.agents/skills/generate-python-runnability-test/`
17. **H48** · **advisory** — **Always comment.** An `ownership.team` that names
    no team: a whole company (`Google`, `Google Cloud`), a filler word (`team`,
    `n/a`, `eng`, `demo`), or the author's own GitHub handle — the same string
    as `poc` or a contributor. The schema only checks it is non-empty, so
    nothing else catches it. Ask for the maintaining team, never suggest one.
    — `.github/schemas/manifest-schema.json`

### Already enforced — do not report

A deterministic check fails the PR for each of these with a precise message. A
comment repeating one lands on an author already looking at a red check.

| Id | Do not report | Enforced by |
| --- | --- | --- |
| — | An env var read in source but absent from `.env.example` | `check_env_vars.py` |
| H3 | `[project].name` not matching the recipe folder basename | `check_recipe_pyproject.py:234` |
| H4 | `requires-python` not admitting 3.11 | `check_recipe_pyproject.py:286` |
| — | `[project].description` not equal to `manifest.description` | `check_recipe_pyproject.py:410` |
| H5 | A missing or non-PyPI `[[tool.uv.index]]` with `default = true` | `check_recipe_pyproject.py:558` |
| H6 | `python-dotenv>=1.0.0` absent from `[project].dependencies` | `python-validate-recipe.yml` |
| H7 | A missing `[build-system]` table | `python-validate-recipe.yml` |
| H8 | `testpaths` too narrow to collect the runnability test | `python-validate-recipe.yml` |
| H1 | A `[tool.ruff*]` table in a recipe `pyproject.toml` | `python-validate-recipe.yml:266` |
| H2 | A `ruff.toml` / `.ruff.toml` anywhere in the recipe subtree | `python-validate-recipe.yml:277` |
| H17 | A `manifest.yaml` ownership placeholder | `validate_manifest.py:59-60` |
| H18 | A `manifest.description` under 10 chars or starting `TODO` | `validate_manifest.py:447` |
| H19 | A `manifest.yaml` key outside the schema, or a bad enum value | `manifest-schema.json` |
| H23 | A solution directly under `skills/`, or nested one level too deep | `validate_placement.py:67` |
| H24 | A file added or modified under a frozen `<language>/agents/` path | `.github/policy.yml:95` |
| H45 | A `.go` file with no owning `go.mod` | `go-format.yml:162` |
| H9 | `uv.lock` out of sync, VCS deps, local deps, missing hashes | `python-dependency-policy.yml` |
| H21 | Missing required files (`README.md`, `pyproject.toml`, `uv.lock`, `.env.example`, `tests/test_runnability.py`) | `validate_structure.py:138` |
| H22 | Folder name outside `^[a-z][a-z-]*$` or over 30 chars | `validate_structure.py:86` |
| — | Recipe size over its tier's file or byte cap | `validate-recipe-structure.yml` |
| H20 | A README under 100 words, or missing a setup or run heading | `validate_readme.py:43` |
| — | Formatting, indentation, line length, quote style, import order | the per-language format workflows |
| — | `PORT`, `HOST`, `DEBUG`, `HOSTNAME` and 33 other names absent from `.env.example` | `check_env_vars.py` allowlist |

### Language contracts

Python is the only language with documented conventions. For the rest, these are
the entire contract — enforced by CI, and nothing else applies.

- **Go** — golangci-lint v2.12.2, config in the root `.golangci.yml`, gofumpt +
  gci + golines at **120** columns, Go 1.26. Style changes belong in that config,
  never as workflow flags.
- **Java** — google-java-format **1.36.1**, deliberately non-configurable. No
  style config exists anywhere in the repo.
- **Kotlin** — ktlint **1.8.0**, `ktlint_official` style, JDK 17. There is no
  `.editorconfig`, so **no line-length limit applies**.
- **TypeScript** — Biome **2.5.10**, root `biome.json`, lineWidth 80, double
  quotes, semicolons always, trailing commas everywhere.

Java, Kotlin and TypeScript have no language-specific required files. Never apply
a Python requirement — `pyproject.toml`, `uv.lock`, `.env.example`, a runnability
test, ruff — to a recipe in another language.

### Wording

Cite the file, not the rule id: `AGENTS.md:40` is something the author can open.
Three or more instances of one defect is ONE grouped comment with a count.
<!-- END REVIEWER RULES -->

## Manual reviewer notes

Only the `github-pr-review` skill sees this section — it has a checkout and can
run commands, which the AI reviewers cannot.

**Mechanical checks.**

```
H1  grep -n '^\[tool\.ruff\(\.\|\]\)' <recipe>/pyproject.toml
H2  find <recipe> -name 'ruff.toml' -o -name '.ruff.toml'
H3  compare [project].name against the containing directory's basename
H9  grep the uv.lock diff for git+, source = { git|editable|directory
H10 grep -rn 'gemini-2\.0-flash\|gemini-2\.5-flash' <recipe>
```

**H2 depth.** Any file with that basename at any depth — ruff resolves config
per-file walking up, so a nested one still takes effect.

**H4 boundaries.** `>=3.10`, `~=3.10`, `>=3.9,<4` and absent all permit older
and fail. `>=3.12` and `~=3.12` exclude 3.11 and fail — CI pins 3.11 for
`uv lock --check`, so it surfaces as a misleading "lockfile out of date". Good:
`>=3.11` or `>=3.11,<3.14`; flag neither.

**H27 wording.** Three states per file — full, partial, none — compared per
extension, never pooled. Required for `.py .ts .tsx .js .jsx .sh`;
consistency-only for `.tf .yaml .yml`.

| Situation | What to say |
|---|---|
| ≥60% carry the full header, a minority don't | "N differ from the M carrying the full Apache block" |
| The full header is a minority | "two different headers: N a shorter notice, M the full block" |
| Nothing carries it | "no .py file in this recipe carries the standard Apache header" |

The middle case matters. On PR #2373, 78 files used a one-line notice and 9 used
Apache; calling the 78 "truncated Apache headers" misdescribed the recipe.

**H48 calibration.** `check_house_rules.py` decides it against a closed list of
generic values plus a comparison with `poc`/`contributors`. Do not widen it by
shape: `DEE`, `octo`, `adk-kotlin`, `attenu-io`, `OpenEAGO`, `RobustAI` and
`FDE/Blackbelt` are all real owning teams already in the repo, and a
"that looks like a username" heuristic flags every one of them. An unfamiliar
name is somebody's org until it matches the `poc`. When it does fire, ask who
maintains the recipe; never propose a team name.

**Known contradictions — do not over-claim.**

- Hardcoded model names never fail CI (`python-validate-recipe.yml:324` emits
  `::notice` only), despite the docs stating it as a requirement.
- `check_env_vars.py` and the extract skill are not in sync. `from os import
  getenv` and lowercase names fail CI as undeclared while the skill refuses to
  declare them. Real deadlocks; the fix is manual.
- `setdefault` is a first-class source for the extract skill and deliberately
  invisible to the CI checker.
- The recipe-name regex differs across three implementations — `my-recipe-`
  passes CI and is rejected by the scaffolder.
- `align-recipe-pyproject` treats a non-PyPI default index as `report_only`;
  `check_recipe_pyproject.py` treats it as a hard fail. Same condition, two
  verdicts.
