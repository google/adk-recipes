# Staged UI widgets

A shopping agent that answers with real interface — product cards, a comparison
table, a delivery timeline, a spend chart — rendered as [A2UI](https://a2ui.org)
next to its reply.

The interesting part is not that it renders widgets. ADK will let a tool call
`render_ui_widget` the moment it has something to show, and for a single widget
that is the right amount of machinery. This recipe is about the turn where that
stops being enough: a shopper who says "bring those back up" three turns later,
a refresh that turns out to change nothing, two tool calls that both want the
same widget id. Inline rendering has no answer to the first, no way to take back
the second, and silently ships a duplicate for the third — none of them raising
an exception. The fix is the same each time — separate *deciding what to show*
from *sending it*.

Worth being precise about what is *not* broken, since it is the obvious guess:
two tools rendering two different widgets inline both arrive. Only the order
follows the model's tool calls.

So no tool here renders. Every tool writes a payload into session state and
returns a short summary; one `after_agent_callback` at the end of the turn
converts what was staged and emits it. Across `app/`, `render_ui_widget` is
called in exactly one place — [app/staging/lifecycle.py](app/staging/lifecycle.py).
The tools package only ever names it in prose, and `test_no_tool_renders` in
[tests/unit/test_tools.py](tests/unit/test_tools.py) matches the call form
across `app/`, so a second render call — in a tool or anywhere else — fails the
suite.

## What that split buys

**A widget lifecycle independent of tool calls.** This is the one with no
inline equivalent at all. Because the payload outlives the turn that built it:

- `show_again` re-emits a carousel with no recomputation — it flips four flags
  and the flush does the rest. Without it, "show me those shoes again" is
  answered by re-running the search, which returns *different* shoes once the
  shopper has edited their profile.
- `update_shopper_preference` re-ranks and re-stages the cards on screen in a
  turn where the model never calls the ranking tool, using the query stored
  alongside the payload. The ranking does re-run — that tool calls it — but the
  shopper never asked for it and never repeats their query.
- `compare_picks([])` compares what the shopper is looking at. They said
  "which of those is better" and named nothing; the ids are in the register.
- A re-rank that comes out byte-identical to what is already on screen is
  *suppressed* rather than resent, so the reply cannot announce an update the
  shopper is unable to see. In the turn that *built* the carousel there is
  nothing on screen yet, so it ships — suppressing there would not spare a
  resend, it would delete the only send.

**Deterministic emission order.** Inline rendering emits in whatever order the
model happened to call the tools. The flush walks a declared tuple
([app/staging/spec.py](app/staging/spec.py)), so a turn that produces both a
carousel and a chart always emits them in the same sequence. Verified live: a
model that called the spend tool first still got `['ui-picks', 'ui-spend']`.

**Duplicate-id safety across tool calls.** `render_ui_widget` rejects a
duplicate widget id, but each function call gets its own `ToolContext`
(`flows/llm_flows/functions.py:1228`), so the check only ever sees one call's
widgets. Verified live: two tools rendering the same id ship it twice whether
the model calls them sequentially or in parallel — and in the parallel case
`merge_parallel_function_response_events` concatenates the two lists into one
without re-checking ids. Flushing once, from one context, is where that check
actually bites.

**An answer to "why is my widget missing".** The flush returns one outcome per
declared widget, emitted or not, with the gate that stopped it: `not staged
this turn`, `already emitted`, `suppressed for this turn`, `register empty`,
`converter produced no components`, `host rejected the widget`. A silent skip
is a bad answer to the question this design gets asked most.

## The trap worth reading twice

An `after_agent_callback` that renders widgets and writes nothing produces no
event, and the widgets are discarded with no error anywhere.

`render_ui_widget` appends to the event's *actions*, not to state. ADK creates
an event for a callback only when it returned content **or** state changed
(`agents/base_agent.py:564-582`):

```python
if after_agent_callback_content:
    return Event(..., actions=callback_context._event_actions)
if callback_context.state.has_delta():
    return Event(..., actions=callback_context._event_actions)
return None
```

So a render-only callback returns `None`, no event is created, and the turn
looks exactly like one in which nothing was staged. The emitted flag this
recipe writes per widget is both the dedupe record and the state delta that
forces the event out.
[tests/unit/test_event_delivery.py](tests/unit/test_event_delivery.py) pins it
down with a real `Runner`, because the failure is silent and a refactor could
reintroduce it without breaking anything visible.

## Why the model never writes UI

The model chooses tools and writes prose. It never emits component JSON, for
three reasons that all showed up in testing:

1. **It cannot be validated into correctness.** A2UI is a strict schema —
   parent-before-child ordering, no orphans, a fixed component vocabulary, a
   fixed icon set. Every payload here goes through
   [app/render/converters.py](app/render/converters.py) and is checked against
   the published schema in the test suite. Malformed UI is a code bug, caught
   by `pytest`, not a bad generation caught by a user.
2. **The chart and the sentence cannot disagree.** The headline average and the
   plotted series come from one list. A model narrating a chart it did not draw
   is a model with two chances to round differently.
3. **Tokens.** A product tile is an SVG data URI. It belongs in the payload,
   not in a tool result the model has to read.

What the model *does* get is every fact it might need to reason about — ids,
prices, and the reason chips printed on each card. An early version withheld
the chips, on the theory that a thinner result would stop the model reciting
the card. A live run answered "why did you recommend that one?" by inventing a
rationale from a different tool's output instead. Not duplicating the widget is
the *instruction's* job — see the next section; starving the model of facts only
buys a fluent guess.

## What the reply says instead

Staging decides what the shopper sees. Something still has to decide what the
model *says* next to it, and that turns out to be the harder half.

The obvious approach is one rule in the system instruction: "when a widget is on
screen, don't repeat it." It works until the second widget type. A comparison
table wants a verdict; a delivery timeline wants the question answered in words
with the dates left on screen; a carousel the shopper asked to see again wants
one sentence, because the model already described it two turns ago. One rule
cannot say all of that, and a per-widget snippet is worse: N widgets means N
strings to keep consistent, and when two stage in the same turn the model gets
two sets of instructions and picks one.

So [app/presentation.py](app/presentation.py) puts two levels between the widget
and the prompt. Each widget declares a **role**; live roles collapse by fixed
precedence to exactly one **contract**; each contract owns one instruction.

| Role | Contract | The reply | Widgets |
| ---- | -------- | --------- | ------- |
| `DATA_PRIMARY` | `SYNTHESIS` | Add at most three things the visual cannot say | picks, comparison, spend |
| `SUPPORTING` | `ANSWER` | Answer directly in a sentence or two; the widget holds the detail | order |
| `REPRISE` | `ACKNOWLEDGE` | One sentence confirming it is back | any revived widget |

Three properties come out of that shape:

**Text-primary is the absence of a contract, not a member of the enum.** A turn
with nothing live resolves to `None` and appends nothing, so an ordinary
conversational turn behaves exactly as it would without this layer.

**It scales by role, not by widget.** Adding a fourth data-primary widget adds a
declaration and no prompt text. Thirty widgets would still be three instruction
strings — and the `presentation_role` field is required, with no default, so a
new widget cannot be added without someone deciding how the model should talk
about it.

**The contract cannot promise a widget that state says isn't shipping.** This is
the one worth dwelling on. The resolver and the flush both call
[app/staging/gates.py](app/staging/gates.py) — the same predicate, not two
copies — so a suppressed widget produces no contract. Walkthrough turn 5 is that
case: the re-rank comes out identical, the carousel is held back, and the
contract line prints `none`. With the gates duplicated, drift would not raise
anything; it would ship a reply saying "the cards above are updated" beside
cards that never moved.

Two gates sit outside that guarantee, for two different reasons, and `gates.py`
names both rather than hiding them. `converter produced no components` needs the
converter to have run, and the resolver runs before it. `host rejected the
widget` is the host's verdict on a widget already handed over, which nothing
local can predict. In both cases state says the widget is live, so a payload
that is present but ends up on screen as nothing — a carousel staged with an
empty `items` list — resolves `synthesis`, and the reply gets shaped to point at
a widget the flush then drops. A payload that is *literally* empty is the
tempting example and is **not** this case: `register empty` is decidable from
state, so it blocks the contract at the model call that writes the reply.

Nothing closes those two gaps automatically — including the reply floor below,
which reads the same state-only predicate and will happily supply a companion
sentence for a widget that never arrives. What the design does instead is refuse
to lose the case: the flush names the gate that dropped the widget and
`log_flush` raises it to a warning. That warning is not unique to an overpromise
— a suppressed widget logs one too, which is why walkthrough turn 5 prints
`held back` — so it is the reason string that tells them apart, not the presence
of the line. Neither gate is reachable through the tools this recipe ships:
every path either returns without staging or stages a payload that renders, and
the only rejection ADK itself raises is a duplicate widget id in one event,
which four distinct surface ids cannot produce. A live host can of course refuse
for reasons of its own — which is why that gate exists at all.

The instruction is appended at the *tail* of the system instruction from
`before_model_callback`, which is where a model weights output-shaping
directives most heavily — and per model call, so the first call of a turn (before
any tool has run) gets nothing and the call that writes the visible reply gets
the contract for what the tools actually staged.

Two supporting details:

- **Reviving is made visible.** `stage_widget` and `revive_widget` otherwise
  leave identical state, so a `temp:ui:revived:*` flag marks the difference and
  the resolver turns it into `REPRISE`. Re-staging in the same turn clears it:
  fresh rankings deserve a fresh description. With the flag, the "re-showing
  this, don't describe it again" instruction also comes out of the tool result
  and into the contract, where it carries more weight.
- **A floor under the shaping.** Every contract tells the model to say *less*,
  and a model having an off moment can take that to nothing at all — leaving a
  carousel with no words beside it, which reads as a bug even when every widget
  is correct. `after_model_callback` catches an empty reply next to a widget
  *state says* is live — the same predicate, with the same blind spot — and
  substitutes the widget's `default_companion`. Notably this recipe never
  uses `skip_summarization`: suppressing the reply outright would *guarantee* the
  bare-widget outcome rather than guard against it.

One thing deliberately left out: the role is refined centrally rather than by a
per-spec `resolve_role` callable. With four widgets the refinement is uniform —
revived means reprise, whatever the widget — and a hook every spec would set
identically is ceremony that rots unused.

## See it without credentials

The staging layer, the converters, and the gates need no model and no network:

```bash
uv run python -m app.walkthrough          # one line per turn
uv run python -m app.walkthrough --a2ui   # plus the A2UI messages
```

Eight turns of a shopping conversation with the model's tool choices scripted.
Each turn prints the widgets, the gate that held anything back, and the contract
the reply resolved to. Turn 3 asks for `answer` rather than `synthesis` because a
delivery timeline is a detail panel; turn 5 is the suppressed no-op refresh,
where the contract correctly prints `none`; turn 6 revives the carousel turn 4
staged and drops to `acknowledge`; and turn 8 stages nothing — printing
`state delta: False`, which is the trap above made visible.

## Layout

| Path | What lives there |
| ---- | ---------------- |
| [app/staging/](app/staging/) | `spec` declares the widgets, `state` is the API tools call, `gates` decides what is live, `lifecycle` is the flush, `contract` resolves the turn's reply shape |
| [app/presentation.py](app/presentation.py) | Roles, contracts, and the one instruction each contract owns |
| [app/render/](app/render/) | Payload → A2UI. `components` builds nodes, `converters` maps each semantic type, `registry` dispatches |
| [app/tools/](app/tools/) | The six tools. All stage; none render |
| [app/store.py](app/store.py) | Catalog, orders, spend history, loaded from `app/data/*.json` |
| [app/profile.py](app/profile.py) | The shopper profile: `user:`-scoped, coerced and validated on write |
| [app/ranking.py](app/ranking.py) | Scores products against the profile and produces the reason chips |
| [app/walkthrough.py](app/walkthrough.py) | The credential-free demo above |

Three state scopes carry the whole mechanism, and the difference matters:
`user:shopper_profile` survives across sessions; `ui:register:*` and
`ui:emitted:*` are session-scoped, which is what makes revival possible;
`temp:ui:dirty:*`, `temp:ui:suppress:*`, and `temp:ui:revived:*` are
per-invocation, so a stale flag cannot emit a widget nobody asked for next turn
— or make next turn's reply an acknowledgement of this turn's carousel.

## Requirements

- **uv**: Python package manager — [install](https://docs.astral.sh/uv/getting-started/installation/)
- A Gemini API key, or a Google Cloud project with Vertex AI enabled
- To *see* the widgets rendered, an A2UI-capable host. Everything else —
  including the walkthrough and the full test suite — runs without one.

> **`adk web` is not that host, and it is the first thing worth knowing.**
> `uv run adk web` from this directory starts and serves the agent correctly —
> tools run, replies arrive, the contract is applied. No widget appears. Its
> bundled UI ships a complete A2UI renderer, but feeds it from `<a2ui-json>`
> blocks in model text and from content parts carrying an `a2ui` field; it
> reads `actions.render_ui_widgets` nowhere, for any `provider`. Nothing is
> misconfigured, and nothing will log a warning. To watch the delivery
> mechanism itself, use the walkthrough below — it prints every widget, gate,
> and contract without a model or a host.

## Setup

> All commands run from the recipe root (`contrib/python/staged-ui-widgets/`).

```bash
uv sync
cp .env.example .env    # then set GEMINI_API_KEY, or the Vertex AI variables
```

## Run

```bash
uv run adk run app                                   # interactive CLI
uv run uvicorn app.fast_api_app:app --reload         # local web server
```

Things worth trying, in this order — each one exercises a claim above:

1. `I need new trail shoes` — a carousel.
2. `which of those is the best value?` — a comparison of items the shopper
   never named. Ask *why* it carries the flag; the answer is rating per dollar,
   which is a stated metric, not a judgement.
3. `actually I only buy Fellstone now` — the cards re-rank in a turn where the
   model never calls the ranking tool.
4. `my shoe size hasn't changed` — a refresh that changes nothing, so nothing
   is resent and the reply says so.
5. `bring those shoe cards back up` — revived from the register, not searched
   again.
6. `where's order 9999?` — no widget, and the reply carries the real order ids.

## Running Tests

```bash
uv run pytest              # unit + runnability
uv run pytest tests/unit   # unit only
uv run pytest tests/integration   # needs live credentials
```

The unit suite needs no credentials and no network: it validates real A2UI
against the published schema, exercises every gate, and includes a
`Runner`-level test that a staged widget actually reaches the client.

The rubric evals in [tests/eval/](tests/eval/) score what the unit suite
cannot: whether the model *talks* about a widget correctly. They run through
ADK's eval module, which the base dependency does not install, so they are an
extra rather than a default; and the config has to be named because ADK's
auto-discovery looks for a `test_config.json` beside the evalset rather than
this layout's shared config:

```bash
uv sync --extra eval
uv run adk eval app tests/eval/evalsets/basic.evalset.json \
  --config_file_path tests/eval/eval_config.json
```

Two of the five rubrics — `does_not_recite_the_widget` and
`claims_only_what_shipped` — are the presentation contract's job stated as a
score, which is the only way to check it end to end. Read the per-case output
rather than the exit status: `adk eval` exits `0` even when every case fails.

## Commands

| Command | Description |
| ------- | ----------- |
| `uv run adk run app` | Run the agent in interactive CLI mode |
| `uv run uvicorn app.fast_api_app:app --reload` | Start the local FastAPI development server |
| `uv run python -m app.walkthrough` | Print a scripted conversation's staging outcomes (add `--a2ui` for the messages themselves) |
| `uv run pytest` | Run all test suites |
