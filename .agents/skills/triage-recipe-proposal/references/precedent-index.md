# Precedent index

One-line summary of every recent `contribute`-labelled proposal with the gate it hit. Cite by number in your drafts so Pier can scan the reasoning quickly.

Refresh with:

```bash
gh issue list --repo google/adk-recipes --label contribute --state all \
  --limit 50 --json number,title,state,stateReason,closedAt
```

As of 2026-10-04. Expect decay — re-fetch before relying on any specific precedent in a borderline reply.

## ACCEPTED

| # | Title | Why it landed |
|---|---|---|
| `#2717` | Economic Research Agent (ERA) — regional economic & real estate | GCP-centric, deployable intent, clear owner. Approved by `@kgu-here` + `@pierpaolo28`, routed to `@happyhuman`. |

## DECLINED

| # | Title | Gate | One-line reason |
|---|---|---|---|
| `#2685` | *(empty)* | A | Empty body — closed as not actionable. |
| `#2684` | *(empty)* | A | Empty body — closed as not actionable. |
| `#2611` | `[RECIPE]` *(empty)* | A | Empty template — asked for resubmission. |
| `#2683` | Model Armor SDP de-identification | C | Proposal said no Dockerfile in initial scope → §1 violation. |
| `#2651` | oncall-task-scoped-authority (first attempt) | C | Explicitly non-deployable; later resubmitted as `#2764`. |
| `#2705` | crypto-payroll-agent on Base L2 | D + E | Non-GCP enterprise scope; also author-maintained vendor dependency (Spraay). |
| `#2752` | session-dollar-budget | E | Depends on author's external gateway as the primary pattern. |
| `#2664` | Google Trends → BigQuery SQL agent | F | Overlapped `#2658` on agent-via-BigQuery-MCP. Author had quality PR; declined on scope not execution. |
| `#2658` | gcp-mcp-finops-agent on hosted BigQuery MCP | G | Pattern already surfaced in existing GCP / ADK docs. |
| `#2755` | VirusTotal MCP alert investigator | G | MCP + tool-allowlist pattern not sufficiently differentiated. |
| `#2619` | Multi-agent GraphRAG over Neo4j | F+G edge | Automated triage flagged real gap, but community lane effectively closed. |

## OPEN (as of 2026-10-04)

| # | Title | Author | First-pass read |
|---|---|---|---|
| `#2767` | revoked-mid-task: authority changes while a task is running | `@aeoess` | Deployable path acknowledged ("add Dockerfile after approval" — OK). Depends on Apache-2.0 Agent Passport SDK. Check: Gate G against existing `safety-plugins`. |
| `#2766` | Untrusted content gate for one public HTTPS tool | `@hexablob` | Explicit provider-backed (IsMalicious). Borderline Gate E — judge whether ADK plugin lifecycle lesson is primary vs. tutorial for the vendor's hosted gate. |
| `#2764` | oncall-task-scoped-authority (resubmission) | `@aimable100` | Resubmission of `#2651` with deployability addressed. Check scope overlap against `#2767` (both are authority / plugin-callback patterns from the same week). |

## Patterns across the dataset

- **Hard gates are near-automatic.** Empty body (A), missing Dockerfile (C), non-GCP core (D) are consistently declined without debate.
- **Soft gates get a thank-you first.** When declining on F or G, the standard opener acknowledges engineering quality or thoroughness before pivoting. See `#2664`, `#2658`, `#2705`.
- **Vendor disclosure doesn't bypass Gate E.** `#2705` disclosed Spraay up front and was still declined on the vendor-dependency reason among others.
- **Two reasons max.** When declining on multiple reasons (e.g. `#2705`), the reply enumerates them as a short numbered list and still closes with a single "not planned" close. Never more than two.
- **Resubmissions are read fresh.** `#2764` is `#2651` resubmitted with the deployability concern addressed; triage it against today's gates, not against the prior decision.
