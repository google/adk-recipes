# Agent-Agnostic Safety Plugins

For production input/output blocking, start with ADK's first-party
[`ModelArmorPlugin`](https://github.com/google/adk-python/blob/main/docs/guides/integrations/model_armor/index.md).
This recipe demonstrates custom callbacks for SDP text replacement before
session persistence and screening tool results, beyond the first-party
plugin's documented blocking behavior. It is an educational implementation,
not a production support commitment.

The recipe keeps ADK 1.28.0 in its lockfile. The local test suite also passes
with ADK 2.9.1 on Python 3.11; this compatibility check uses mocked Google
services and does not establish live Gemini behavior.

## Overview

This recipe provides a multi-agent system built with the Agent Development Kit (ADK), focusing on how to implement global safety guardrails using ADK's plugin feature. The system includes two distinct safety plugins: one that uses an agent as a judge and another that leverages the Model Armor API.

## Safety Plugins

The core of this example is demonstrating safety plugins using two different approaches: Gemini as a judge and Model Armor. Both plugins use hooks to send relevant messages and content to their respective safety filters, which then determine whether the content should be filtered or blocked.

Another key goal of these plugins is to prevent session poisoning by not saving harmful content to session memory, even if the initial LLM response correctly identified it as malicious. This is crucial because a class of safety vulnerabilities can exploit existing harmful messages in the session to elicit further unsafe responses from agents.

Together, the plugins demonstrate the following hooks:

* `on_user_message_callback`: Sends the user's message to the safety classifier. If the message is deemed unsafe, it is replaced with a message indicating that it was removed. The plugin then responds with a canned message.
* `before_tool_callback`: Sends the tool name and inputs to the safety classifier. If unsafe, the tool call is blocked and returns an error as if the tool itself had failed due to a safety violation. This hook is implemented by the LLM judge only.
* `after_tool_callback`: Sends the tool's output to the safety classifier. Performs the same action as `before_tool_callback`.
* `after_model_callback`: Sends the model's response to the safety classifier. If the response is detected as unsafe, it is replaced with a canned message stating that the model's response was removed.

The plugins are attached to the `Runner` in `main.py`, which is the ADK's main orchestrator, providing guardrails for all agents using the runner (i.e. the `root_agent` and `sub_agent`).

### Gemini as a Judge Plugin

The `LlmAsAJudge` plugin uses a large language model (LLM), configured through `MODEL_NAME_GENERATED_2`, to function as a safety filter. The LLM agent itself acts as the safety classifier.

**Configuration**

The `LlmAsAJudge` class can be configured via its constructor:

* `judge_agent`: You can specify which LLM agent to use as the judge. The default is `default_jailbreak_safety_agent`, which is an agent designed to respond with only "SAFE" or "UNSAFE," but you can swap it out with any other agent instance.

* `judge_on`: This set determines which callbacks will trigger the judge. By default, it's set to check the `USER_MESSAGE` and `TOOL_OUTPUT`, but you can add or remove checks for `BEFORE_TOOL_CALL` and `MODEL_OUTPUT`.

* `analysis_parser`: This is a function that parses the text output from the judge agent into a boolean value (True for unsafe, False for safe). By default, it checks if the string "UNSAFE" is present in the judge's response. You can implement your own parser to handle different judge outputs, allowing for custom safety logic.

### Model Armor Plugin

The `ModelArmorSafetyFilterPlugin` integrates safety with the [Model Armor API](https://docs.cloud.google.com/model-armor/overview). This plugin performs content safety checks by sending user prompts, tool outputs, and model responses to the Model Armor service.

If the API identifies any content violations based on the configured Model Armor template, it modifies the agent's flow, returning a predetermined message to the user, similar to the LLM judge plugin.

For user prompts, the plugin also supports Sensitive Data Protection (SDP) de-identification. Configure the Model Armor template with both an SDP inspect template and an SDP de-identify template. When SDP is the only matched filter, the plugin forwards the transformed text from `deidentifyResult.data.text` to the agent. An inspect-only SDP match, or SDP combined with another safety-filter match, is blocked.

For example, with a replacement transformation configured in the SDP de-identify template:

```text
User prompt: Contact me at alex@example.com.
Agent input: Contact me at ***.
```

The same policy applies to model responses and tool outputs: an SDP-only match
with usable transformed text is redacted; every other match blocks. Tool
results are screened as JSON with `sanitizeUserPrompt`, so prompt-injection
detection also covers data returned by tools. A transformed JSON object keeps
its structure; other replacements are returned under `redacted_output`.

Text replacement preserves adjacent non-text parts, including tool calls and
their signatures, when there is one plain public text part. A text-only user
message may also collapse multiple text parts into one replacement. If SDP
requests redaction of thought text, signed text, multiple model text parts,
or text interleaved with other user content, the message is blocked: the
aggregate replacement cannot safely be mapped back to those parts. Content
with no filter match is left unchanged. Preserved tool arguments and media
are not screened by this text-only path.

Timeouts, API errors, unsuccessful invocations and inconsistent verdicts block
content. Every returned filter must have a result, successful execution and an
explicit match verdict, even when the overall invocation reports success.
Calls use the asynchronous Google client, a transport deadline and an
application timeout, with automatic RPC retries disabled. Logs contain verdict
metadata, never prompts, responses, session state or exception messages.

The implementation is split into a reusable package:

| Module in `safety_plugins/plugins/model_armor/` | Responsibility |
| --- | --- |
| `constants.py` | Stages, actions, RPC names and replacement messages |
| `client.py` | Lazy regional client, credentials, RPC deadlines and cleanup |
| `response.py` | Protobuf validation, filter matches and SDP replacement text |
| `policy.py` | Pure `allow` / `redact` / `block` decisions |
| `telemetry.py` | Metadata construction without screened content |
| `logs.py` | Standard Python logging; no application-specific dependency |
| `plugin.py` | ADK callbacks and explicit external-payload screening |
| `__init__.py` | Public imports; no client creation at import time |

Copy this package to reuse Model Armor independently of the demo agents.
`ModelArmorSafetyFilterPlugin()` reads configuration at construction, or accepts
explicit project, location and template IDs. An injected `ModelArmorClient`
can also supply explicit Google credentials. Pass either `client` or explicit
project/location/template arguments; combining them raises `ValueError` rather
than silently ignoring configuration. Environment template settings are ignored
when a client is injected. `timeout_s` still applies in either case and must be
passed explicitly or supplied through `MODEL_ARMOR_TIMEOUT_S`.
Close the Runner with `async with` to release the transport.

Blocked user content is replaced in place before ADK saves it. A temporary
state marker stops the invocation in `before_run_callback`, with a
`before_model_callback` fallback. It is consumed after use so the next turn
can proceed. Applications loading external data in their own callbacks can
call `screen_external_payload()` before writing that data to state; that
method returns a safe dictionary or `None` when blocked.

Coverage is limited to text and JSON tool results. Tool arguments, images,
audio, existing session history and arbitrary state writes are not screened.
Use buffered model responses: this recipe does not guarantee safe streaming
of partial responses. The LLM judge remains a separate plugin.

*Note*: To use this plugin, create a [Model Armor template with advanced SDP settings](https://docs.cloud.google.com/model-armor/manage-templates#set-sensitive-data-protection-settings). Model Armor requires a regional endpoint and the caller needs the Model Armor User and Viewer roles. If the SDP templates live in another project, grant the Model Armor service agent the DLP User and Reader roles in that project. Set the project ID, region, and template ID through the environment variables below.

## Agent Details

| Feature | Description |
| --- | --- |
| **Interaction Type** | Conversational |
| **Complexity** | Medium |
| **Agent Type** | Multi Agent |
| **Components** | Plugins (LLM Judge, Model Armor), Tools |
| **Vertical** | Safety / Security |

## Quick Start with Google Agents CLI (Recommended)

The fastest way to get a production-ready version of this agent is using the
[Google Agents CLI](https://github.com/google/agents-cli). It scaffolds a full
project with CI/CD, deployment scripts, and best practices built in.

**Install the CLI** (one-time):

```bash
uvx google-agents-cli setup
```

**Create the project from this recipe** (replace `my-safety-plugins` with your project name):

```bash
agents-cli create my-safety-plugins -a adk@safety-plugins
```

This will:
- Copy the safety-plugins recipe into a new project
- Prompt you to select deployment options (Cloud Run, Agent Runtime, etc.)
- Generate CI/CD pipelines and infrastructure-as-code
- Set up a ready-to-deploy project structure

Once created, follow the generated project's README for deployment instructions.

## Setup and Installation (Local Development)

If you prefer to run the agent directly from this repository without the
starter pack scaffolding, follow the steps below.

### Prerequisites

*   Python 3.11+
*   [uv](https://docs.astral.sh/uv/) for dependency management and packaging.

    ```bash
    curl -LsSf https://astral.sh/uv/install.sh | sh
    ```

* A project on Google Cloud Platform
* [Google Cloud CLI](https://cloud.google.com/sdk/docs/install)

### Installation

```bash
# Clone this repository.
git clone https://github.com/google/adk-recipes.git
cd adk-recipes/core/python/safety-plugins
# Install the package and dependencies.
uv sync
```

### Configuration

1.  Copy `.env.example` to `.env` and fill in your project details:

    ```bash
    cp .env.example .env
    ```

2.  Required environment variables (set in `.env` or your shell):

    ```bash
    GOOGLE_GENAI_USE_VERTEXAI=1
    GOOGLE_CLOUD_PROJECT=<your-project-id>
    GOOGLE_CLOUD_LOCATION=global
    GOOGLE_CLOUD_MODEL_ARMOR_LOCATION=us-central1
    MODEL_ARMOR_TEMPLATE_ID=<your-template-id>  # Only required for the Model Armor plugin
    MODEL_ARMOR_TIMEOUT_S=5
    MODEL_NAME_GENERATED_1=gemini-3.5-flash
    MODEL_NAME_GENERATED_2=gemini-3.1-flash-lite
    ```

3.  Authenticate with Google Cloud:

    ```bash
    gcloud auth application-default login
    gcloud auth application-default set-quota-project $GOOGLE_CLOUD_PROJECT
    ```

The agent's `__init__.py` automatically loads `.env` via `python-dotenv`
and attempts to discover your GCP project via Application Default
Credentials (ADC). If ADC is configured, you only need to set
`GOOGLE_CLOUD_PROJECT` when your default project differs from what
`gcloud` returns.

Model Armor can use a separate project through
`GOOGLE_CLOUD_MODEL_ARMOR_PROJECT`; otherwise it uses `GOOGLE_CLOUD_PROJECT`.
Its location falls back to `GOOGLE_CLOUD_LOCATION` when the dedicated variable
is absent, but must be regional (`global` is rejected). The timeout is in
seconds and must be finite and positive. Set `MODEL_ARMOR_TIMEOUT_S` (the
provided `.env.example` uses `5`) or pass `timeout_s` explicitly; there is no
implicit timeout when the variable is absent.

## Running the Agent

### Using `adk` (standard ADK workflow)

ADK provides convenient ways to bring up the demo agents locally. These two
commands load `root_agent` without either safety plugin. Use the plugin CLI
below to run with guardrails.
You may talk to the agent using the CLI:

```bash
uv run adk run safety_plugins
```

Or on a web interface:

```bash
uv run adk web
```

The command `adk web` will start a web server on your machine and print the URL.
Select "safety_plugins" in the top-left drop-down menu.

### Using the plugin CLI (advanced)

To test the safety plugins specifically, use the `main.py` entry point with the
`--plugin` flag:

```bash
# LlmAsAJudge plugin
uv run python -m safety_plugins.main --plugin llm_judge

# Model Armor plugin
uv run python -m safety_plugins.main --plugin model_armor

# No safety filter (baseline)
uv run python -m safety_plugins.main
```

You can also modify `tools.py` to add text that the plugins will filter,
allowing you to see the safety hooks in action.

## Running Tests

Install the dev dependencies:

```bash
uv sync --group dev
```

Then run the tests from the `safety-plugins` directory:

```bash
uv run pytest tests
```

These tests use synthetic Google protobuf responses and a mocked Gemini
boundary. They require neither `.env` nor credentials. They cover response
validation, timeout handling, redaction, blocking, transport cleanup and the
content actually sent to the model and saved by an ADK Runner.

To validate a real Model Armor template, enable advanced SDP email replacement
and prompt-injection detection in that template, authenticate the chosen
gcloud account, then run:

```bash
uv run --frozen python scripts/live_model_armor.py \
  --project <your-project-id> \
  --account <your-gcloud-account> \
  --location us-central1 \
  --template <your-template-id> \
  --email-replacement '[REDACTED]'
```

This opt-in command makes real Model Armor calls with synthetic data and a
deterministic local model, so Gemini is not called. It checks benign input,
SDP transformation on user/model/tool content, injection blocking, tool-call
preservation during redaction, blocking of ambiguous thought/public responses,
and session history. Set `--email-replacement` to the exact replacement configured in your
SDP template. Success requires the expected transformed values at the model,
final-response and stored-session boundaries; plugin log messages do not
determine the result. The JSON report includes individual checks and the
probe's synthetic request/output values. It exits nonzero on failure. The access
token stays in memory; the command changes no active gcloud account, ADC
configuration or template.

## Customization

* **Custom judge agents**: Replace the default jailbreak judge with your own `LlmAgent` instance by passing a `judge_agent` parameter to `LlmAsAJudge()`.
* **Selective hooks**: Control which callbacks trigger the judge by adjusting the `judge_on` parameter.
* **Custom analysis parsers**: Implement your own parser function for `analysis_parser` to handle different judge output formats.
* **Model Armor templates**: Configure different Model Armor templates for different content safety requirements.

## Disclaimer

This agent recipe is provided for illustrative purposes only and is not intended for production use. It serves as a basic example of an agent and a foundational starting point for individuals or teams to develop their own agents.

This recipe has not been rigorously tested, may contain bugs or limitations, and does not include features or optimizations typically required for a production environment (e.g., robust error handling, security measures, scalability, performance considerations, comprehensive logging, or advanced configuration options).

Users are solely responsible for any further development, testing, security hardening, and deployment of agents based on this recipe. We recommend thorough review, testing, and the implementation of appropriate safeguards before using any derived agent in a live or critical system.
