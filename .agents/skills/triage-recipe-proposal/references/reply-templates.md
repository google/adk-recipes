# Reply templates

Exact wording derived from real closing comments. Replace `<slot>` with the specific detail from the proposal. Keep the brevity — Pier's replies are typically 2–5 sentences.

---

## ACCEPT — standard

Based on `#2717` (`@pierpaolo28`), with Agent Garden coordination from `@kgu-here`.

```
Thanks for the proposal — this fills a real gap in <coverage area> and the
<GCP service / ADK pattern> framing is a good fit for `contrib/`. Approved
from my side.

We can work together on meeting the onboarding standards. Feel free to start
the PR and have the first review with @happyhuman. Please leave a link to
the PR here as a reference.
```

Add before the closing paragraph **only when the recipe has clear customer value AND an Agent Garden angle is plausibly in scope**. For customer-facing proposals this is a parallel coordination cc — informational, not a PR blocker. Phrase it as a specific decision ask so Katherine knows what to weigh in on.

```
cc @kgu-here for Agent Garden coordination — curious whether <specific angle of
this recipe> fits the surfacing criteria. No block on the PR; just want your read.
```

**If the recipe is technically clean but customer value is unclear** (infra / plumbing / safety / developer-tool patterns with no customer-facing surface), do NOT accept and cc her — instead use the **DEFER TO @kgu-here** template below. Katherine's AG assessment is the decision, not a parallel coordination note.

---

## DEFER TO @kgu-here — technically clean, customer value unclear

Use when none of A–G fires but the recipe lacks obvious customer-facing surface (infra / plumbing / safety / developer-tool patterns). Katherine decides whether to accept or close, not Pier. Based on `#2769` (authority-revocation plumbing).

```
Thanks for the <rescope | proposal> — this is <well-scoped | addresses the
<prior concern from #NNNN> cleanly>.

Before accepting, I want to defer this to @kgu-here. <The pattern> is
technically clean, but if there isn't a clear customer-facing / Agent Garden
angle for it, we don't think it earns its place in `contrib/` on infra merit
alone.

@kgu-here — could you weigh in on whether <one-sentence description of the
recipe> is something you'd want to surface in Agent Garden (e.g.
<specific audience: enterprise security / compliance / finance teams>)?
If yes, I'll approve and route the PR; if the use case doesn't resonate,
I'll close.
```

Leave the issue OPEN until Katherine responds. On her "yes", follow up with the standard ACCEPT template. On her "no" (or silence past a reasonable window), close with the Gate G / coverage-gap template, referencing her read.

---

## DECLINE — Gate A, not actionable (empty body)

Based on the closing comments on `#2685` / `#2684`.

```
Thanks for opening this. The proposal body is empty — every template
question is unanswered, so we don't have enough to triage.

Closing as not actionable. If this was submitted by mistake, please open a
new issue with the template filled in (purpose, language, deployability
plan, dependencies, maintainer).
```

Then `gh issue close <num> --repo google/adk-recipes --reason "not planned"`.

---

## DECLINE — Gate C, not deployable

Based on `#2683` (Model Armor SDP) and `#2651` (first oncall-authority attempt).

```
Thanks for the detailed proposal. Declining this one: the Recipe Checklist
§1 requires every `contrib/` recipe to be deployable (root `Dockerfile`
and `deployable: true` in `manifest.yaml`), and the proposal says the
initial scope would not include a Dockerfile.

Happy to revisit once a deployable path is in scope.

Closing as not planned.
```

---

## DECLINE — Gate D, scope fit (outside GCP enterprise)

Based on `#2705` (crypto-payroll). Note the multi-reason structure — this one legitimately combined gates D and E, so use it as the pattern when the decline really does rest on two reasons.

```
Thanks for the thorough proposal<, and for the upfront disclosure around
<vendor>>.

After review, this is a decline for `contrib/`, for the following reason<s>
taken together:

1. **Scope fit.** `adk-recipes` is centered on Google Cloud enterprise
   agents (BigQuery / Vertex / Cloud Run, retail, support, operations).
   <The crypto / L2 / non-GCP infra> primary subject sits outside that
   center, even though the ADK integration itself is clean.
2. <optional: vendor-dependency reason — see Gate E template>.

Closing as not planned.
```

---

## DECLINE — Gate E, vendor dependency as primary pattern

Based on `#2752` (session-dollar-budget).

```
Thanks for the proposal. Declining this one — the recipe is designed around
a single external <gateway / vendor> dependency maintained by the author,
which we aren't able to curate into `contrib/` as the primary pattern.

<Optional: if the underlying ADK pattern is genuinely interesting, add>
If you'd like to propose a vendor-neutral version of the <plugin lifecycle /
callback discipline / X> pattern that doesn't require your gateway as the
backbone, we'd be open to that.

Closing as not planned.
```

---

## DECLINE — Gate F, scope overlap

Based on `#2664` (overlaps `#2658`).

```
Thanks @<login> — <PR #NNN is quality work | the proposal is well-scoped>.
Declining not on execution but on scope overlap: #<existing-num>
(`<existing-recipe-name>`) is already covering <the overlapping surface>,
and we'd rather not carry two parallel recipes in that space.

If you want to still contribute, we have the standard contrib/ lane open for
<complementary idea>, happy to help scope that if you're interested.

Closing as not planned.
```

---

## DECLINE — Gate G, differentiation / coverage-gap

Based on `#2755` (VirusTotal MCP) and `#2658` (gcp-mcp-finops).

```
Thanks for the <detailed> proposal. After review, we're not moving forward
with this one. <The MCP connection and tool-allowlisting patterns the
recipe would demonstrate | The underlying agent-on-hosted-BigQuery-MCP
pattern> aren't sufficiently differentiated to carry a dedicated recipe at
this time<; the current repo prioritization favors recipes that fill
coverage gaps not already surfaced by existing Google Cloud and ADK
documentation>.

Closing as not planned.
```

---

## REDIRECT — Gate B, core/ scope

```
Thanks for proposing this. The `[RECIPE PROPOSAL]` template is for
`contrib/` recipes; `core/` recipes are curated by the agents-cli team
through a separate internal process.

Closing here — if you'd like this considered for `core/`, reach out through
the agents-cli channel directly, or re-scope the proposal as a `contrib/`
submission.
```

---

## PARTIAL ACCEPT — soft-gate adjustment

Use when the idea has merit but one soft gate (F or G) fires at the edges. Based on how the `#2619` thread discussed "fills a real gap but community lane effectively closed".

```
Thanks for the proposal. The <graph retrieval / X> angle fills a real gap,
but as-scoped it <overlaps with #NNN | sits very close to existing coverage>.

If you're open to re-scoping to <focus narrower on Y / de-emphasize Z>,
I'd approve that version. Otherwise, I'll park this for now.

Marking as needs-info — please reply with whether a re-scope works.
```
