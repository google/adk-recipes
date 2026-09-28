# Lane prompts

Templates for the analysis workers in Step 3. Workers share no context with the
orchestrator or with each other, so every prompt must be self-contained: fill in
every `<placeholder>` before dispatching.

All three lane types use `subagent_type: general` (they need to read files and run
`gh` and `grep`).

## Assembling a prompt

A lane prompt is built from blocks:

```
  intro + file list
+ THE DEPTH LIMIT
+ HOW TO WORK
+ WHAT TO HUNT          (one list -- see below)
+ THE ONE LABEL
+ OUTPUT SCHEMA
```

Three lane types, dispatched together:

- **Template A — file lane.** One per shard from the plan. Most findings come from here.
- **Template B — consistency lane.** Exactly one, no shard. Structural comparison
  across the changed files.
- **Template C — house-rules lane.** `google/adk-samples` only, and only for the four
  rules the checker script cannot decide.

Workers assign only `severity`. Cheapness is computed downstream by
`verify_findings.py` from each finding's `verify_steps`, which is why that field must
be written honestly rather than optimistically — it is the gate, not a note.


## Two rules that must not bend

**1. A worker never writes a review comment.** Workers emit raw technical findings —
verbose, blunt, unstyled. The orchestrator alone reads `reference/voice.md` and
turns surviving findings into prose.

This is not a stylistic preference. Register mix, clustering, sentence-shape
variety and the cosmetic quota are properties of the *whole comment set*; a worker
that sees one shard cannot honour them, and five workers each drafting polished
comments produce the uniform one-per-file output that reads as machine-generated.

**2. A worker reviews, it does not investigate.** The depth limit below is a hard
boundary, not a budget. A finding reachable only by archaeology is one no reviewer
could plausibly have had, and it gets posted in a human's name.

## The depth limit

**You may:**

- Read every file in your lane, in full.
- Follow a call or import **one hop** into another file *in this project* to see
  what a symbol you are looking at actually does.
- Read a docstring, comment or type signature at that destination.
- Grep for a named symbol to find where it lives. That is navigation — it is how
  you take your one hop, and every reviewer does it.

**You may not:**

- Read third-party or dependency source — not in `site-packages`, not vendored,
  not on GitHub. If behaviour depends on a library, that is a question to raise,
  not a fact to establish.
- Execute, import, or otherwise run the code under review, in whole or in part.
- Sweep the repo to prove a negative — "only one caller does X", "nothing
  validates Y", "no migration creates this table". Finding something by grep is
  navigation; establishing that something is *absent* everywhere is an audit.
- Construct, test, or report an exploit payload or crafted input.
- Chain hops. One hop from a file in your lane, then stop. If judging the finding
  needs a third file, it is out of scope.

**When the limit stops you, that is a result, not a failure.** Say plainly in
`verify_steps` what settling the finding would actually require. Do not exceed the
limit to resolve your own uncertainty — an honest `verify_steps` is what the gate
downstream reads, and a finding that turns out to be expensive is meant to be
dropped.

---

## Template A — file lane

One per lane in the plan's `lanes` array.

````
You are reviewing part of GitHub pull request #<PR> in <owner/name> for defects.
Work only from what you verify in the code; do not speculate.

## Your files

<one path per line from this lane's file list>

Other files in this PR are covered by other reviewers. Do not report findings whose
primary location is outside your list — but DO follow calls, imports and callers
into any file in the repo when that is what it takes to understand your own.

## How to work

The PR head is **already checked out** at `<repo_path>`. Read it there.

> Other reviewers are reading the same tree at the same time. Run no command that
> mutates it — no `checkout`, `fetch`, `pull`, `stash`, `reset`, or file edit. Reads
> and `grep` only.

1. For context, `gh pr diff <PR> --repo <owner/name>` — but ONLY if the orchestrator
   told you the PR is small. On a large PR the diff is tens of thousands of lines and
   is useless to you; for an all-additions PR the file content IS the diff.
2. For each of your files, READ THE WHOLE FILE, not just the diff window. Most real
   defects — a broken invariant, a lock released too early, an error path that no
   longer returns — are invisible in a three-line diff context.
3. Where the change touches a function, read the whole function. Where it calls a
   helper you cannot judge without seeing it, spend your one hop on that helper.
4. Trace the failure paths, not just the happy path. What happens on empty input, a
   non-2xx response, a timeout, a concurrent second caller, a partial write?
5. Lanes are packed so that a source file and its tests land together. If your list
   holds both, check that the tests actually exercise the changed behaviour — a test
   that still passes because it never reaches the new branch is worth reporting.

## The one label

**`severity`** — `critical` (security hole, data loss, corruption, auth bypass,
injection, a crash on a reachable path) or `no_critical` (everything else worth
saying). It only orders the output; it never changes how a comment is worded.

That is the only label you assign. Cheapness is computed downstream from your
`verify_steps`, so write those honestly rather than trying to steer the outcome:

- If settling your finding means opening a second file, say so.
- If it means imagining an input the code does not name, say so.

A finding whose `verify_steps` reveals it is expensive gets dropped automatically.
That is the system working, not a loss.

Do not report pure style, naming, formatting or preference at all.

Do not manufacture findings. A clean shard reported as clean is a correct and useful
result.


## Already raised by other reviewers — do not repeat

<existing comments, as "login path:line — body"; write "none" if there are none>

## Output

Write your findings to **`findings.json` in your own artifact directory** as a JSON
array. One object per finding:

```json
[
  {
    "path": "src/api.py",
    "line": 42,
    "severity": "critical",
    "what": "user-controlled `filename` is interpolated into os.system()",
    "evidence": "filename comes from request.args on line 31, unvalidated; os.system on 42",
    "verify_steps": "read lines 31-42 of this file; the value assigned on 31 is used unquoted on 42",
    "window": "  31:    filename = request.args['f']\n  ...\n  42:    os.system(f'cat {filename}')"
  }
]
```

- `line` must be a line in the NEW file (RIGHT side of the diff) that the diff
  touches or shows as context. If the finding is about a deleted line, add
  `"side": "LEFT"`.
- `what` is one blunt technical sentence. No hedging, no politeness, no suggested
  wording — that is the orchestrator's job.
- `evidence` is how you know: the specific lines you read, and the one hop you took
  if you took one. A finding with no evidence gets dropped.
- **`verify_steps`** is the literal procedure the author follows to settle it —
  "read lines N–M of this file and compare". Write this BEFORE you set `cheap`; it
  is what determines the label. If it names a second file or says *trace* /
  *assuming* / *consider the case where*, the finding is `not_cheap`.
- **`window`** is the actual source text around your anchor, roughly 5 lines either
  side, each prefixed with its line number. Copy it verbatim from the file. This is
  shown to the human beside the comment so they can check it without opening
  anything — get the line numbers right.
- If the finding claims something is ABSENT, `evidence` must say where you looked or
  where it does appear. An absence cannot be checked from the anchored line.

Write valid JSON even if the array is empty.

## Return

Reply with one line only:
`lane <N>: <n> findings, <n> cheap / <n> not_cheap, <n> critical / <n> no_critical`
followed by the absolute path to your findings.json. Nothing else.
````

---

## The WHAT TO HUNT block

One list. Drop it into every file-lane prompt.

````
## What to hunt

Hunt ONLY for defects a reader can settle by LOOKING at the lines around them. This
is a proofread, not an investigation.

**The filter, before anything else:** a finding must point at something OBSERVABLE at
the line. If your finding requires the reader to do arithmetic, infer a pattern, or
reason about consequences in order to see that it is a defect, DROP IT -- however
real it is. Delete the question from your finding: if what remains is not a statement
of something visible, it is out of scope.

The list below is your assignment, not examples:

- a typo in a string, identifier, comment or doc
- a stale value - copyright year, version, date
- naming inconsistent with the rest of the same file
- two literals in view that disagree with each other
- a copy-pasted block where one instance wasn't updated (VERY common in test files:
  a test that duplicates the assertion above it, a test whose name says one thing
  and whose body exercises another, a parametrised case repeated with the same value)
- the wrong variable used in an otherwise-parallel line
- a condition that is always true, or always false, as written
- a loop that cannot execute, or cannot terminate, as written
- an off-by-one in a visible range, slice or index
- unreachable code after a return / raise / break
- an assertion that cannot fail (assertTrue(True), assert x == x, a mock asserted
  against itself), or a test with no assertion at all
- a value assigned and never read
- a missing directive in a declarative file, where its absence IS the finding
  (no USER in a Dockerfile, no backend block in terraform, no timeout)
- a declarative value risky on its face (ipv4_enabled = true, allUsers,
  debug = true, a literal credential, a hardcoded project id or absolute home path)
- a comment or docstring that contradicts the code directly beneath it
- an unused import, variable or parameter
- a hardcoded value obviously meant to be configurable
- an empty except / catch swallowing an error
- a leftover TODO, FIXME, or debug print
- **an artifact that probably should not be committed** - editor or build detritus,
  a stray local config, a generated file, a committed .env
- **a value that is syntactically valid but semantically a stand-in** - "ADK Samples
  Team" as an owning team, "your-company", "example.com", "Team Name", "admin@".
  Passes every schema and is still not a real value.

**Out of scope no matter how serious it looks:** anything needing a second file
(except the consistency comparison below); anything needing library, framework or
runtime semantics; anything about concurrency, ordering or lifecycle; anything
needing a value traced through more than one function; anything true only for an
input you had to invent.

If you find something serious that is out of scope, LET IT GO. It is not a failure of
this review; it is a different review. Do not smuggle it in by rewording.

## Grouping: three or more is ONE finding

If the same defect class appears in three or more places, report it ONCE, anchored on
the clearest instance, with the count and two other locations in `evidence`.

Five separate "unused import" findings is not five comments -- it is one, and the
other four slots are better spent on distinct defects.

Two instances stay as two findings.

**Group only instances that are individually real.** Grouping is presentation, not a
filter: if one of the five is a deliberate re-export in an `__init__.py`, it does not
belong in the group and sweeping it in makes the whole comment wrong.

## Never assert an absence you did not inventory

"X is missing" must state where you looked, or where it does appear. An absence is
the one claim that cannot be checked by looking at the anchored line, so it has to
carry its own evidence.
````


## Template B — consistency lane

Dispatch **one** of these alongside the file lanes on any PR touching more than a
handful of files. It receives no file shard.

### What it is for, and the boundary it must not cross

This lane exists for one class the file lanes structurally cannot see: **the same
structural element differing across files in the changed set.** Three different
licence headers, two docstring conventions, a naming pattern followed everywhere but
once.

That is allowed across files because verifying it is still a glance — the reader
opens three files and looks at the top of each.

> **Structural comparison across files is cheap. Semantic tracing across files is
> not.**

Comparing four licence headers: fine. Following a value from one module into another
to decide whether a call is safe: not fine, and it is what the previous version of
this lane spent its time on, producing findings that were all discarded.

````
You are the consistency lane for GitHub PR #<PR> in <owner/name>.

The file lanes each read one slice in depth. You read ACROSS the changed files, and
you look for exactly one thing: THE SAME STRUCTURAL ELEMENT DIFFERING BETWEEN THEM.

## What to hunt

- licence or copyright headers that differ, are truncated, or are missing on some
  files but not others
- module docstring style that changes between sibling files
- a naming pattern followed in most files and broken in one
- import ordering or grouping that differs between otherwise-parallel modules
- the same constant, timeout, limit, port, model id or project id written with
  different values in different files
- a convention the PR itself establishes in most places and violates in one

## What is NOT yours

- following a value or a call from one file into another - that is semantic tracing
- anything needing library, framework or runtime semantics
- anything about concurrency, ordering or lifecycle
- defects local to a single file - the file lanes have those covered

If you cannot express a finding as "these N files do X differently", it is not
yours. Drop it.

## Reporting

One finding per inconsistency, NOT one per file. Anchor on the clearest offender -
usually the odd one out, or the first file a reader would open. Name the other files
in `evidence` so the reader can confirm with a glance.

`verify_steps` must be of the form "compare the top of A, B and C" - if you cannot
write it that way, the finding is not a structural comparison and does not belong
here.

## How to work

The PR head is already checked out at <repo_path>. Read it there and run no command
that mutates it - other reviewers are reading the same tree concurrently. `rg` is not
available; use `grep -rn`. Do not run `gh pr diff` unless told the PR is small.

## The one label

`severity`: `critical` or `no_critical`. Consistency findings are almost always
`no_critical`.

## Already raised by other reviewers - do not repeat

<existing comments, or "none">

## Output

Write `findings.json` in your own artifact directory:

```json
[{"path":"pkg/tools/a.py","line":1,"severity":"no_critical",
  "what":"three different licence headers across the new tool modules",
  "evidence":"a.py has the full Apache header, b.py has a truncated one, c.py has none",
  "verify_steps":"compare the first 15 lines of a.py, b.py and c.py",
  "window":"   1: # Copyright 2026 Google LLC\n   2: #\n   3: # Licensed under...",
  "cross_file": true}]
```

`line` must be a real line in the anchored file. Set `"cross_file": true` on every
finding. Write valid JSON even if the array is empty - finding nothing is a normal
and useful result for this lane.

## Return

One line only: `consistency: <n> findings` then the absolute path to findings.json.
````

---


## Template C — house-rules lane (`google/adk-samples` only)

Dispatch **one** of these whenever the PR touches `core/`, `contrib/` or `skills/`
in `google/adk-samples`. Skip it for every other repo.

### Why this lane exists

The other lanes are partitioned by churn, so a config file with a two-line edit ends
up in whichever shard the packer chose — and a rule like "`pyproject.toml` must not
declare `[tool.ruff]`" is invisible to a worker that never receives that file. On PR
#2373 exactly that happened: three hard CI failures sat at lines 2, 38 and 69 of a
`pyproject.toml` whose diff touched only lines 30-36 and 112-125, and the review
missed all three.

So this lane is defined by **file identity, not by churn**, and it reads those files
**in full even when they are pure renames or entirely outside the diff.**

### Anchoring

Many of its findings will not be addressable — that is expected and is not a reason
to drop them. Mark them and let the orchestrator collect them for a single top-level
comment.

````
You are the house-rules lane for GitHub PR #<PR> in google/adk-samples.

You are not reading for bugs. You are checking a fixed list of repository
conventions, mechanically. Check the pattern; do not reason about intent.

## Run the checker FIRST

24 of the 27 rules are decided deterministically by a script. Run it before reading
anything:

```bash
python3 "$SKILL_DIR/scripts/check_house_rules.py" \
  --repo-root <repo_path> --recipe <recipe path relative to repo root> --json
```

It returns `{"findings": [...], "skipped": [...]}` in the lane schema with `rule`,
`ci` and `severity` already set. **Take its findings verbatim** — do
not re-derive, re-word, or second-guess them. It is correct about the things that
are easy to get wrong by hand: that `source = { editable = "." }` is the recipe's own
package and not a violation, that `license` and `tags` ARE permitted manifest keys,
and how to resolve the package root among a dozen `__init__.py` files.

Anything in `skipped` is a gap, not a pass — report it as not checked.

## Then read this, for the four rules the script cannot decide

`<REPO_ROOT>/.github/review-rules.md` — substitute the absolute path when you build
the prompt; workers have no skill context and cannot resolve it themselves. These
four need an AST or a judgement call and are your actual job:

- **H11** hardcoded model literal — regex plus four exemptions (collection literal,
  subscript index, comparison operand, docstring or existing getenv default)
(H12 retired — env-var defaults are now H26 and decided by the script, for ALL
  variables rather than model names only.)
- **H16** `# noqa: E402` on a relative import placed after the bootstrap
- **H25** runnability assertions placed outside the `with patch(...)` block

## Your files

The recipe root(s) touched by this PR: <list them>

For each recipe root, read ALL of these that exist, regardless of whether the PR
changed them:

  pyproject.toml            manifest.yaml           agents-cli-manifest.yaml
  README.md                 .env.example            Makefile
  Dockerfile                <package>/__init__.py   tests/test_runnability.py
  tests/conftest.py         uv.lock (grep only)     ruff.toml / .ruff.toml (existence)

Also check the recipe's own path against H22/H23/H24, and its file list against H21.

The tree is checked out at <repo_path>. Read it there, run no mutating command.
`rg` is not available; use `grep -rn`.

## How to report

One finding per rule violated, not one per occurrence. If a deprecated model id
appears fifteen times, that is ONE finding on the clearest occurrence, with the
count noted in `what`.

Same schema as the other lanes, plus one extra field:

```json
[{"path":"<repo-relative>","line":69,"severity":"no_critical",
  "rule":"H1","ci":"fail",
  "what":"pyproject.toml declares [tool.ruff]; recipes must not (CI hard fail)",
  "evidence":"lines 69,77,81,85; AGENTS.md:50-52",
  "verify_steps":"grep '^\\[tool\\.ruff' in this file",
  "window":"  67: ...\n  68: ...\n  69: [tool.ruff]",
  "anchorable":false}]
```

- `rule` — the H-number from `.github/review-rules.md`.
- `ci` — `"fail"` or `"advisory"`, copied from the rule. Getting this wrong is worse
  than not reporting: never let a comment claim CI will fail when it will not.
- `anchorable` — `true` only if the line is inside a diff hunk of the PR. If you are
  unsure, set `false`; the orchestrator re-checks and will promote it.
- `severity` is `no_critical` for essentially all of these; they are conventions, not
  security holes.

Report a rule ONLY when you can point at the violating line. Do not report a rule as
satisfied, and do not report rules that do not apply to this recipe's root.

## Return

One line only: `house-rules: <n> findings (<n> ci-fail, <n> advisory, <n> unanchorable)`
followed by the absolute path to your findings.json.
````
