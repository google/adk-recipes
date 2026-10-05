# Agent-Agnostic Safety Plugins

## Overview

This repository provides an example of a multi-agent system built with the Agent Development Kit (ADK), focusing on how to implement global safety guardrails using ADK's plugin feature. The system includes two distinct safety plugins: one that uses an agent as a judge and another that leverages the Model Armor API.

## Safety Plugins

The core of this example is demonstrating safety plugins using two different approaches: Gemini as a judge and Model Armor. Both plugins use hooks to send relevant messages and content to their respective safety filters, which then determine whether the content should be filtered or blocked.

Another key goal of these plugins is to prevent session poisoning by not saving harmful content to session memory, even if the initial LLM response correctly identified it as malicious. This is crucial because a class of safety vulnerabilities can exploit existing harmful messages in the session to elicit further unsafe responses from agents.

Both plugins operate on the following hooks:

* `on_user_message_callback`: Sends the user's message to the safety classifier. If the message is deemed unsafe, it is replaced with a message indicating that it was removed. The plugin then responds with a canned message.
* `before_tool_callback`: Sends the tool name and inputs to the safety classifier. If unsafe, the tool call is blocked and returns an error as if the tool itself had failed due to a safety violation.
* `after_tool_callback`: Sends the tool's output to the safety classifier. Performs the same action as `before_tool_callback`.
* `after_model_callback`: Sends the model's response to the safety classifier. If the response is detected as unsafe, it is replaced with a canned message stating that the model's response was removed.

The plugins are attached to the `Runner` in `main.py`, which is the ADK's main orchestrator, providing guardrails for all agents using the runner (i.e. the `root_agent` and `sub_agent`).

### Gemini as a Judge Plugin

The `LlmAsAJudge` plugin uses a large language model (LLM), specifically Gemini 2.5 Flash Lite, to function as a safety filter. The LLM agent itself acts as the safety classifier.

**Configuration**

The `LlmAsAJudge` class can be configured via its constructor:

* `judge_agent`: You can specify which LLM agent to use as the judge. The default is `default_jailbreak_safety_agent`, which is an agent designed to respond with only "SAFE" or "UNSAFE," but you can swap it out with any other agent instance.

* `judge_on`: This set determines which callbacks will trigger the judge. By default, it's set to check the `USER_MESSAGE` and `TOOL_OUTPUT`, but you can add or remove checks for `BEFORE_TOOL_CALL` and `MODEL_OUTPUT`.

* `analysis_parser`: This is a function that parses the text output from the judge agent into a boolean value (True for unsafe, False for safe). By default, it checks if the string "UNSAFE" is present in the judge's response. You can implement your own parser to handle different judge outputs, allowing for custom safety logic.

### Model Armor Plugin

The `ModelArmorSafetyFilterPlugin` integrates safety with the [Model Armor API](https://cloud.google.com/security-command-center/docs/model-armor-overview). This plugin performs content safety checks by sending user prompts and model responses to the Model Armor service.

If the API identifies any content violations based on the configured Model Armor template, it modifies the agent's flow, returning a predetermined message to the user, similar to the LLM judge plugin.

*Note*: To use this plugin, you must have a Model Armor template in a Google Cloud Platform (GCP) project. Please refer to the [official documentation on how to create a template](https://cloud.google.com/security-command-center/docs/manage-model-armor-templates). Once you have created a template, you will need to modify the plugin's constructor parameters with your project ID, location ID, and template ID to proceed.

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

*   Python 3.12+
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
    GOOGLE_CLOUD_LOCATION=us-central1
    MODEL_ARMOR_TEMPLATE_ID=<your-template-id>  # Only required for the Model Armor plugin
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

## Running the Agent

### Using `adk` (standard ADK workflow)

ADK provides convenient ways to bring up agents locally and interact with them.
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

## Customization

* **Custom judge agents**: Replace the default jailbreak judge with your own `LlmAgent` instance by passing a `judge_agent` parameter to `LlmAsAJudge()`.
* **Selective hooks**: Control which callbacks trigger the judge by adjusting the `judge_on` parameter.
* **Custom analysis parsers**: Implement your own parser function for `analysis_parser` to handle different judge output formats.
* **Model Armor templates**: Configure different Model Armor templates for different content safety requirements.

## Disclaimer

This agent recipe is provided for illustrative purposes only and is not intended for production use. It serves as a basic example of an agent and a foundational starting point for individuals or teams to develop their own agents.

This recipe has not been rigorously tested, may contain bugs or limitations, and does not include features or optimizations typically required for a production environment (e.g., robust error handling, security measures, scalability, performance considerations, comprehensive logging, or advanced configuration options).

Users are solely responsible for any further development, testing, security hardening, and deployment of agents based on this recipe. We recommend thorough review, testing, and the implementation of appropriate safeguards before using any derived agent in a live or critical system.
