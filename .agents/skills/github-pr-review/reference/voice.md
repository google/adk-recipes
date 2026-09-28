# Voice calibration — the general review style

The house style for review comments produced by this skill. It is calibrated on two
things: a rated corpus of real review comments on `google/adk-samples`, and the
recorded accept/reject outcome of 20 drafted comments on PR #2373 — live decisions,
not a rating exercise.

Ratings below mean **strong** (on-style), **weak** (recognisably off), or
**rejected** (do not produce this shape).

This file is the single source of truth for how a comment must read. When in doubt,
match an example below rather than inventing a new shape. It is also the file to
replace if a team wants to shift the style — everything else in the skill is
independent of it.

---

## The two registers

Every comment is either Register A or Register B. There is no third option.

### Register A — full sentence, ends in a question mark

Proper capitalisation and punctuation. **Must end in `?`.** Often opens with a hedge.
May carry one short supporting fact before or after the question.

Hedges that tested well: `It seems`, `Perhaps`, `I think`, `I see`, `Is there a reason`,
`Can we`, `Can you please`.

```
Can you please add error handling around this call?
It seems the lock is released before the write finishes. Is that intentional?
Is there a reason we're not reusing `get_client()` here? It seems like it would do the same thing.
```

### Register B — lowercase fragment

2–6 words. No leading capital. No terminal period. Used for obvious defects and for
cross-referencing another comment.

```
missing await here
same issue as above
this'll break if the list is empty
```

Target mix: roughly **60% Register A, 40% Register B**.

---

## The first filter: anchor on a fact

> **A comment must point at something observable at the line. If it asks a question,
> the question is about what to DO with that fact — never about establishing the
> fact by reasoning.**

This is the strongest predictor in the whole file, and unlike everything else here
it comes from live accept/reject decisions rather than a rating session. On PR #2373
it separates all 16 accepted comments from all 4 rejected ones — 16 for 16.

**Rejected — each one asks the reader to derive something:**

| | Comment | What it demands |
|---|---|---|
| R1 | `Is 200 characters enough to stay clear of the keys? The fixed prefix looks like about 64.` | do the arithmetic |
| R2 | `` `DENN` and `CHAL` break the pattern the rest of the map follows `` | infer a pattern across a map |
| R3 | `` Would a failed call pass this? `error` is in the accepted list. `` | reason about test semantics |
| R4 | `Should this `chdir` be restored afterwards?` | reason about consequences |

**Accepted — each one points at something on screen:**

| | Comment | The fact |
|---|---|---|
| A1 | `hardcoded project name here` | the literal is right there |
| A2 | `` `city_clean` isn't used below `` | assigned, never read |
| A3 | `` this `f` has nothing to interpolate `` | an f-string with no braces |
| A4 | `no licence header on this one` | the top of the file |
| A5 | `looks like three files got concatenated` | mid-file copyright comments |
| A6 | `` Is falling back to `TX` right here? The label still says the requested state. `` | the fallback literal, plus the label two lines down |
| A7 | `The padding appends the same query over and over, but the banner below calls them unique. Which is right?` | two visible statements that disagree |
| A8 | `Does `$DEPLOY_CMD` still have the key values in it at this point?` | a question the author answers from what they built |

Note A6–A8: a question is fine, and so is a supporting clause. What matters is that
the **fact is visible** and the question is about its consequence or intent — not a
request for the reader to work the fact out.

**The test:** delete the question from your comment. Is what remains a statement of
something visible at the line? If nothing remains, or what remains is an inference,
the comment fails.

## The hard rule

> **A full-sentence comment must end in a question mark.
> A declarative multi-clause statement is never acceptable.**

This single rule accounts for almost every rejection in the calibration set. Compare:

| | Comment | Verdict |
|---|---|---|
| 18 | `The lock is released before the write finishes.` | weak — declarative, no question |
| 19 | `It seems the lock is released before the write finishes. Is that intentional?` | **strong** |
| 20 | `It seems the lock is released before the write finishes, which means two concurrent callers could interleave and corrupt the file. Perhaps move the release after the flush?` | **rejected** — too dense |

18 → 19 → 20 is the density ruler. **19 is the target.** 18 is under-committed
(states a fact but asks nothing). 20 narrates the full causal chain and prescribes a
fix — that is the single most common AI-review failure mode.

**One thought per sentence.** Do not explain the mechanism, the consequence, and the
remedy in one breath. That is what makes 20 fail — a single sentence doing three
jobs, not the presence of a suggestion.

### A remedy is welcome — as its own sentence

This is a correction to the rating session, made from live evidence. Of the 16
comments accepted on PR #2373, **four were edited before posting, and all four edits
added a suggested fix**:

| Drafted | Posted |
|---|---|
| `the licence header stops mid-sentence` | `…mid-sentence. I suggest cleaning up all the headers in a more consistent form. You may borrow from other existing recipes under core/ if you wish.` |
| `same default as the advisor module` | `…advisor module. You may add these default values to .env.example` |
| `Should this endpoint come from an env var rather than being pinned here?` | `…pinned here? It also has hardcoded project and other things. Please clean up.` |
| `real project id committed here` | `Real project ID is committed here.` |

So: **observation first, then a short plain sentence saying what to do.** Not
"Consider…", not "It would be better to…" — the shapes above: *"I suggest…"*, *"You
may…"*, *"Please…"*.

The distinction from 20 is structural, not topical:

- **20 (rejected):** `It seems the lock is released before the write finishes, which means two concurrent callers could interleave and corrupt the file. Perhaps move the release after the flush?` — one sentence carrying mechanism *and* consequence, then a fix.
- **Accepted shape:** `the licence header stops mid-sentence. I suggest cleaning up all the headers in a more consistent form.` — a fact, then a remedy. Two sentences, one job each.

Omit the remedy when the fix is a judgement call about how to restructure something.
Include it when it is obvious, or when it points at an existing example to copy.

The fourth edit is a smaller signal: a comment that reads as a full clause gets
capitalised and given a full stop. `no backend block here` stayed lowercase;
`Real project ID is committed here.` did not. Fragments stay fragments; clauses
become sentences.

### Never assert an absence you did not inventory

A claim that something is **missing** must say where you looked, or where it *does*
appear. This rule exists because of a comment this skill posted and had to publicly
correct:

> **Wrong:** *"`load_dotenv()` is only in `tests/` and `eval/`."* — asserted from a
> truncated `grep | head -6`. It was in fact called from four package modules, and
> the PR author would rightly have replied "it's right there in `agent.py`".
>
> **Right:** *"`load_dotenv()` is called from `agent.py`, `real_estate_advisor.py`,
> `universal_whitepaper_orchestrator.py` and `dynamic_search_harvester.py`, but not
> from `economic_research/__init__.py`."*

An absence is the one claim you cannot verify by looking at the anchored line, so it
must carry its own evidence.

---

## Say what you are pointing at

Brevity is not the same as being elliptical, and that difference separates
`missing await here` (**strong**) from `prefix ends up as just bash` (unusable). Both
are short. The first describes something on the anchored line and is therefore
complete. The second is a short comment about a *distant mechanism* — the worst
combination available: too terse to explain itself, too remote to check.

Three rules, and they bind harder than the register mix.

### Name the referent

Every comment must name the identifier, setting or directive it is about. A bare
demonstrative — "this", "the prefix", "the setting above" — is allowed only when the
thing is unambiguous **on the anchored line itself**.

| | Comment | Verdict |
|---|---|---|
| N1 | `different user ids can collide here` | unusable — collide into what? |
| N2 | `Could two different user ids end up with the same sanitised value here?` | **strong** |
| N3 | `old version still holds the value` | unusable — which version? |
| N4 | `Does the previous version get destroyed anywhere, or does the old value stay readable?` | **strong** |

### Carry the verification path

If the comment depends on anything not on the anchored line, **name it and say
where**. The rated corpus already does this, and it is what makes a comment checkable
rather than merely assertive:

- `I see timeout is set to 5 here but 30 in config.py. Which one is correct?`
- `Perhaps this should be user_id instead of userId? The rest of the file uses snake_case.`

Both tell the reader exactly where to look. Compare `this contradicts the setting
above` — the same finding with the path removed, made expensive to verify purely
through wording.

### Assertions must be certain; questions need not be

State a defect flatly **only** when it is visible on the anchored line. Anything
requiring inference goes in question form.

This is a cost rule, not a politeness one. `different user ids can collide here` is a
claim that is embarrassing if wrong. `Could two user ids collide here?` costs nothing
if the answer is no — the author replies "no, because X" and everyone moves on. Bare
Register B fragments are assertions, which is precisely why they are reserved for
things plainly on screen.

### The cold-read test

Before presenting any comment, look at **only** the anchored line ±10 and the comment
text. Can you say what it refers to, and how you would check it? If not, rewrite or
drop it. Every comment, not a sample.

---

## Plausibility — could the reviewer have known this?

Everything above governs how a comment *sounds*. This section governs whether a
human could plausibly have *found* it. A comment can satisfy every rule in this
file and still be obviously machine-generated, because the giveaway is not the
wording — it is how much the commenter would have had to know.

Before writing any comment, ask: **reading these files, would a person have
arrived here?** If the honest answer is no, the comment is unpostable at any level
of polish.

### The depth tell

The finding is unreachable from the code the reviewer read. It needed a
dependency's internals, a repo-wide grep, or a cross-file comparison nobody makes
while reading a diff.

```
IdentityMiddleware resolves the caller but doesn't compare it to the {user_id}
in ADK's session routes. Should it?
```

Correct, serious, and impossible. Knowing that ADK's `ApiServer` route table
carries `{user_id}` requires reading `api_server.py` inside the installed package.
Ask what you could actually wonder instead:

```
Does this cover ADK's own session routes too, or only ours?
```

Same line, same defect, same outcome once the author looks. No claim to knowledge
the reviewer never had.

### The proof tell — the worse one

The comment quotes a crafted input. **You only cite a working bypass if you built
and ran one.** A reviewer has a suspicion, not a proof of concept.

| | Comment | Verdict |
|---|---|---|
| P1 | `The \`sudo\` branch above skips past the flags but the \`env\` branch doesn't. Does \`env -i rm -rf /\` still classify?` | rejected — payload proves it was executed |
| P2 | `The \`sudo\` branch above skips past the flags but this one doesn't. Is that deliberate?` | **strong** |
| P3 | `Perhaps resolve the path before the \`.agents/skills/\` check? \`x/../../../evil.sh\` gets through as it stands.` | rejected — "gets through as it stands" asserts a verified traversal |
| P4 | `Does \`_is_skills_path\` resolve \`..\` before it checks the prefix?` | **strong** |

Banned outright: exploit strings, payloads, "I tried X and got Y", any phrasing
that reports the result of running the code.

### Cheap to verify

Separate from whether a comment is *true* is what it costs the author to find out.
That cost is the dominant variable, because of an asymmetry:

- cheap and wrong — the author glances, says no, loses five seconds. Harmless.
- cheap and right — the author sees it immediately and fixes it. Ideal.
- **expensive and wrong** — the author burns twenty minutes, finds nothing, and
  discounts everything else you wrote. The worst outcome available.
- expensive and right — a real bug, bought at twenty minutes and some goodwill.

A reviewer who is 80% accurate but always cheap is a pleasure. A reviewer who is
80% accurate and always expensive is a liability. **Optimise the cost, not the
hit rate.**

> **Cheap-to-verify:** looking only at the ~10 lines around the anchor, plus at
> most one in-file lookup the comment explicitly names, the author can settle the
> comment
>
> - without inventing an input, value or scenario the code does not already name,
> - without relying on any library, framework or service behaviour not visible there,
> - in one pass, without taking notes.
>
> A minute is the sanity check, not the criterion. If it takes longer, one of the
> three conditions above was already broken — find which.

**Score the final comment text, not the underlying finding.** The same defect can
be cheap or expensive depending on how it is written:

| | Comment | Cost |
|---|---|---|
| C1 | `` I think `ty` is on 3.10 here while `requires-python` says 3.11? `` | **cheap** — names both sides, so the one lookup is free |
| C2 | `this contradicts the setting above` | expensive — same defect, but the reader has to hunt |
| C3 | `` The `sudo` branch above skips past the flags but this one doesn't. Is that deliberate? `` | **cheap** — both branches on screen, claim is the asymmetry |
| C4 | `prefix ends up as just `bash`` | expensive — the proof is in another file |

The trap: **everything can be on screen and the comment still expensive.** A
four-line function is fully visible, but if settling the comment means inventing
two inputs and running them through a regex in your head, it is not cheap. The
test is not "can I see the code" — it is "must I supply anything the code does
not already name".

Conditions written in the code are free to reason about (`if allowlist:` invites
you to consider an empty allowlist). Conditions you bring yourself are not.

### The question mark is epistemic, not decorative

The hard rule above says a full sentence ends in `?`. That rule exists because the
reviewer is genuinely asking. **A question mark applied to something you have
already verified is a lie about your own certainty**, and the confidence leaks
through the surrounding phrasing every time — in the specificity, in the
supporting clause, in the crafted example.

If you know the answer, you are not asking a question, and the comment needs to be
rewritten around what you could actually have suspected. This is the single most
useful test in this file: **write the comment you would write before doing the
work, not after.**

---

## Severity does not change tone

The most counterintuitive finding, and the easiest one to get wrong.

The user handles command injection with exactly the politeness used for a missing
try/catch. Severity changes **what you point at**, never **how you sound**.

| | Comment | Verdict |
|---|---|---|
| 13 | `This is user input going straight into a shell command. Can we use `subprocess.run()` with a list instead?` | **strong** |
| 14 | `this is a command injection` | weak |
| 15 | `I don't think we can ship this one — `filename` comes from the request and goes straight into `os.system()`.` | weak |
| 16 | `Command injection here. `filename` is user-controlled.` | rejected |
| 17 | `Is `filename` validated anywhere upstream? If not this is a command injection.` | rejected |

**13 is the model for critical findings**: plain observation of what the code does,
then the concrete fix offered as a `Can we …?` question.

Do not use blocking language (`I don't think we can ship this`), and do not lead with
a severity noun-phrase (`Command injection here.`). 17 shows that even a question
fails if it is a conditional accusation.

---

## Banned outright

Every one of these appeared in a rejected example or contradicts the rules above.

- Bold labels and severity prefixes — `**Critical:**`, `Security issue:`, `nit:` is borderline (8 rated only weak)
- Emoji of any kind. The user writes `:-)`, never 🔴/✅/⚠️
- Markdown headers, bullet lists, numbered lists inside a comment
- ` ```suggestion ` blocks (24 rejected)
- `Consider …`, `It would be better to …`, `I recommend …` — note these are banned as
  *phrasings*, not because suggesting a fix is banned. `I suggest …`, `You may …` and
  `Please …` are all attested in real posted comments.
- `… to improve readability and maintainability` and similar abstract-quality prose
- Referencing line numbers in prose (`before line 42`) — the comment is already anchored to a line
- Multi-clause causal explanation in a single sentence (see 20). A remedy in its own
  sentence is fine — see "A remedy is welcome".
- Any comment whose fact must be derived by the reader rather than seen (R1–R4 above)
- Asserting something is missing without saying where you looked
- Polished cross-references (26 rejected: `Related to my comment above about the timeout.`)
  Use Register B instead (22: `same issue as above`)
- Crafted payloads and exploit strings — see the proof tell above
- Any claim that rests on code the reviewer did not read — see the depth tell above

---

## Formatting habits

- Backticks around every identifier, path, and filename: `` `user_id` ``, `` `config.py` ``
- `:-)` occasionally, only on positive or social comments, never on a defect
- No sign-offs, no greetings, no "Thanks for the PR!"

---

## Full rated corpus

Round 1 — general shapes.

| # | Comment | Verdict |
|---|---|---|
| 1 | `Can you please add error handling around this call?` | **strong** |
| 2 | `**Critical:** This introduces a race condition. Consider using a mutex to guard access to the shared counter.` | rejected |
| 3 | `Perhaps this should be `user_id` instead of `userId`? The rest of the file uses snake_case.` | **strong** |
| 4 | `this'll break if the list is empty` | **strong** |
| 5 | `I think this will throw if `results` comes back empty. Should we guard against that?` | weak |
| 6 | `Consider refactoring this function to improve readability and maintainability. It currently handles multiple responsibilities.` | rejected |
| 7 | `Is there a reason we're not reusing `get_client()` here? It seems like it would do the same thing.` | **strong** |
| 8 | `nit: typo in "recieve"` | weak |
| 9 | `I see `timeout` is set to 5 here but 30 in `config.py`. Which one is correct?` | **strong** |
| 10 | `🔴 Security issue: this SQL query is vulnerable to injection.` | rejected |
| 11 | `This looks like it would leak the connection if the exception fires before line 42. Perhaps wrap it in a `try/finally`?` | rejected |
| 12 | `The reason we avoid `eval()` here is that the input comes straight from the request body. Can we use `json.loads()` instead?` | weak |

Round 2 — severity register, density, fragments, suggestions, cross-refs.

| # | Comment | Verdict |
|---|---|---|
| 13 | `This is user input going straight into a shell command. Can we use `subprocess.run()` with a list instead?` | **strong** |
| 14 | `this is a command injection` | weak |
| 15 | `I don't think we can ship this one — `filename` comes from the request and goes straight into `os.system()`.` | weak |
| 16 | `Command injection here. `filename` is user-controlled.` | rejected |
| 17 | `Is `filename` validated anywhere upstream? If not this is a command injection.` | rejected |
| 18 | `The lock is released before the write finishes.` | weak |
| 19 | `It seems the lock is released before the write finishes. Is that intentional?` | **strong** |
| 20 | `It seems the lock is released before the write finishes, which means two concurrent callers could interleave and corrupt the file. Perhaps move the release after the flush?` | rejected |
| 21 | `missing await here` | **strong** |
| 22 | `same issue as above` | **strong** |
| 23 | `is this still needed?` | **strong** |
| 24 | ` ```suggestion ` block | rejected |
| 25 | `Same as the one in `utils.py` — probably worth fixing both.` | weak |
| 26 | `Related to my comment above about the timeout.` | rejected |

---

## Authentic comments from the real corpus

Verbatim, previously posted by the user. Highest-fidelity reference available.

```
Can you please add a real team here?
```

```
Perhaps you want to change these 2025 years to 2026?
```

```
I see. The reason we introduced `team` was to make sure no sample/recipe will become
orphaned (since we had to deal with a number of them before). We wanted to make sure a
team owns the recipe in case the poc leaves. If you think you will be able to remain
the owner and continue to maintain it, we can accept you as the team.
```

```
Oh okay. Sounds good. I will approve your PR.
Congratulations on submitting the very first recipe to our newly restructured contrib/python by the way.   :-)
```

Note the third example: longer explanatory prose **is** in range when the user is
justifying a policy in a back-and-forth reply. That is a reply register, not a
first-pass review register. A first-pass review comment stays short.

Note also `Perhaps you want to change these 2025 years to 2026?` — a stale copyright
year. Confirms that small cosmetic findings are genuinely in scope, in strict
moderation (see the quota in SKILL.md Step 4).
