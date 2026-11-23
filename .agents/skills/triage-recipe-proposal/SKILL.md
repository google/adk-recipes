---
name: triage-recipe-proposal
description: Triage a  [RECIPE PROPOSAL]  issue in google/adk-recipes by running the historical accept/decline rubric and drafting the maintainer reply. Invoke when the user shares a proposal issue number or URL, or asks to "triage the open recipe proposals".
---


# Triage a recipe proposal (google/adk-recipes)


You are helping Pier ( @pierpaolo28 ) decide **accept** or **decline** on a  contrib/  recipe proposal, and drafting the comment to post. Every decision in this skill is anchored to how prior proposals were actually resolved.


The repo's own automated triage **skips** any issue whose title contains  [RECIPE PROPOSAL]  (see  .github/workflows/ai-issue-automated-triage.yml ), so these are reviewed entirely by hand. That is what this skill accelerates.


---


## 0. Inputs


The user will give you one of:


a GitHub issue number (#2766, 2766)
a full URL (`https://github.com/google/adk-recipes/issues/2766`)
the phrase "the open proposals" / "the triage queue" — then list all open ones first


If multiple proposals are in scope, triage them one by one and produce one draft reply per issue.


## 1. Fetch


Always start from the live issue, not memory:
bash
gh issue view <num> --repo google/adk-recipes \
  --json number,title,author,createdAt,body,labels,comments,state


If the user asked for "the queue", first:
bash
gh issue list --repo google/adk-recipes --label contribute --state open \
  --limit 50 --json number,title,author,createdAt


Also, before deciding on scope-overlap, scan what already exists so you don't re-derive from stale memory:
bash
# Existing recipes
gh api repos/google/adk-recipes/contents/contrib/python --jq '.[].name'
gh api repos/google/adk-recipes/contents/core/python   --jq '.[].name'
gh api repos/google/adk-recipes/contents/plugins       --jq '.[].name'


# Pending proposals and open PRs that could overlap
gh issue list --repo google/adk-recipes --label contribute --state open \
  --limit 50 --json number,title,author
gh pr list --repo google/adk-recipes --state open \
  --limit 50 --json number,title,author


## 2. Decision rubric — hard gates first, in order


Walk the gates in order. The **first** gate that triggers determines the outcome. Do not accumulate reasons past the first failing hard gate (or at most two tightly coupled reasons, e.g. Gate C + D or Gate D + E).


### Gate A — Template completeness
**Reject-as-not-actionable** if the body is empty, is only the unmodified template boilerplate, or leaves the six template questions unanswered. The template asks: (1) intent, (2) language, (3) deployable Y/N, (4) GCP services / third-party APIs, (5) owner, (6) extras.


Precedent: #2685, #2684, #2611.
Use the **not-actionable** reply template below.
Close with --reason "not planned".


### Gate B — Wrong root (core/ or top-level  plugins/  scope creep)
If the proposal is for core/ rather than contrib/, redirect — core/ follows a separate internal process and the template's scope notice explicitly says "contrib/ recipes only".
Top-level plugins/<vertical> directories are also curated separately by the core team rather than opened ad-hoc via contrib/ recipe PRs (see #2823, #2800).


### Gate C — Deployability (and standalone library vs. agent recipe)
**Decline** if:
The proposal says explicitly that the initial scope will not include a Dockerfile, or that it will not be deployable. The Recipe Checklist §1 requires every contrib/ recipe to have a root Dockerfile and deployable: true in manifest.yaml.
Precedent: #2683 (Model Armor SDP), #2651 (oncall-task-scoped-authority first attempt).
If the proposer says "I will add the Dockerfile after approval" — that is **fine**, not a decline (see #2767, #2769).
2. The proposal is actually an **installable Python library / SDK dependency** rather than an end-to-end deployable agent recipe (e.g., answering Section 3 with "It will be a Python package that can be added as a dependency to an ADK project").
Precedent: #2870 (adk-ledger metering library — declined on Gate C + D).
**Tone rule:** When declining a standalone library submission, do **not** append an open-ended invitation to submit a brand-new recipe (e.g. do not say "If you'd like to propose a self-contained, deployable GCP agent recipe..., happy to review"). State the reasons and close cleanly as not planned.


### Gate D — Scope fit (GCP enterprise agent)
**Decline** if the recipe's primary subject sits outside Google Cloud enterprise agent patterns, or lists **no required GCP services** (or mentions GCP only as an optional adaptation). The repo is centered on BigQuery / Vertex AI / Cloud Run / Memory Bank / Agent Engine / ADK-native patterns, plus the retail vertical in  plugins/ . Non-GCP infra (crypto rails, L2 blockchains, games, non-GCP cloud as the backbone) does not fit even when the ADK integration is clean.


Precedent: #2705 (crypto-payroll on Base L2), #2796 (PulseBet signed price records), #2799 (Plazmi game MCP), #2808 (SanctionsKit MCP with no GCP service), #2857 (Weilliptic SDK with GCP only "where applicable"), #2870 (adk-ledger with "None" for required GCP services).


### Gate E — Vendor or author-owned package dependency as primary pattern
**Decline** if the recipe's core teaching value depends on a single external SaaS, gateway, SDK, or author-maintained PyPI/GitHub package owned by the proposer, such that the recipe is effectively a tutorial for that vendor's product or creates a single point of maintenance outside the repo and the ADK release process.


Precedent:
External vendor SDK / SaaS / gateway: #2857 (Weilliptic-sdk), #2808 (SanctionsKit), #2796 (pulse-verity), #2752 (session-dollar-budget), #2705 (Spraay).
Author-maintained external Python library as backbone: #2800 (statmike/adk-smart-router — declined even though surrounding BigQuery integration was first-party), #2870 (delta-fde/ravidalal-adk-ledger).


### Gate F — Scope overlap (including composite / hybrid proposals)
**Decline** if an existing recipe or an already-accepted/pending proposal in the queue covers the same pattern. Overlap means "we don't want two parallel recipes in that space", not "nobody has ever touched BigQuery before".


**Direct overlap → Decline:**
#2664 declined because #2658 already covered agent → BigQuery via hosted MCP.
#2823 (FinOps Zero-Trust Governor) declined because its mid-task OAuth revocation guardrail directly overlapped #2769 (already in the triage queue with its original author) and its FinOps billing surface overlapped #2658.
**Partial overlap inside a hybrid proposal → Collaborate or re-scope before AG review (#2858 pattern):**
If a proposal combines a genuinely new vertical/model (e.g. TimesFM + BigQuery forecasting in #2858) with a secondary component that overlaps an already in-flight recipe (e.g. AlphaEvolve in #2810 / PR #2814), **do not** immediately approve the hybrid proposal.
Instead: (1) suggest collaborating on the in-flight recipe (#2810 / PR #2814) if that overlapping piece is their main interest, or (2) ask the author to **update the proposal** to focus strictly on the non-overlapping core (TimesFM + BigQuery forecasting, dropping the simulated/overlapping piece) and note that once updated, we will get @kgu-here's point of view for Agent Garden.


### Gate G — Differentiation / coverage-gap
**Decline** if the pattern is already well-covered by existing GCP documentation, ADK samples, or in-repo recipes, and the proposal does not add a sufficiently differentiated angle.


Precedent: #2755 (VirusTotal MCP — MCP + tool-allowlisting not differentiated enough), #2658 (FinOps on hosted BigQuery MCP — covered by existing GCP docs), #2823 (BigQuery FinOps + governance callbacks).
This is the softest and most subjective gate. If uncertain, surface the decision to the user rather than auto-declining.


### Accept vs. defer to AG


If none of A–G fires, the next question is **customer value**.  contrib/  is a curated gallery, not a dumping ground for well-engineered recipes — every recipe needs a reason for someone to actually want to run it.


**Clear customer value** — vertical / finance / retail / healthcare / forecasting / support customer-facing agent with an obvious end-user scenario → **Pier accepts on his own** (or after quick alignment with  @kgu-here ). Optionally cc  @kgu-here  for parallel Agent Garden surfacing coordination (informational, not a PR blocker), or ask  @kgu-here  for her read once any scope overlap is trimmed (as in  #2858 ).
Precedent: #2717 (Economic Research Agent), #2835 (Personal Health Agent), #2843 (Idea Generation / Co-Scientist), #2845 (auto-hill-climb), #2810–#2812 (AlphaEvolve, CodeMender, WeatherNext).
Default reviewer: @happyhuman or @tommywagz (first PR review).


**Technically clean but customer value is unclear** — infra / plumbing / safety / developer-tool patterns with no obvious customer-facing surface → **Defer the accept/decline decision to  @kgu-here ** (unless it already fails Gate F/G overlap like  #2823 , in which case decline directly). Draft a reply that says Pier is deferring the decision to her, and ask her to weigh in on whether the use case has an Agent Garden angle. If yes, Pier approves and routes the PR; if the use case doesn't resonate, close as not planned. The recipe does not earn its place in  contrib/  on technical merit alone.
Use the **DEFER TO @kgu-here** template in references/reply-templates.md.
Precedent: #2769 (authority-revocation plumbing — technically a clean rescope of #2767, but no obvious customer-facing surface, so deferred for her read).


## 3. Draft the comment


Use the templates in  references/reply-templates.md . Match the voice: Pier's replies are short, cordial, cite the specific checklist rule or precedent, and never pad with apology or unsolicited invitations to submit unrelated recipes. Fill the `<slot>`s with the specific detail from the proposal.


Then present to the user:
Issue: #<num> — <title>
Author: @<login>  (first contribution? check prior issues/PRs in google/adk-recipes)
Decision: ACCEPT | DECLINE (<gate letter> — <short reason>) | DEFER / RE-SCOPE
Overlap/gap scan: <what you ran and what you found>


--- draft reply ---
<the drafted comment>
---


Suggested command:
gh issue comment <num> --repo google/adk-recipes --body-file -   # paste draft
# then, if declining:
gh issue close <num> --repo google/adk-recipes --reason "not planned"
# AND if a related PR is open for this proposal, ALWAYS close the PR as well:
gh pr close <pr-num> --repo google/adk-recipes --comment "Closing alongside #<num>."


Do **not** post the comment or close the issue on your first turn — present the triage cards to the user first, and only execute  gh issue comment  /  gh issue close  once the user confirms or adjusts the responses.


## 4. Edge-case guidance


**Hybrid proposal where one component overlaps an in-flight recipe (#2858).** Point the author to the in-flight issue/PR (e.g. #2810 / PR #2814) to collaborate if they care primarily about that component; otherwise ask them to update the proposal to focus solely on the non-overlapping subject (e.g. TimesFM) so @kgu-here can weigh in on Agent Garden fit. Leave the issue open while awaiting the update.
**Multiple proposals from the same author (#2820 vs. #2843).** We avoid running two parallel recipes from the same author at once. When one of an author's proposals is selected to move forward (e.g. #2843 Idea Generation by @rhvaz17), close the secondary proposal (#2820 Investor Relations) with a brief cordial note that we are focusing on moving ahead with their other approved proposal instead.
**Standalone library / SDK pitched as a recipe (#2870, #2800).** Decline under Gate C (Recipe Checklist §1 requires a deployable agent recipe with a root Dockerfile and deployable: true in manifest.yaml, not an installable Python library) and Gate D/E (external package / no required GCP services). Do **not** include an open-ended "if you'd like to propose a deployable GCP recipe instead..." footer.
**Vendor-authored proposal with full disclosure (#2705, #2796, #2808).** Thank them for the disclosure explicitly, then apply gates D/E normally. Disclosure does not bypass the gate.
**Proposal with a PR already open (#2823/PR #2824, #2664/PR #2665, #2800/PR #2819).** Always check for an open PR by the same author or referencing the issue (gh pr list --repo google/adk-recipes --author <login> --state open). Note the PR in the reply, decline (or accept) on the proposal's scope merits, and briefly acknowledge the engineering work before pivoting to the gate reason. Do not conflate "the PR passes local validators" with "the recipe should land". **When closing a declined issue (--reason "not planned"), ALWAYS close the related open PR at the same time** (gh pr close <pr-num> --repo google/adk-recipes --comment "Closing alongside #<num>.").
**Core/ repo request mislabeled as contrib/.** Redirect, do not decline on scope.


## 5. Who signs off


@pierpaolo28 — primary decision authority on every proposal.
@kgu-here — Agent Garden decision authority. Her role depends on the proposal:
**Clear customer-facing proposal:** parallel coordination for AG surfacing — informational, not a PR blocker (or consulted after a hybrid proposal is narrowed to its core vertical, as in #2858).
**Technically clean but customer value is unclear (infra / plumbing / safety / developer-tool):** Katherine is the **gate** — Pier defers the accept/decline decision to her. If she doesn't see an AG angle, the recipe does not land in contrib/ on technical merit alone.
@happyhuman / @tommywagz — not proposal deciders; the first PR reviewers once a proposal is accepted.


The user running this skill is almost certainly Pier. Draft in first person for Pier unless told otherwise.


## References


references/reply-templates.md — exact wording for each outcome, derived from the actual comments on #2870, #2858, #2857, #2823, #2820, #2808, #2800, #2796, #2769, #2755, #2752, #2717, #2705, #2685, #2683, #2664, #2658.
references/precedent-index.md — one-line summary of every resolved and open proposal with the gate it hit, so you can cite precedent by number.