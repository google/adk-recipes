# Safety Plugins — Agent-Agnostic Guardrails

## Intent and scope

This recipe demonstrates global ADK `BasePlugin` guardrails attached to a
Runner. The demo agents perform sums and Fibonacci calculations; safety logic
belongs in the plugins. Changes stay inside this recipe.

Two independent implementations are available:

- `LlmAsAJudge`: a separate Gemini agent classifies selected lifecycle events.
- `ModelArmorSafetyFilterPlugin`: a regional Google Model Armor client screens
  user text, model text and JSON tool results.

## Model Armor package

The reusable artifact is `safety_plugins/plugins/model_armor/`:

| Module | Responsibility |
| --- | --- |
| `constants.py` | Stages, actions, RPC names and replacement messages |
| `client.py` | Lazy async Google client, regional RPCs, deadlines and cleanup |
| `response.py` | Protobuf validation, matched filters and SDP replacement text |
| `policy.py` | Pure allow/redact/block decisions |
| `telemetry.py` | Metadata construction without screened content |
| `logs.py` | Standard logging, with no application-specific logging dependency |
| `plugin.py` | ADK callbacks and external-payload adaptation |
| `__init__.py` | Public imports; no singleton or client initialization |

Read the package in that order. Public imports remain
`from safety_plugins.plugins.model_armor import ModelArmorSafetyFilterPlugin`.
Copy the complete package for reuse; it needs no demo agent, prompts or util
module. The separate LLM judge still uses `prompts.py` and `util.py`.

## Screening flow

1. `main.py --plugin model_armor` attaches the plugin to the Runner. It wraps
   the root agent, sub-agent, model responses and tool results.
2. `on_user_message_callback` joins all text parts and calls
   `sanitizeUserPrompt`. Replacements mutate the message before ADK persists it.
3. An unsafe prompt sets `temp:model_armor_user_prompt_unsafe`.
   `before_run_callback` consumes that marker and stops the invocation with a
   canned reply. `before_model_callback` provides a fallback halt for runtimes
   that do not use the Runner's early exit. The marker must not block a later
   legitimate turn.
4. `after_model_callback` screens text using `sanitizeModelResponse`.
5. `after_tool_callback` serializes the result as JSON and uses
   `sanitizeUserPrompt` so tool content also receives injection screening.
6. `screen_external_payload` is an explicit helper for application callbacks:
   invoke it before writing fetched data to state. It returns a safe dict or
   `None`; it is not automatically attached to arbitrary state writes.

Every stage uses the same policy. A successful scan with no match is allowed.
SDP alone with a successful textual de-identification is replaced. SDP without
usable replacement text, any other match, and combinations with non-SDP filters
are blocked. API failures, timeouts, partial invocations, unspecified verdicts
and contradictory aggregate/filter verdicts fail closed.

Project SDP replacements only onto plain public unsigned text. One text part
can be replaced while preserving adjacent non-text parts; multiple parts may
collapse only in a text-only user message. Redactions of thought/signed text,
multiple model text parts, or interleaved user content block before telemetry
is recorded. Never publish thoughts or discard tool calls during redaction.

Tool replacements that remain JSON objects preserve their structure. Other
replacement text is wrapped under `redacted_output`; the original is never
returned as a fallback. External callback replacements must remain JSON dicts.

## Configuration and lifecycle

- `GOOGLE_CLOUD_PROJECT` and `MODEL_ARMOR_TEMPLATE_ID` select the template.
- `GOOGLE_CLOUD_MODEL_ARMOR_PROJECT` optionally selects a separate project.
- `GOOGLE_CLOUD_MODEL_ARMOR_LOCATION` selects the regional endpoint, falling
  back to `GOOGLE_CLOUD_LOCATION`. `global` is rejected for Model Armor.
- `MODEL_ARMOR_TIMEOUT_S` defaults to 5 seconds and must be finite and positive.
  The RPC has this deadline and no automatic retries; an application guard
  cancels calls that exceed the deadline by one second.
- Resolve environment values at construction, not in default arguments.
- Google client initialization is lazy. Close the Runner with `async with` so
  the plugin closes the transport. An unused client must not initialize just
  to close.
- An explicit `ModelArmorClient` can be injected for testing or to pass Google
  credentials. Keep credentials out of logs and files.

The Model Armor template needs advanced SDP with both inspection and
replacement templates to return `deidentifyResult.data.text`. Inspect-only
matches block. Region and organization location policies must allow the chosen
location. The Model Armor service agent needs access to its SDP templates.

## Data and coverage

No datastore or ingestion is included. Logs contain stage, action, matched
filters, invocation status, error class and SDP metadata; never prompts,
responses, full state, transformed text or exception messages.

Coverage is textual. Tool arguments, images/audio, existing session history
and arbitrary state writes are outside this recipe's automatic checks. Use
buffered model responses; safe streaming of partial responses is not promised.
Do not claim protection for those paths from the current tests.

Only the LLM judge implements `before_tool_callback`. Its default checks are
user messages and tool outputs; model output and tool-input checks require a
wider `judge_on` set. It uses a separate Gemini Runner and its own session.

## Running and verification

Run from `core/python/safety-plugins/`:

- `uv sync --group dev`
- `uv run python -m safety_plugins.main --plugin model_armor`
- `uv run python -m safety_plugins.main --plugin llm_judge`
- `uv run python -m safety_plugins.main --plugin none`
- `uv run --frozen pytest tests`
- `uv run --frozen ruff check safety_plugins tests scripts`

`adk run safety_plugins` and `adk web` load the demo `root_agent` without either
plugin; use `main.py` for guardrails. Models come from
`MODEL_NAME_GENERATED_1` and `MODEL_NAME_GENERATED_2`; `.env.example` uses
`gemini-3.5-flash` and `gemini-3.1-flash-lite`.

Unit tests disable dotenv loading, mock Gemini and the Google RPC boundary,
and use synthetic protobuf responses. They require no credentials. Runner
integration tests verify the actual model input, saved history, blocked turns,
recovery, tool replacement and transport cleanup. There are no eval datasets
or `agents-cli` eval configuration.

`scripts/live_model_armor.py` is an opt-in check against an existing Google
Cloud template with email de-identification and injection detection. It uses
real Model Armor RPCs with synthetic content and a deterministic local model;
Gemini is not called. It uses the explicitly requested gcloud account without
changing active configuration or ADC, and exits nonzero on a failed check.
Its verdict compares actual model input, final output and stored session data
against `--email-replacement`. Logs are diagnostic only. The report includes
the probe's fixed synthetic content and transformed values, never credentials.
Distinguish these live RPC checks from unit tests and from live Gemini behavior.
