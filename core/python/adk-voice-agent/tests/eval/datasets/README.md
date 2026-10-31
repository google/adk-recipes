# Evaluation datasets

Run with `make eval` (System 1, the voice) and `make eval-sandbox` (System 2, the computer), or
through the wrapper they call, `scripts/run_eval.py`:

```bash
# System 1 (ADK Voice Agent + System 1 -> System 2 handoff): system1.json, the default
uv run python scripts/run_eval.py --suite system1

# A few cases
uv run python scripts/run_eval.py --suite system1 --cases gated_send,publish_consent

# Standalone System 2 (coding harnesses on the user's computer) suite
uv run python scripts/run_eval.py --suite system2
```

Every case embeds its own `rubrics`, `reference`, and deterministic `expectations`
(including the `history` it starts from), so nothing is looked up elsewhere.

| Dataset | What it covers |
|---|---|
| `system1.json` | The System 1 set ([Evals](../../../docs/evals.md)): 29 spoken, underspecified requests from the Alex persona, graded on routing (a quick `look` versus a thread) and the `project` model, act-first thread management (the approach line, findings, the computer's questions, steering, stopping), the gates (sends, check-ins, plan-first on request, publishing), continuity and memory (a dropped call, recall days later, "like last time", building on a thread, remember and forget, a changed preference), safety (an email with instructions aimed at the assistant), session-start brevity, project threads (follow-ups about an open project or thread page via `OPEN ON SCREEN` that need no restating, an unrelated request from inside a thread, `navigate`), and how it sounds (`delivery` checks plus the `voice_delivery` judge) |
| `system2.json` | The System 2 set: 13 fixed dispatches run by real Claude Code and Antigravity on docker computers seeded from the Alex persona ([`../fixtures/alex/`](../fixtures/alex/): home snapshot, thread journal, fake app upstreams), graded by [`../eval_config_system2.yaml`](../eval_config_system2.yaml) and [`../system2_agent/graders.py`](../system2_agent/graders.py) on outcome, filing, continuity, safety, latency, and cost. Run with `make eval-sandbox` |

## How a voice case runs

Each case runs in its own server, as the only user it has seen: `scripts/run_eval.py` seeds
the case's `history` fixtures ([`../voice/history.yaml`](../voice/history.yaml); `alex` when
a case names none), starts the server, generates the case's trace, and grades every case
at once. A dropped call or a running thread in one case can never reach another.

| History | What it adds |
|---|---|
| `alex` | Alex's profile (partner Sam, kids Maya and Leo, no red-eyes), GitHub not connected, Monday's call that settled the Tahoe cabin (found only by `recall`), last week's grocery list, a hello note, yesterday's look into the failing payments tests, and a date-night search that just finished with five options |
| `alex_dropped_call` | A call about Leo's birthday party that dropped three minutes ago, so it carries over |
| `alex_away` | Back after a few hours: a dentist question and a retry plan wait on Alex, and a packing list finished while nobody was listening |
| `alex_portland` | The Portland trip as a project: a plan from two days ago with day three and the hotel still open, and nothing waiting on Alex |

The cases follow running threads from the scripted computer in
[`../voice/scenarios.yaml`](../voice/scenarios.yaml): threads act at once, say their
approach first and report findings as they go (`finding` steps), ask when it matters
(`question`), pause a Gmail send at the gateway (`ask`), and check in on a long run (the eval
server's 9-second run budget). Spoken results reach a live text-mode session only while a
turn is open, so cases that grade an answer give the user a short follow-up turn ("Okay.",
"So, am I?") while the computer works.

Every deterministic check in a case has a known-bad trace that fails it
([`tests/unit/test_voice_focus_checks.py`](../../unit/test_voice_focus_checks.py)).
