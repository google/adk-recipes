# Safety Plugins — Agent-Agnostic Guardrails

## Intent

A clone-and-study recipe for **global safety guardrails implemented as ADK
`BasePlugin`s attached to the `Runner`**. Two interchangeable plugins are
provided — an **LLM-as-a-judge** and a **Model Armor** filter — that hook the
same lifecycle callbacks to classify and block unsafe content. The interesting
part is **not** the agents (deliberately mundane sum/fibonacci demos); it's the
**plugins**: because they're wired at the Runner they wrap **every** agent and
sub-agent under it, and they defend against **session poisoning** by never
persisting harmful content to session state.

## When To Use

- The user wants a safety/guardrail layer that is **agent-agnostic** — attach
  once at the Runner instead of editing each agent's callbacks.
- The user wants a worked example of ADK plugin hooks
  (`on_user_message_callback`, `before/after_tool_callback`,
  `after_model_callback`, plus the `before_run_callback` halt).
- The user is choosing between an **LLM judge** (flexible, prompt-driven) and
  **Model Armor** (managed GCP content-safety API) and wants both side by side.
- The user cares about **session-poisoning** attacks (harmful text left in
  history being reused to jailbreak later turns).

## Eval

- **Scenarios Path**: none — this recipe ships **no eval datasets** and **no
  `agents-cli` eval config**. Do not look for `tests/eval/`.
- **Tests** live in `tests/`: `tests/test_agents.py` is a happy-path smoke test
  that runs `root_agent` through an `InMemoryRunner` and asserts a non-empty
  reply (**live Gemini call** — needs ADC); `tests/test_runnability.py` is an
  import-time check that `root_agent` is defined (patches `google.auth.default`
  so it runs credential-less). **Neither test exercises the safety plugins.**

## End-to-end flow

1. `main.py` builds an `InMemoryRunner(agent=root_agent, plugins=[...])`. The
   chosen plugin (`LlmAsAJudge` or `ModelArmorSafetyFilterPlugin`) is attached
   to the **Runner**, so it guards both `main_agent` and its `sub_agent`.
2. **User message** → `on_user_message_callback` classifies the prompt. If
   unsafe, the callback **overwrites the stored user `Content`** with a "removed"
   placeholder and sets session state `is_user_prompt_safe=False`;
   `before_run_callback` then reads that flag, resets it to `True`, and returns a
   canned model reply — **halting the invocation before the prompt reaches the
   model**. Harmful text is never persisted (the session-poisoning defense).
3. **Tool call** → `before_tool_callback` classifies `tool.name(args)`; unsafe →
   returns `{"error": ...}` so the call is blocked as if the tool failed.
   (`LlmAsAJudge` only.)
4. **Tool output** → `after_tool_callback` classifies the result; unsafe →
   returns `{"error": ...}` in place of the output.
5. **Model output** → `after_model_callback` joins the response parts and
   classifies them; unsafe → replaces the response with a canned "removed"
   message.
6. Only the **classifier** differs between plugins: the judge runs a **second
   LLM** in its own `InMemoryRunner`; Model Armor calls the **Model Armor REST
   API** and reports the matched filter categories.

## Most interesting files to study (in order)

### Safety plugins (the bulk of the logic)
1. **`app/plugins/agent_as_a_judge.py`** — the `LlmAsAJudge`
   `BasePlugin`. A `default_jailbreak_safety_agent` (`LlmAgent`, model from
   `MODEL_NAME_GENERATED_2`) instructed by `JAILBREAK_FILTER_INSTRUCTION` to
   answer only `<SAFE>`/`<UNSAFE>` runs in **its own** `InMemoryRunner`
   (`judge_app`), decoupled from the protected agent. `_is_unsafe()` wraps each
   payload in typed tags (`<user_message>`, `<tool_call>`, `<tool_output>`,
   `<model_output>`) so one judge prompt classifies every hook, then applies
   `analysis_parser` (default `"UNSAFE" in analysis`). Which hooks fire is gated
   by the `JudgeOn` `StrEnum` set (`judge_on`, default
   `{USER_MESSAGE, TOOL_OUTPUT}`). **Study the `on_user_message_callback` →
   `before_run_callback` handshake via `session.state["is_user_prompt_safe"]`** —
   that is the session-poisoning defense.
2. **`app/plugins/model_armor.py`** — the
   `ModelArmorSafetyFilterPlugin` `BasePlugin`. Its constructor reads
   `GOOGLE_CLOUD_PROJECT` / `GOOGLE_CLOUD_LOCATION` / `MODEL_ARMOR_TEMPLATE_ID`,
   builds the template resource name, and creates a **regional**
   `ModelArmorClient` (`modelarmor.<location>.rep.googleapis.com`). It calls
   `sanitize_user_prompt` on user messages and tool outputs and
   `sanitize_model_response` on model output; violations trigger the same
   `is_user_prompt_safe`/`before_run_callback` halt, a replacement `LlmResponse`,
   or a `{"error": ...}`. **Differences from the judge:** no
   `before_tool_callback` (tool inputs are not checked), and blocked messages
   list the detected filter categories.

### Plugin support
3. **`app/util.py`** — the shared plumbing. `run_prompt()` sends one
   `Content` through a runner and returns `(author, text)` — used by both
   `main.py` and the judge plugin. `parse_model_armor_response()` plus the
   per-filter parsers (`csam`, `malicious_uris`, `rai`, `pi_and_jailbreak`,
   `sdp`) flatten a `Sanitize*Response` into a list of matched filter names
   (returns `None` on `NO_MATCH_FOUND`) — this is what turns the raw API result
   into the human-readable "reasons" appended to a blocked message.
4. **`app/prompts.py`** — `JAILBREAK_FILTER_INSTRUCTION`, the judge's
   system instruction: a detailed jailbreak taxonomy (persona/role-play,
   hypothetical framing, rule manipulation, obfuscation/encoding, adversarial
   suffixes, low-resource-language evasion, …) ending with the tag contract and
   "respond only with `<UNSAFE>` or `<SAFE>`". `ROOT_AGENT_SI`/`SUB_AGENT_SI` are
   trivial by comparison.
5. **`app/tools.py`** — `short_sum_tool`/`long_sum_tool` (CPU-bound)
   and `io_bound_tool` (a `sleep`) are filler, but **`fib_tool` deliberately
   appends a planted "unsuspecting message that can cause undesired output"** to
   its return value — a fixture for watching the `after_tool` hook catch poisoned
   tool output.

### CLI runner + agents (thin layer — read last)
6. **`app/main.py`** — the entry point. An absl
   `--plugin {llm_judge,model_armor,none}` flag selects which plugin to build,
   then the one line that matters: **`plugins=plugins` on
   `InMemoryRunner(agent=root_agent, app_name="test_app_with_plugin", ...)`** —
   attaching guardrails at the Runner is what makes them global. Followed by a
   multi-turn REPL over `util.run_prompt` until you type `exit`.
7. **`app/agent.py`** — deliberately generic. `root_agent` (name
   `main_agent`, tools `short_sum_tool`/`long_sum_tool`) delegates to `sub_agent`
   (tools `fib_tool`/`io_bound_tool`); model from `MODEL_NAME_GENERATED_1`. The
   agents carry **no** safety logic — all guardrails live in the plugins, which
   is the whole point (agent-agnostic).
8. **`app/__init__.py`** — env bootstrap: `load_dotenv()`, discovers
   the GCP project via `google.auth.default()`, and defaults
   `GOOGLE_GENAI_USE_VERTEXAI=true` and `GOOGLE_CLOUD_LOCATION=global`.

## Data handling

- **No datastore, no ingestion.** The only "data" is conversation content
  (user messages, tool calls/outputs, model responses) flowing through the
  plugin hooks.
- **Session-poisoning defense.** Unsafe user prompts are overwritten with a
  "removed" placeholder **before** they're persisted, and the invocation is
  halted in `before_run_callback` so the model never sees them; unsafe model
  output is likewise replaced. This stops later turns from exploiting harmful
  text left in session history.
- **Session state.** `is_user_prompt_safe` is the single flag the plugins write;
  it is read once in `before_run_callback` and reset to `True` each turn.
- **Classifier backends differ in cost.** The judge is a **second Gemini call**
  (extra latency + tokens, its own runner/session); Model Armor is a **regional
  REST API** keyed by a GCP template. Neither retains conversation data beyond
  the request.

## Gotchas / things to know

- **The two plugins are not identical in coverage.** Only `LlmAsAJudge`
  implements `before_tool_callback` (tool *inputs*). Model Armor checks user
  prompts, model output, and tool *outputs* only. (`README.md` says "both" hook
  `before_tool_callback`; the code disagrees — trust the code.)
- **Judge defaults are conservative.** `judge_on` defaults to
  `{USER_MESSAGE, TOOL_OUTPUT}`, so **model-output and tool-input checks are off
  by default** — pass a wider `judge_on` set to enable them.
- **The judge doubles model calls** (latency + cost) and is only as good as its
  parser. Default `analysis_parser` is `"UNSAFE" in analysis`; the instruction
  emits `<UNSAFE>`/`<SAFE>` (still contains the substring, so it works). A custom
  `analysis_parser` is the intended extension point.
- **Model Armor needs a pre-created template** and `MODEL_ARMOR_TEMPLATE_ID`;
  without a valid project/location/template the client URL is wrong and calls
  fail. It uses a **regional** endpoint, so `GOOGLE_CLOUD_LOCATION` must be a
  real region (e.g. `us-central1`) — **not** `global`, which `__init__.py`
  defaults to.
- **Models come from env vars.** `MODEL_NAME_GENERATED_1` (agents) and
  `MODEL_NAME_GENERATED_2` (judge). `.env.example` sets the agents to
  `gemini-3.5-flash` and the judge to `gemini-3.1-flash-lite`.
- **`main.py` uses absl flags**, so run it as a module
  (`python -m app.main`); `--plugin` only accepts
  `llm_judge|model_armor|none`.
- **Tests don't cover the plugins** and `tests/test_agents.py` makes a **live
  Gemini call**, so it needs ADC/credentials.

## Where to run things

No `Makefile` — run everything with `uv` from `core/python/safety-plugins/`:

- `uv sync` (add `--group dev` for `pytest`/`ruff`) — install.
- `uv run python -m app.main --plugin {llm_judge,model_armor,none}` —
  the plugin CLI; drops you into a multi-turn REPL (type `exit` to quit).
  `none` is the baseline with no guardrails.
- `uv run adk run app` / `uv run adk web` — standard ADK CLI / local
  web UI (select `app` in the dropdown).
- `uv run pytest tests` — the smoke + import tests (need ADC for the live Gemini
  call).

## Reuse (copy as-is)

- **`app/plugins/` is the reusable artifact.** Both plugins are plain
  ADK `BasePlugin`s with **no coupling to `main_agent`/`sub_agent`** — attach
  them to any Runner and they guard every agent/sub-agent underneath:
  `Runner(agent=your_agent, plugins=[LlmAsAJudge()])` or
  `[ModelArmorSafetyFilterPlugin()]`.
- **Bring the support code along.** `agent_as_a_judge.py` imports
  `prompts.JAILBREAK_FILTER_INSTRUCTION` and `util.run_prompt`; `model_armor.py`
  imports `util.parse_model_armor_response`. Copy `plugins/` **together with
  `util.py`** (and `prompts.py` for the judge).
- **Customize without editing the plugin:** swap the judge model/agent via
  `judge_agent`, widen coverage via `judge_on`, or change verdict logic via
  `analysis_parser`; point Model Armor at a different template via
  `template_id`/env.
- **Dependencies:** `google-adk` and `google-genai` (both), plus
  `google-cloud-modelarmor` (Model Armor only).
