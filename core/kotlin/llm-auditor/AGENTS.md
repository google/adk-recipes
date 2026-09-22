# LLM Auditor — Fact-Checking and Revision Pipeline (Kotlin)

## Intent

Show how to compose a **`SequentialAgent`** in ADK Kotlin: two `LlmAgent`s that
run in order, where the second consumes what the first produced. The domain
task — fact-check an answer, then correct it — is a vehicle for three ADK
concepts that are hard to demonstrate in isolation:

1. Sequential multi-agent composition with a shared model instance.
2. A built-in tool (`GoogleSearchTool`) grounding one sub-agent.
3. An `AfterModelCallback` post-processing a sub-agent's raw output before it
   reaches the user.

This is the Kotlin counterpart of the `llm_auditor` recipe in adk-python.

## When To Use

Study this recipe when you need a **pipeline where one agent's output is
another's input**, or when a sub-agent's raw response needs cleaning before it
is surfaced. It is not the recipe for parallel fan-out, agent-to-agent
delegation via `transfer_to_agent`, or tool authoring — it uses a built-in tool
rather than defining one.

## Eval

There is **no automated test suite**. `gradle test` reports `NO-SOURCE`; CI
therefore verifies that the recipe *compiles* against the pinned adk-kotlin
release, not that it behaves correctly. Verify behaviour by hand:

```bash
export GOOGLE_API_KEY="..."
gradle run
```

Then paste a question-answer pair with a deliberate factual error, as in the
README's example (`Why is the sky blue? / Because the water is blue.`). A
correct run shows the critic listing each claim with a verdict, then the
reviser emitting a corrected answer **with no `---END-OF-EDIT---` marker
visible** — that marker leaking into output is the specific regression the
after-model callback exists to prevent.

## End-to-end flow

```
user: "Double check this: Question: ... Answer: ..."
  │
  ├─ critic_agent   ── GoogleSearchTool ──▶ web
  │    identifies each claim, verifies it, emits verdicts
  │
  └─ reviser_agent
       reads the original answer + the critic's findings,
       minimally edits the text, terminates with ---END-OF-EDIT---
       │
       └─ AfterModelCallback strips the marker and anything after it
```

`SequentialAgent` runs the two in declaration order and passes conversation
state along; the reviser sees the critic's output as prior context.

## Most interesting files to study (in order)

- **`LlmAuditorAgent.kt`** — the whole composition, ~45 lines. One `Gemini`
  instance is built once and handed to both sub-agents, so the model is
  configured in a single place. `rootAgent` is `@JvmField` for Java callers.
- **`ReviserAgent.kt`** — the most instructive file. `removeEndOfEditMark` is an
  `AfterModelCallback` that rewrites the `LlmResponse` before it is emitted: it
  walks `content.parts`, truncates the first part containing `END_MARK`, and
  drops every part after it. Note it uses `copy()` throughout — responses are
  immutable.
- **`CriticAgent.kt`** — minimal by comparison; the interesting line is
  `tools = listOf(GoogleSearchTool())`, which is all that grounding requires.
- **`CriticPrompt.kt` / `ReviserPrompt.kt`** — the prompts carry most of the
  behaviour. The reviser prompt is what instructs the model to end with
  `---END-OF-EDIT---`; the callback and the prompt are a matched pair, so
  changing one without the other breaks the output.
- **`Main.kt`** — three lines. `ReplRunner` supplies the interactive loop.
- **`WebMain.kt`** — the same agent served over HTTP via `AdkDevServer`.

## Data handling

Nothing is persisted. `WebMain.kt` uses `AdkServerConfig.inMemory()`, so session
and artifact state live in the process and vanish on exit. The critic sends
claim text to Google Search, so **user input reaches an external service** — do
not paste confidential content into it.

## Gotchas / things to know

- **The prompt and the callback are coupled.** `END_MARK` is defined alongside
  the reviser prompt and matched by the callback. Edit the prompt's terminator
  and the marker will start appearing in user-visible output.
- **The callback stops at the first matching part** (`break`), discarding later
  parts. That is deliberate — everything after the marker is scratch — but it
  means a response that legitimately continues after the marker loses content.
- **The dev server binds loopback.** Since adk-kotlin 0.9.0, `AdkDevServer`
  listens on `127.0.0.1`. Reaching it from a container or another machine needs
  an explicit `host` on `AdkServerConfig`; the README says how.
- **KSP is on the classpath but generates nothing.** There are no `@Tool`
  annotations here — the critic uses a built-in tool. The `ksp(...)` line is
  inert and kept only for symmetry with recipes that do define tools.
- **No Gradle wrapper.** CI falls back to the runner's `gradle`, so the build
  is not pinned to a Gradle version.

## Where to run things

All commands run from this directory (`core/kotlin/llm-auditor`). Requires
JDK 17+ and `GOOGLE_API_KEY`.

| Task | Command |
| --- | --- |
| Build | `gradle build` |
| CLI (REPL) | `gradle run` |
| Dev UI on :8080 | `gradle run -PmainClass=com.google.adk.samples.agents.llmauditor.WebMainKt` |
| What CI runs | `gradle test` (compiles; no tests exist) |
