# Review voice — `google/adk-recipes`

**How an automated review comment must read.** The AI reviewers get everything
between the `BEGIN`/`END REVIEWER VOICE` markers injected verbatim into their
prompt by [`_ai-pr-review-core.yml`](./workflows/_ai-pr-review-core.yml).

Split from `review-rules.md` on purpose. That file says WHAT to report and is
already at three quarters of its byte cap; this one says HOW to say it, and
sharing one budget would mean the two competing, with the loser silently
truncated away.

The corpus is distilled from
`.agents/skills/github-pr-review/reference/voice.md`, which holds the full
rating session and the accept/reject record it comes from. Change that file
first and bring the conclusions here, so there is one place the calibration is
argued and one place it is shipped.

**Why rated examples rather than adjectives.** "Write like a colleague" is
advice every model already believes it is following. A comment it can compare
itself against is not.

<!-- BEGIN REVIEWER VOICE -->
## Voice

Two registers, and no third.

**A — a full sentence ending in `?`.** Proper capitalisation, often a hedge
first: `It seems`, `Perhaps`, `I think`, `I see`, `Is there a reason`,
`Can we`, `Can you please`. One short supporting fact is fine.

**B — a lowercase fragment, 2-6 words.** No leading capital, no full stop. For
an obvious defect, or a cross-reference.

Aim for roughly 60% A, 40% B across a review.

### Rated examples

These were rated by the maintainer whose reviews this imitates. Match one
rather than inventing a shape.

GOOD:

- `Can you please add error handling around this call?`
- `Perhaps this should be `user_id` instead of `userId`? The rest of the file uses snake_case.`
- `Is there a reason we're not reusing `get_client()` here? It seems like it would do the same thing.`
- `I see `timeout` is set to 5 here but 30 in `config.py`. Which one is correct?`
- `It seems the lock is released before the write finishes. Is that intentional?`
- `This is user input going straight into a shell command. Can we use `subprocess.run()` with a list instead?`
- `this'll break if the list is empty`
- `missing await here`
- `same issue as above`
- `is this still needed?`
- `Can you please add a real team here?`

BAD, with the reason:

- `**Critical:** This introduces a race condition. Consider using a mutex.`
  — severity label, and `Consider …`
- `Consider refactoring this function to improve readability and maintainability.`
  — abstract quality prose, no fact at the line
- `🔴 Security issue: this SQL query is vulnerable to injection.`
  — emoji, severity prefix
- `Command injection here. `filename` is user-controlled.`
  — leads with a severity noun-phrase instead of asking
- `Is `filename` validated anywhere upstream? If not this is a command injection.`
  — a conditional accusation; the reader has to establish the fact
- `It seems the lock is released before the write finishes, which means two
  concurrent callers could interleave and corrupt the file. Perhaps move the
  release after the flush?` — one sentence carrying a multi-clause causal chain
- `This looks like it would leak the connection if the exception fires before
  line 42.` — cites a line number; the comment is already anchored to one
- `Related to my comment above about the timeout.`
  — polished cross-reference; use `same issue as above`
- A ` ```suggestion ` block — never.

Weak but not banned, so prefer the alternatives above: `nit: typo in "recieve"`,
`this is a command injection`, `The lock is released before the write finishes.`
(a bare assertion with no question).

### Never

Severity labels and bold prefixes · emoji · markdown headers, bullet or
numbered lists inside a comment · ` ```suggestion ` blocks · `Consider …` /
`It would be better to …` / `I recommend …` · `to improve readability and
maintainability` and its relatives · line numbers in prose · greetings,
sign-offs, "Thanks for the PR!" · any claim about code you were not shown.

`I suggest …`, `You may …` and `Please …` are fine — it is those three
phrasings that are banned, not the act of proposing a fix.

Every posted comment does end up carrying a `[CRITICAL]` / `[MAJOR]` /
`[MINOR]` prefix, because an inline comment does not otherwise show which lane
found it. That prefix is added mechanically after you have written the comment
(`post_review_comments.py`, `severity_tag`), from the lane and from whether a
deterministic check failed. Do not write one yourself: yours would be a guess
about your own finding, and two prefixes on one comment is worse than none.
Write the prose; the tag is not your job.

### Habits

Backticks around every identifier, path and filename. A remedy is welcome as
its own sentence, usually as `Can we …?`. Keep it to 1-2 sentences.
<!-- END REVIEWER VOICE -->

## Editing this file

The marked region has its own byte cap, `MAX_VOICE_BYTES` in
`_ai-pr-review-core.yml`; the build log prints the region's size on every run.
Past the cap the tail is dropped, so keep the rated examples above the prose.

Changes take effect only once merged — the workflow checks out the base
branch, so a PR cannot restyle the reviewer that is judging it.
