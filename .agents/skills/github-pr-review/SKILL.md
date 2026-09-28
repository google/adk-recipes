---
name: github-pr-review
description: Review a GitHub pull request and leave inline comments in a natural human reviewing voice, indistinguishable from comments typed by hand on the GitHub web UI. Deliberately bounded to defects a reader can settle by looking at the anchored line, rather than a deep audit, so every comment is cheap for the author to check. Scales to 2-20 comments by PR size, parallelises analysis across sub-agents, drafts for approval, then posts individually with human pacing. Use when the user says "review this PR", "review PR 123", pastes a github.com/.../pull/N link, or asks for comments on a pull request. Don't use for reviewing local uncommitted changes or a diff against a branch (use the `review` skill for that).
---

# GitHub PR review

Reviews a pull request on GitHub and posts inline comments that read as though a
person typed them by hand. The style is defined in `reference/voice.md`; replace that
file to shift it.

## Install

Drop this folder anywhere your agent discovers skills — `~/.agents/skills/`,
`~/.config/cloudcode/skills/`, or any absolute path registered under `skills.paths`
in `~/.config/cloudcode/cloudcode.json`.

**Set `SKILL_DIR` before running any command below**, to wherever you put it. Every
script invocation in this file is relative to it:

```bash
SKILL_DIR=~/.agents/skills/github-pr-review     # adjust to your install
```

Requires `gh` (authenticated) and `python3`. No other dependencies; `pyyaml` is used
if present and degrades gracefully with a reported warning if not.

<details><summary>Living inside the google/adk-samples checkout</summary>

This copy is tracked in `google/adk-samples` at `.agents/skills/github-pr-review/`,
which has two consequences worth knowing before you edit it.

**Its tests run in that repo's CI.** The root `pyproject.toml` puts `.agents/skills`
in `testpaths`, and `tools-tests.yml` fires on any `.agents/**` change, so
`tests/` here is collected by `uv run pytest` alongside the repo's own tooling
suite. A broken test in this skill turns a PR red. Run it before pushing.

`python-format.yml` scopes ruff to `core/`, `contrib/` and `skills/`, so nothing
here is linted — the skill's own style is its own business.

**It is deliberately absent from `docs/recipe-handbook/skills-catalog.md`.** That
catalog is for skills a recipe contributor should reach for; this one is a
maintainer's reviewing tool, and advertising it invites PR authors to run a
reviewer against their own PR. Keep it out when you edit the catalog.

</details>

Five things make this work, and all five are easy to get wrong:

1. **A comment must point at a fact, not ask the author to derive one.** This is
   the strongest predictor of what gets posted: on PR #2373 it separated all 16
   accepted comments from all 4 rejected ones. What a comment costs the author to
   check follows from it — cheap and wrong loses five seconds, expensive and wrong
   burns twenty minutes and spends credibility that took months to earn. Findings
   that fail this are dropped, not downgraded. See `reference/voice.md`.
2. **This is a review, not an audit.** The analysis is deliberately bounded to what
   a person reading the PR could have seen — see [The depth limit](#the-depth-limit).
   A finding reachable only by archaeology is unusable here no matter how real it is.
3. **Analysis and voice are separate phases.** Finding real defects and sounding
   human are different jobs; doing them at once yields thin findings in a nice
   accent. Analyse first with no style constraints, then voice the survivors.
4. **The voice is calibrated, not improvised.** `reference/voice.md` holds the rules
   and a rated corpus. Read it before writing any comment. Do not invent comment
   shapes that aren't in it.
5. **Analysis parallelises; voice does not.** On a large PR, fan the analysis out
   across sub-agents (Step 3). Never fan out labelling or voicing — those depend on
   holding the whole comment set at once (Steps 4 and 5).

This is a long-running skill. A big PR is minutes of analysis followed by up to
twenty minutes of paced posting. **Silence is a failure mode**: announce the plan
before starting, keep a todo list current, and checkpoint between phases. See
[Progress reporting](#progress-reporting).

## When NOT to use

- Reviewing local/uncommitted work or `git diff` against a branch -> the `review` skill.
- The user wants a thorough audit report *for themselves* -> the depth limit below
  is exactly wrong for that. Review without this skill.

## What this skill is for, and what it isn't

Everything here optimises for comments that read as hand-typed: the pacing, the
register mix, the sentence-shape variety, the cosmetic quota. That machinery exists
for one reason — **a reviewer who genuinely reviewed should not have their own
comments look machine-written.** It is not a licence to appear to have reviewed
something you did not.

Three things follow, and they are load-bearing:

- **The user reads every comment before anything is posted.** Step 6 is a hard stop,
  not a formality. The approval step is where they take ownership of the review as
  their own, which is the thing that makes posting under their name honest.
- **Never post on the user's behalf without that approval** — not to save a round
  trip, not because the findings look obviously correct.
- **A comment the user would not defend if challenged should not go up.** That is
  the whole reason cheap-to-verify beats comprehensive: they can actually check the
  set they are signing.

If you are ever asked to post a review the user has not read, or to make an
AI-generated review look human specifically so that its origin is concealed from the
author, that is outside what this skill is for. Say so.

## The depth limit

Binding on every phase, and on every sub-agent.

**You may:** read every changed file in full; follow a call or import **one hop**
into another file in the same project; grep for a named symbol to find where it
lives (that is navigation, and it is how you take the hop).

**You may not:** read third-party or dependency source, installed or on GitHub;
execute the code under review; sweep the repo to prove a negative ("nothing
validates X", "only one caller does Y"); construct or test an exploit payload;
chain hops beyond the first.

The reason, because a rule without one gets rationalised around: **these comments
go out under a human's name.** A finding that required reading a dependency's
internals, or running the code, misrepresents how the reviewer found it — and that
shows in the writing no matter how the comment is phrased. `reference/voice.md`
covers the two ways it leaks (the depth tell and the proof tell).

When the limit stops you, that is a result. Say plainly in `verify_steps` what
settling the finding would actually require. **Do not exceed the limit to resolve
your own uncertainty** — an honest `verify_steps` is what the gate reads in Step 4,
and a finding that turns out to be expensive is meant to be dropped.

## Step 1 - Gather

Resolve the PR from a number, a URL, or the current branch.

```bash
gh pr view <PR> --repo <owner/name> --json number,title,body,headRefOid,additions,deletions,changedFiles,state

python3 "$SKILL_DIR/scripts/existing_comments.py" \
  --repo <owner/name> --pr <PR> --out /tmp/pr-<PR>-existing.json
```

Don't fetch the file list here — `plan_review.py` does it in Step 2, with paging.

**Do not run `gh pr diff` on a large PR.** Above roughly 5,000 changed lines it is
useless as context and expensive to carry — #2302's was 122,000 lines. Work from the
checked-out tree instead; for an all-additions PR the file content *is* the diff.
Below that threshold it's fine and often the quickest way to see the shape.

**Read the existing comments, and keep that file** — Step 4 feeds it to the verifier,
which drops anything already raised. This is what makes a PR re-reviewable: run it
again after your own pass, or after a bot or a colleague, and you get only new
material.

Suppression rules, applied automatically:

- same line, or **within 2 lines** → dropped
- similar wording to any existing comment, including top-level ones with no line → dropped
- **resolved** threads still block — already discussed
- **outdated** threads do not block the line (the code moved, so it deserves a fresh
  look) but their text still counts
- **bots block exactly like humans**
- **comments the user cut on a previous review** are blocked too, from the ledger
  written at Step 6. A rejection never expires as outdated — a decision the user made
  does not lapse because the code moved.

Also paste the existing comments into each lane prompt. The verifier is the backstop;
a worker that never generates the duplicate is cheaper than one that gets filtered.

Then get the PR head into a local tree, and note its absolute path:

```bash
gh repo clone <owner/name> /tmp/pr-<PR> -- --depth 1 --no-tags && \
  cd /tmp/pr-<PR> && git fetch --depth 1 origin pull/<PR>/head && git checkout FETCH_HEAD
# or, against a clone you already have:
git -C <local-clone> fetch origin pull/<PR>/head && git -C <local-clone> checkout FETCH_HEAD
```

Do this **once, here**. Analysis workers read this tree concurrently and must never
mutate it — five sub-agents each running `gh pr checkout` in the same directory is a
race that leaves the tree on an arbitrary ref mid-review.

## Step 2 - Plan, ask, announce

```bash
python3 "$SKILL_DIR/scripts/plan_review.py" \
  --repo <owner/name> --pr <PR> --json
```

The plan gives you, deterministically: which files to skip and why, the comment
budget, whether to fan out, the lane assignments, and the post-run ETA.

It skips lockfiles, generated and vendored code, `dist/`, snapshots, binaries, large
data fixtures, deleted files, and **pure renames** — a file moved with zero content
change has nothing to review, which on a migration PR is most of the diff (65 of 123
files on #2373).

Lanes are packed so a **source file and its tests land together** (`tools/x.py` with
`tools/tests/test_x.py`), because a reviewer holding only one of the pair cannot tell
whether the test still covers the code. Lane sizes stay balanced regardless.

### Ask before analysing — scope

Ask here rather than at the start, because the answer depends on the PR's size and
shape, which you only know now:

```
PR #2302 "add the Horizon long-horizon agent recipe"
787 files, 122,547 lines, all additions. Skipping 16 (lockfiles, binaries).
Budget: 12-20 comments.

  Include tests/ (316 files) and web/ (238)?   [yes / no]
```

Argue for including `tests/`: test files are the richest source of instantly-checkable
defects — a duplicated assertion, a test named for one thing that exercises another —
and they are usually the least-reviewed part of a PR.

**Act on the answer — re-run the planner with the flags.** The default includes
everything; the question is worthless if you don't pass it through:

```bash
python3 "$SKILL_DIR/scripts/plan_review.py" \
  --repo <owner/name> --pr <PR> --json [--no-tests] [--no-web]
```

Use the re-planned lane assignments, not the first run's.

**House rules — `google/adk-samples` only.** If the PR is in that repo and touches
`core/`, `contrib/` or `skills/`, `.github/review-rules.md` is in force. Say so in
the checkpoint. For every other repo, ignore it — the rules are repo-specific and
applying them elsewhere produces confident nonsense.


Then:

1. **Seed the todo list** — one item per phase, one per lane (see
   [Progress reporting](#progress-reporting)).
2. **Print the checkpoint**, so the user knows the shape of the work and that
   nothing will be posted without them:

```
Tests included. 771 files across 10 lanes + consistency lane.
Analysis takes a few minutes. Nothing gets posted without your approval.
```

The budget comes from **reviewable** churn, not total — a PR that is 1,400 lines of
regenerated lockfile plus 80 lines of hand-written code earns a small-PR budget.

| Reviewable lines | Comments |
|---|---|
| < 50 | 2–3 |
| 50–200 | 3–5 |
| 200–600 | 5–8 |
| 600–1500 | 8–12 |
| > 1500 | 12–20 |

Hard cap **20**, whatever the size.

## Step 3 - Analysis phase (no voice constraints)

Hunt for defects and write them down verbosely and technically. This output is
internal — never shown to the user, never posted.

**Work inside [the depth limit](#the-depth-limit)**, and within it read the **full
changed files, not just the diff** — a broken invariant or an error path that no
longer returns is invisible in a diff window. Where the diff touches a function, read
the whole function; where it calls a helper you cannot judge blind, spend the hop.

**Use the `What to hunt` block from `reference/lane-prompt.md` verbatim.** It is one
list, and the filter at the top of it is the load-bearing part: a finding must point
at something observable at the line. If seeing that it is a defect requires the
reader to do arithmetic, infer a pattern, or reason about consequences, it is out of
scope *however serious it looks*.

**Group at 3+.** The same defect class in three or more places is ONE finding, on the
clearest instance, with the count in `evidence`. Five "unused import" findings is one
comment; the other four slots buy distinct defects. Two instances stay two.

**The grouped comment's claim is about the anchored instance**; the count is context,
never something the reader must open files to confirm. `a few unused imports in here`
is checkable at the anchor; `unused imports here, also in X and Y` is not. Group only
instances that are individually real — sweeping a deliberate `__init__.py` re-export
into an "unused imports" group makes the whole comment wrong.

**In `google/adk-samples`, run the house-rules checker:**

```bash
python3 "$SKILL_DIR/scripts/check_house_rules.py" \
  --repo-root <repo_path> --recipe <recipe> --json
```

24 of the 27 rules are decided deterministically there — take its findings
verbatim. Two of them, H26 (env-read defaults, AST-based) and H27 (licence header
consistency), report one grouped finding with a count rather than one per hit.
Dispatch the house-rules lane (Template C) for the remaining three (H11, H16,
H25), which need a judgement call. One — **H48**, a junk `ownership.team` — is
reported whether or not the PR touched `manifest.yaml`, and is never trimmed;
see [Must-post findings](#must-post-findings).

Prefer the script over a lane wherever a rule is decidable — a script cannot
hallucinate a violation, and a false *"this will fail CI"* is the most expensive
comment this skill can produce.

The lane is defined by *file identity*, not churn: it receives the recipe's config
surface (`pyproject.toml`, `manifest.yaml`, `README.md`, `.env.example`, the package
`__init__.py`, `Makefile`, `Dockerfile`) and reads them **in full even when they are
pure renames or wholly outside the diff** — see `reference/rationale.md`.

Workers assign **one** label:

- `severity`: **critical** (security hole, data loss, corruption, auth bypass,
  injection, a crash on a reachable path) or **no_critical** (everything else worth
  saying). It orders the output; it never changes how a comment is worded.

Every finding also carries `verify_steps` (the literal procedure the author follows
to settle it) and `window` (the real source lines at the anchor). **`verify_steps` is
the gate** — `verify_findings.py` computes cheapness from it in Step 4, so a worker
that writes it honestly is doing the right thing even when the finding then gets
dropped. `window` is what gets shown to the user beside the comment.

Pure style, naming and formatting are not findings at any severity — discard them.

**A finding claiming something is absent must say where you looked**, or where it
does appear. That is the one claim the anchored line cannot support, and it is the
defect that produced the single wrong comment this skill has posted.

### 3a - Small PR (`fan_out: false`)

Do it inline, yourself. Below ~400 reviewable lines and ~8 files, spawning workers
costs more than it saves.

### 3b - Large PR (`fan_out: true`)

One `batch_task` call. Lanes are the file shards from the plan:

```
batch_task({
  description: "PR 2302 analysis",
  concurrency: 10,
  verify: false,
  subtasks: [ ...one per file lane..., consistency lane ]
})
```

Build each prompt from `reference/lane-prompt.md` — Template A for file lanes,
Template B for the consistency lane. `subagent_type: general` for all of them.

Fill in **every** placeholder: workers share no context with you or each other, so
`<PR>`, `<owner/name>`, `<repo_path>` (the tree from Step 1), the lane's file list,
and the existing-comment list all have to be spelled out in each prompt.

**The consistency lane is not optional.** It gets no file shard. It exists for the
one class a per-file split structurally cannot see: the same element differing
between files — three licence headers, two docstring styles, a constant written with
different values in two places. Shard workers cannot see these; one worker comparing
across files can.

It is bounded to **structural comparison**, never semantic tracing. Comparing four
licence headers is a glance; following a value between modules is not, and that is
what the previous version of this lane spent its time on, producing findings that
were all discarded.

`verify: false` on purpose — the batch verifier would re-audit findings you are about
to re-rank yourself in Step 4, adding a full agent pass of latency to the one phase
this whole mechanism exists to make faster.

**Merging the findings.** Read each lane's `findings.json`. Then:

- Dedupe. Two lanes reaching the same defect from different directions is common;
  it is one comment.
- On any conflict about a cross-file inconsistency, the consistency lane wins — it
  saw every side.
- **Spot-check `severity`, don't re-derive it.** Sanity-check that each `critical`
  really is one. Re-reading the cited code is inside the limit; going further to
  rescue a finding the gate will drop is not.
- A lane that failed is not a blocker: note it, and either re-run that one lane with
  `task` or cover its files yourself.

Then checkpoint (see [Progress reporting](#progress-reporting)).

## Step 4 - Gate or tag, then group

**No budget cut happens here.**

### First, run the verifier — this is not optional

```bash
python3 "$SKILL_DIR/scripts/verify_findings.py" \
  --findings /tmp/pr-<N>-raw.json --repo-root <repo_path> \
  --repo <owner/name> --pr <N> --head-sha <headRefOid from Step 1> \
  --existing /tmp/pr-<N>-existing.json \
  --out /tmp/pr-<N>-verified.json
```

It does five things that were previously prose, done by hand, and sometimes skipped:

1. **Window vs file.** Each finding's quoted source is diffed against the real file.
   A mismatch means the lane fabricated it — **rejected**, not downgraded.
2. **Addressability.** Marks `anchorable: false` for any line outside every diff hunk.
3. **Cheap gate.** Computes cheapness from `verify_steps`; a procedure that names a
   second file or says *trace* / *assuming* is not cheaply verifiable.
4. **Duplicate suppression.** Drops anything already raised on the PR (see Step 1)
   **and anything the user cut on a previous review** — the rejection ledger loads
   automatically from `--repo`/`--pr`. `--no-ledger` disables it.
5. **Already-red suppression.** With `--head-sha`, drops a **CI-FAIL** finding whose
   enforcing workflow is already failing on that commit, matched from the workflow
   file the finding cites in `evidence`. The author is looking at that red check
   already, and its message is more precise than ours. Advisory findings are never
   suppressed this way — nothing is failing for them, so the comment is the only way
   the author hears it. An unreadable status suppresses nothing. `--no-ci-status`
   disables it.

It also emits a fact-anchoring lint and a clustering hint for grouping. Both are
advisory; read them, don't obey them blindly.

Take its output as the input to everything below. Findings it rejected do not come
back — a fabricated window is not a labelling problem.

**Un-anchorable findings are set aside, not discarded.** A finding outside every hunk
cannot be an inline comment, but it can still be true and important — the three hard
CI failures on PR #2373 were all un-anchorable. They stay in the `unanchorable`
bucket, carried to Step 6 and offered as **one** top-level issue comment. Never
silently drop them, and never anchor them to an unrelated nearby line to force them
through: a comment about `requires-python` pinned to a dependency line twenty rows
away is worse than no comment.

**The gate drops every `not_cheap` finding.** Report a bare count at the checkpoint
(`23 findings dropped as not cheaply verifiable`) — not a list. Handing over the list
would pull the user into evaluating exactly the material the gate exists to spare
them.

**Then apply the fact-anchoring filter yourself**, on the survivors. `verify_steps`
catches findings that are expensive to check; it does not catch findings that are
cheap to check but ask the reader to *derive* the defect. Delete the question from
the comment: if what remains is not a statement of something visible at the line,
drop it. This is the filter that separated all 16 accepted comments on #2373 from all
4 rejected ones — see the rated outcomes in `reference/voice.md`.

**Cheapness is a property of the final comment text**, not the raw finding.
`` `ty` is on 3.10 while `requires-python` says 3.11 `` is cheap because it names
both sides; *"this contradicts the setting above"* is the same defect and expensive.
Re-check at the end of Step 5 once the wording exists.

Survivors are ordered `critical` first. There is no other label — `confidence` was
removed because after the verifier runs, everything surviving is high-confidence by
construction, so the column never discriminated.

Severity orders the output and nothing else. It never changes how a comment is
worded (`reference/voice.md`).

Rules that still apply at this stage:

- **If there are not enough real issues, produce fewer.** Padding produces exactly
  the vacuous filler that reads as machine-generated. If a PR is genuinely clean,
  say so and post nothing.
- **≤ 2** cosmetic comments (stale years, a constant that disagrees, a naming
  inconsistency) — all `no_critical` by definition.
- **1–2** positive remarks when genuinely warranted, **suppressed entirely if
  anything `critical` was found**. Nobody compliments the styling on a PR with a
  security hole.

## Step 5 - Voice phase

> **Never delegate this step.** Not to a sub-agent, not split across two passes.
> Everything below is a property of the comment set *as a whole* — the register mix,
> the clustering, the sentence-shape variety, the cosmetic quota. An agent holding
> one shard cannot honour any of them, and the failure is invisible per-comment and
> glaring in aggregate.

Read `reference/voice.md` now. Summary of the binding rules:

- Every full-sentence comment **ends in a question mark**. Declarative multi-clause
  statements are banned.
- Or use a lowercase fragment of 2–6 words with no terminal period.
- **Tone never escalates with severity.** Command injection is raised as politely as
  a missing try/catch.
- One thought per comment. No causal chains, no prescriptions stacked on diagnoses,
  no line numbers in prose.
- Banned: bold labels, severity prefixes, emoji, headers, bullets, `suggestion`
  blocks, "Consider …", "It would be better to …".
- Backticks on identifiers and paths.
- **No crafted payloads, and no claim resting on code you didn't read** — the proof
  tell and the depth tell. Write the comment you would have written *before* doing
  the work, not after.
- **Name the referent, and carry the verification path.** A comment must say what it
  points at, and if it depends on anything off the anchored line it must name that
  and say where. `old version still holds the value` is unusable; `Does the previous
  version get destroyed anywhere?` is not.

Aim for ~60% Register A / 40% Register B.

Findings arrive from workers as blunt technical prose (`user-controlled filename is
interpolated into os.system()`). That is raw material, not a draft. Rewrite every
one from scratch against the corpus.

**Severity must not leak into the wording.** A `critical` comment is not sharpened
and a `no_critical` one is not softened. The user reads the ordering; the PR author
reads only the comment, and it has to look the same either way.

### Distribution matters as much as wording

- **Cluster on the substantive files.** Uniform coverage — one comment per file,
  evenly spread — is a machine signature. Real reviewers dwell where the risk is and
  skim the rest. Fanning out by file makes this *worse* by default, since every lane
  returns findings: when you select, deliberately let some lanes contribute nothing.
- Do not let two comments share a sentence shape. Three comments opening with
  `It seems` is a tell.
- Vary length. A review where every comment is one line, or every comment is two
  sentences, looks generated.
- Cross-reference in Register B when the same issue recurs (`same issue as above`)
  rather than restating it.

**Before leaving this step**, re-check the `cheap` label against the wording you
actually wrote (Step 4), and run the cold-read test from `reference/voice.md` on
every comment: anchored line ±10 plus the comment text, nothing else — can you tell
what it refers to and how you'd check it? If not, rewrite or drop it.

## Step 6 - Present, take a decision

Generate the report, print the summary, then **stop and wait.**

The report goes to `~/Documents/agents/pr-reviews/`. Don't ask — that folder is the
destination unless the user names another path in the same breath.

```bash
python3 "$SKILL_DIR/scripts/build_report.py" \
  --candidates /tmp/pr-<N>-candidates.json --repo <owner/name> --pr <N> \
  --out-md ~/Documents/agents/pr-reviews/pr-<N>-review.md
```

That writes one markdown report — **summary table first, detail sections below** —
carrying the code window beside every comment, which is the whole point: the user
must be able to run the check without opening a file.

`--out-csv` still exists for spreadsheet triage but is off by default; the CSV held
the same rows as the markdown tables, so writing both left two copies of one report
on disk.

### Present the set

One table, ordered `critical` first, then by path. Every row carries the source line
it is anchored to, so the user can settle it without opening a file.

```
16 comments ready (2 critical), ~14 min to post.
3 more findings are un-anchorable — see below.
47 findings dropped as not cheaply verifiable.

Report: ~/Documents/agents/pr-reviews/pr-2373-review.md
```

**Recommend, don't decide.**

- Don't propose "criticals only" by reflex. A review that is entirely critical
  security findings is itself implausible; real ones mix a sandbox escape with a
  stale Python version.
- Point out anything you are unsure of rather than burying it. The user cutting a
  comment costs nothing; a bad comment posted costs their credibility.

**Then apply the size budget from Step 2.** If the set exceeds the cap, trim and say
exactly what was dropped. Never trim silently.

#### Must-post findings

**H48** — an `ownership.team` naming a company, a filler word, or the author's own
GitHub handle — is exempt from the budget and from the "is it worth a slot?" cut.
It survives trimming even on a PR already at its cap.

It is the only field in the repo where a wrong value passes every deterministic
check: the schema asks for a non-empty string and stops there. Nothing downstream
notices, and the recipe ships with no reachable owner. So the comment goes out
every time the checker fires, and when `manifest.yaml` is outside the diff hunks
it goes in the un-anchorable top-level comment rather than being dropped.

Ask who maintains the recipe. Never propose a team name — the same reason as H17.

Never post without explicit approval, and let the user cut or reword anything. A
wrong comment posted in their name costs their credibility with the PR author, so
this step is where they take ownership of the review as their own.

**Record what they cut**, right after they choose and before posting:

```bash
python3 "$SKILL_DIR/scripts/rejections.py" --record \
  --repo <owner/name> --pr <PR> \
  --candidates /tmp/pr-<PR>-candidates.json \
  --approved /tmp/pr-<PR>-comments.json
```

`--candidates` is everything drafted, `--approved` is what they said yes to; the
difference is the rejection. **Pass the matching pair from this run** — mismatched
files record the wrong set and poison the ledger.

Step 1's suppression only sees comments that were *posted*. One the user read and cut
left no trace on GitHub, so without this it returns on the next review and has to be
rejected again — which happened three times on the #2373 re-review. The verifier
loads the ledger automatically; nobody has to remember to pass it.

### Un-anchorable findings

If the `unanchorable` bucket is non-empty, present it as its own short table and
offer **one** top-level issue comment covering all of them:

```
Un-anchorable (3) — real, but the lines aren't in any diff hunk:
  pyproject.toml:69   [tool.ruff] tables        CI-FAIL
  pyproject.toml:38   requires-python >=3.10    CI-FAIL
  pyproject.toml:2    name != folder basename   CI-FAIL

Offer as one top-level comment?
```

This is the one permitted exception to "no summary comment" (Step 7). Keep it to a
bulleted list of the findings, with no preamble about the review as a whole and no
restatement of the inline comments.

Order them **CI-failures first** — an author will act on "this blocks the build"
and may not act on a convention nit. Mark advisory items as such, and never claim
CI will fail when `.github/review-rules.md` says the check is a `::notice`.

## Step 7 - Post

Write the approved set to JSON and dry-run it first — blocking, it's fast, and it is
the gate:

```bash
python3 "$SKILL_DIR/scripts/post_comments.py" \
  --repo <owner/name> --pr <PR> --file /tmp/pr-comments.json --dry-run
```

It validates every line against the diff hunks and fails loudly if a line isn't
addressable. Then run the real thing **as a background job** and hand control back:

```bash
python3 "$SKILL_DIR/scripts/post_comments.py" \
  --repo <owner/name> --pr <PR> --file /tmp/pr-comments.json
```

```
Posting 9 comments, ~8 min. Running in the background - you'll be notified when
it's done, so carry on with whatever you like.
```

Do not sleep-and-poll it. You get a completion notification; `tail` the job only if
the user asks how far along it is.

The script posts each comment individually, in file order, sleeping a random 10–20s
between them. **Do not shorten the gaps** — the pacing is the point
(`reference/rationale.md`). If it fails partway, the `.posted` state file records
what landed; re-run the identical command to resume.

**A pending review blocks everything** — GitHub allows one per user and this endpoint
implicitly opens one, so every comment 422s. The script preflights and stops with
instructions. Do **not** submit or discard their pending review for them: submitting
posts publicly under their name and may carry a verdict; discarding destroys drafts
they wrote. Surface it and let them choose.

**No verdict and no summary comment.** Do not approve, do not request changes, do not
leave a top-level recap. The user handles the verdict themselves.

The single exception is the un-anchorable list from Step 6, when the user approves
it — a bulleted list of findings whose lines are in no diff hunk. Not a recap: it
says nothing about the review as a whole and repeats no inline comment.

comments.json format:

```json
[
  {"path": "src/api.py", "line": 42, "body": "missing await here"},
  {"path": "src/api.py", "line": 88, "body": "Can we use `subprocess.run()` with a list here?"}
]
```

`line` must be a line present in the diff. Add `"side": "LEFT"` to comment on a
deleted line.

## Progress reporting

The user cannot see analysis happening. On a large PR the gap between "review this"
and the draft is minutes, and the post run is minutes more. Fill it.

**Todo list**, seeded in Step 2 and kept current — this is the live progress bar:

```
- Gather PR + existing comments
- Plan lanes and budget
- Lane 1 analysis (4 files, 320 lines)
- Lane 2 analysis (6 files, 295 lines)
- ...
- Consistency lane
- Merge, dedupe and label findings
- Draft comments in voice
- Present table and await approval
- Post (background)
```

Mark each done as it lands, not in batches. On a small PR collapse the lane items
into a single "Analyse changed files".

**Checkpoints.** Four short messages, no more. Prose, not tables.

1. *After the plan* (Step 2) — the mode/scope question, then size, skips, budget,
   lane count, and the promise that nothing posts without approval.
2. *After analysis* (Step 3/4) — the funnel, so the shape of the cut is visible:
   ```
   Analysis done. 74 findings across 12 lanes. 3 rejected by the verifier,
   25 un-anchorable, 26 dropped as not cheaply verifiable or style.
   20 candidates, drafting now.
   ```
3. *At the decision* (Step 6) — the table, the un-anchorable list, the report path,
   the ETA.
4. *At posting* (Step 7) — backgrounded, ETA, notification promise.

`batch_task` does not stream partial results, so nothing arrives between checkpoints
1 and 2. That makes checkpoint 1 load-bearing: it must state that analysis takes a
few minutes, or the silence reads as a hang.

If anything degrades — a lane fails, the diff is unfetchable, the PR is closed, every
file is skipped — say so immediately rather than quietly working around it.
