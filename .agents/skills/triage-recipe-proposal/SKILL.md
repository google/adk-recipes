---
name: triage-recipe-proposal
description: Triage a `[RECIPE PROPOSAL]` issue in google/adk-recipes by running the historical accept/decline rubric and drafting the maintainer reply. Invoke when the user shares a proposal issue number or URL, or asks to "triage the open recipe proposals".
---

# Triage a recipe proposal (google/adk-recipes)

You are helping Pier (`@pierpaolo28`) decide **accept** or **decline** on a `contrib/` recipe proposal, and drafting the comment to post. Every decision in this skill is anchored to how prior proposals were actually resolved.

The repo's own automated triage **skips** any issue whose title contains `[RECIPE PROPOSAL]` (see `.github/workflows/ai-issue-automated-triage.yml`), so these are reviewed entirely by hand. That is what this skill accelerates.

---

## 0. Inputs

The user will give you one of:

- a GitHub issue number (`#2766`, `2766`)
- a full URL (`https://github.com/google/adk-recipes/issues/2766`)
- the phrase "the open proposals" / "the triage queue" — then list all open ones first

If multiple proposals are in scope, triage them one by one and produce one draft reply per issue.

## 1. Fetch

Always start from the live issue, not memory:

```bash
gh issue view <num> --repo google/adk-recipes \
  --json number,title,author,createdAt,body,labels,comments,state
```

If the user asked for "the queue", first:

```bash
gh issue list --repo google/adk-recipes --label contribute --state open \
  --limit 50 --json number,title,author,createdAt
```

Also, before deciding on scope-overlap, scan what already exists so you don't re-derive from stale memory:

```bash
# Existing recipes
gh api repos/google/adk-recipes/contents/contrib/python --jq '.[].name'
gh api repos/google/adk-recipes/contents/core/python   --jq '.[].name'
gh api repos/google/adk-recipes/contents/plugins       --jq '.[].name'

# Pending proposals that could overlap
gh issue list --repo google/adk-recipes --label contribute --state open \
  --limit 50 --json number,title
```

## 2. Decision rubric — hard gates first, in order

Walk the gates in order. The **first** gate that triggers determines the outcome. Do not accumulate reasons past the first failing hard gate — the reply template handles only one primary reason.

### Gate A — Template completeness
**Reject-as-not-actionable** if the body is empty, is only the unmodified template boilerplate, or leaves the six template questions unanswered. The template asks: (1) intent, (2) language, (3) deployable Y/N, (4) GCP services / third-party APIs, (5) owner, (6) extras.

- Precedent: `#2685`, `#2684`, `#2611`.
- Use the **not-actionable** reply template below.
- Close with `--reason "not planned"`.

### Gate B — Wrong root (core/ scope creep)
If the proposal is for `core/` rather than `contrib/`, redirect — core/ follows a separate internal process and the template's scope notice explicitly says "`contrib/` recipes only".

- Not a flat decline; comment that core/ goes through a different process and close.

### Gate C — Deployability
**Decline** if the proposal says explicitly that the initial scope will not include a `Dockerfile`, or that it will not be deployable. The Recipe Checklist §1 requires every `contrib/` recipe to have a root `Dockerfile` and `deployable: true` in `manifest.yaml`.

- Precedent: `#2683` (Model Armor SDP), `#2651` (oncall-task-scoped-authority first attempt).
- If the proposer says "I will add the Dockerfile after approval" — that is **fine**, not a decline. See `#2767` for an explicit case of this phrasing.

### Gate D — Scope fit (GCP enterprise agent)
**Decline** if the recipe's primary subject sits outside Google Cloud enterprise agent patterns. The repo is centered on BigQuery / Vertex AI / Cloud Run / Memory Bank / Agent Engine / ADK-native patterns, plus the retail vertical in `plugins/`. Non-GCP infra (crypto rails, L2 blockchains, non-GCP cloud as the backbone) does not fit even when the ADK integration is clean.

- Precedent: `#2705` (crypto-payroll on Base L2).

### Gate E — Vendor-dependency as primary pattern
**Decline** if the recipe's core teaching value depends on a single external SaaS/gateway owned by the proposer, such that the recipe is effectively a tutorial for that vendor's product.

- Precedent: `#2752` (session-dollar-budget — depends on author's external gateway), the vendor-overlap half of `#2705`.
- A hosted provider API is **not** automatically a problem if the ADK-level pattern (plugin lifecycle, callback discipline, refusal semantics) is the thing being taught and the provider is one of several possible backends. See `#2766` as a borderline case to flag rather than auto-decline.

### Gate F — Scope overlap
**Decline** if an existing recipe or an already-accepted/pending proposal covers the same pattern. Overlap means "we don't want two parallel recipes in that space", not "nobody has ever touched BigQuery before".

- Precedent: `#2664` declined because `#2658` already covered agent → BigQuery via hosted MCP; also `#2619` where graph/GraphRAG was fine as a gap but the community lane was effectively closed.
- Suggest to the proposer that they contribute to the existing recipe instead when applicable.

### Gate G — Differentiation / coverage-gap
**Decline** if the pattern is already well-covered by existing GCP documentation, ADK samples, or in-repo recipes, and the proposal does not add a sufficiently differentiated angle.

- Precedent: `#2755` (VirusTotal MCP — MCP + tool-allowlisting not differentiated enough), `#2658` (FinOps on hosted BigQuery MCP — covered by existing GCP docs).
- This is the softest and most subjective gate. If uncertain, surface the decision to the user rather than auto-declining.

### Accept vs. defer to AG

If none of A–G fires, the next question is **customer value**. `contrib/` is a curated gallery, not a dumping ground for well-engineered recipes — every recipe needs a reason for someone to actually want to run it.

**Clear customer value** — vertical / finance / retail / support / customer-facing agent with an obvious end-user scenario → **Pier accepts on his own**. Optionally cc `@kgu-here` for parallel Agent Garden surfacing coordination (informational, not a PR blocker).
- Precedent: `#2717` (Economic Research Agent) — approved by `@pierpaolo28` with Katherine coordinating AG surfacing.
- Default reviewer: `@happyhuman` (first PR review).

**Technically clean but customer value is unclear** — infra / plumbing / safety / developer-tool patterns with no obvious customer-facing surface → **Defer the accept/decline decision to `@kgu-here`.** Draft a reply that says Pier is deferring the decision to her, and ask her to weigh in on whether the use case has an Agent Garden angle. If yes, Pier approves and routes the PR; if the use case doesn't resonate, close as not planned. The recipe does not earn its place in `contrib/` on technical merit alone.
- Use the **DEFER TO @kgu-here** template in `references/reply-templates.md`.
- Precedent: `#2769` (authority-revocation plumbing — technically a clean rescope of #2767, but no obvious customer-facing surface, so deferred for her read).

## 3. Draft the comment

Use the templates in `references/reply-templates.md`. Match the voice: Pier's replies are short, cordial, cite the specific checklist rule or precedent, and never pad with apology. Fill the `<slot>`s with the specific detail from the proposal.

Then present to the user:

```
Issue: #<num> — <title>
Author: @<login>  (first contribution? check with `gh api /users/<login>/contributions-collection`)
Decision: ACCEPT | DECLINE (<gate letter> — <short reason>)
Overlap/gap scan: <what you ran and what you found>

--- draft reply ---
<the drafted comment>
---

Suggested command:
gh issue comment <num> --repo google/adk-recipes --body-file -   # paste draft
# then, if declining:
gh issue close <num> --repo google/adk-recipes --reason "not planned"
```

Do **not** post the comment or close the issue yourself. The user posts.

## 4. Edge-case guidance

- **First-time contributor with a close-but-not-quite proposal.** If the idea has merit and only one soft gate fires (F or G), surface this to the user and suggest an "approved with scope adjustment" reply rather than a flat decline — this is what happened on parts of the `#2619` thread.
- **Vendor-authored proposal with full disclosure.** Thank them for the disclosure explicitly (see `#2705` reply), then apply gates E/D normally. Disclosure does not bypass the gate.
- **Proposal with a PR already open.** Note the PR in the reply, decline (or accept) on the proposal's merits, and separately acknowledge the engineering quality (see `#2664`, `#2658`). Do not conflate "the PR is good" with "the recipe should land".
- **Core/ repo request mislabeled as contrib/.** Redirect, do not decline on scope.
- **Multiple proposals from the same author in the queue.** Triage them independently; prior-week accept does not create a backlog claim.

## 5. Who signs off

- `@pierpaolo28` — primary decision authority on every proposal.
- `@kgu-here` — Agent Garden decision authority. Her role depends on the proposal:
  - **Clear customer-facing proposal:** parallel coordination for AG surfacing — informational, not a PR blocker. Pier accepts on his own.
  - **Technically clean but customer value is unclear (infra / plumbing / safety / developer-tool):** Katherine is the **gate** — Pier defers the accept/decline decision to her. If she doesn't see an AG angle, the recipe does not land in `contrib/` on technical merit alone. See the "Accept vs. defer to AG" section above and the DEFER template in `references/reply-templates.md`.
- `@happyhuman` — not a decider; the first PR reviewer once a proposal is accepted.

The user running this skill is almost certainly Pier. Draft in first person for Pier unless told otherwise.

## References

- `references/reply-templates.md` — exact wording for each outcome, derived from the actual closing comments on `#2705`, `#2683`, `#2651`, `#2685`, `#2755`, `#2752`, `#2664`, `#2658`, `#2717`.
- `references/precedent-index.md` — one-line summary of every closed proposal with the gate it hit, so you can cite precedent by number.
