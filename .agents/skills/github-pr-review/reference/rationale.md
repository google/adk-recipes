# Why these rules exist

Kept out of `SKILL.md` because that file loads on every invocation and this is
background. Read it when a rule looks arbitrary and you are tempted to skip it.

## The house-rules lane reads files outside the diff

Churn-based partitioning cannot see a rule violation on an unchanged line. On PR
#2373 three hard CI failures sat at lines 2, 38 and 69 of a `pyproject.toml` whose
diff touched only lines 30-36 and 112-125. A twelve-lane review missed every one.

## Rules are a script wherever they can be

When the 25 house rules were run by hand, two nearly became false positives —
`source = { editable = "." }` is the recipe's own package, and `license`/`tags` turn
out to be permitted manifest keys — and one **was posted wrong**: an absence
asserted from a truncated `grep | head -6`, which required a public correction.

## The window doubles as a hallucination detector

Findings quote their source verbatim, so it can be diffed against the file. On #2302
that cleared 132 of 134 findings and caught the two that were fabricated. A lane that
invents a finding invents the source line with it.

## Addressability is checked before drafting, not at the gate

GitHub only accepts a comment on a line inside a diff hunk. On #2373 a third of all
findings sat outside every hunk. Discovering that at the posting gate means having
already drafted, and become attached to, comments that cannot go up.

## Comments are posted one at a time, slowly

`POST /pulls/{n}/comments` is what GitHub creates when a person types a comment on a
line. The 10-20s gaps exist because a burst of nine comments in ten seconds is the
single most obvious tell available. A pending review blocks all of it: GitHub allows
one per user, and the endpoint implicitly opens one.

## Where the voice rules come from

`reference/voice.md` is calibrated on two things: a 26-example rating session, and —
more importantly — the real accept/reject outcome of 20 drafted comments on #2373.
Where they disagree, the live outcome wins. That is how the ban on suggesting a fix
was overturned: all four comments the user edited before posting had a remedy added.
