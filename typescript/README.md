# Agent Development Kit (ADK) TypeScript Recipes

> [!IMPORTANT]
> **This folder is retired.** `typescript/agents/` no longer accepts new
> recipes, or changes to the recipes already here. Recipes now live in
> **`contrib/typescript/`**.
>
> - **Contributing a new recipe?** Start with the
>   [recipe checklist](../docs/recipe-checklist.md).
> - **Already have a recipe here?** Move it to `contrib/typescript/<name>` and
>   follow the same checklist. See the
>   [contributor guide](../docs/README.md).
>
> Pull requests that add or modify files under `typescript/agents/` fail CI.

[![License](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)

<img src="https://github.com/google/adk-docs/blob/main/docs/assets/agent-development-kit.png" alt="Agent Development Kit Logo" width="150">

This collection provides ready-to-use recipes built on top of
[Agent Development Kit TypeScript](https://github.com/google/adk-js). These agents
cover a range of common use cases and complexities, from simple conversational
bots to complex multi-agent workflows.

## 🚀 Getting Started with TypeScript Recipes

Follow these steps to set up and run the recipes:

1.  **Prerequisites:**
    *   **Install TypeScript ADK:** Ensure you have TypeScript Agent
        Development Kit installed and configured. Follow the Go instructions in the
        [ADK Installation Guide](https://google.github.io/adk-docs/get-started/installation/#typescript).
    *   **Set Up Environment Variables:** Each agent example relies on a `.env`
        file for configuration (like API keys, Google Cloud project IDs, and
        location). This keeps secrets out of the code.
        *   You will need to create a `.env` file in each agent's directory you
            wish to run (usually by copying the provided `.env.example`).
        *   Setting up these variables, especially obtaining Google Cloud
            credentials, requires careful steps. Refer to the **Environment
            Setup** section in the [ADK Installation
            Guide](https://google.github.io/adk-docs/get-started/installation/#go)
            for detailed instructions.
    *   **Google Cloud Project (Recommended):** While some agents might run
        locally with just an API key, most leverage Google Cloud services like
        Vertex AI and BigQuery. A configured Google Cloud project is highly
        recommended. See the
        [ADK Quickstart](https://google.github.io/adk-docs/get-started/typescript)
        for setup details.


2.  **Clone this repository:**

    To start working with the ADK TypeScript recipes, first clone the public `adk-recipes` repository:
    ```bash
    git clone https://github.com/google/adk-recipes.git
    cd adk-recipes/typescript
    ```

3.  **Explore the Agents:**

    *   Navigate to the `agents/` directory.
    *   The `agents/README.md` provides an overview and categorization of the available agents.
    *   Browse the subdirectories. Each contains a specific recipe with its own
    `README.md`.

4.  **Run an Agent:**
    *   Choose an agent from the `agents/` directory.
    *   Navigate into that agent's specific directory (e.g., `cd agents/customer_service`).
    *   Follow the instructions in *that agent's* `README.md` file for specific
        setup and running the agent.
    *   Browse the folders in this repository. Each agent and tool have its own
        `README.md` file with detailed instructions.

**Notes:**

These agents have been built and tested using
[Google models](https://cloud.google.com/vertex-ai/generative-ai/docs/learn/models)
on Vertex AI. You can test these recipes with other models as well. Please refer
to [ADK Tutorials](https://google.github.io/adk-docs/tutorials/) to use
other models for these recipes.

## 🧱 Repository Structure
```bash
.
├── typescript                  # Contains all the TypeScript recipe code
│   ├── agents                  # Contains individual agent recipes
│   │   ├── agent1              # Specific agent directory
│   │   │   └── README.md       # Agent-specific instructions
│   │   ├── agent2
│   │   │   └── README.md
│   │   ├── ...
│   └── README.md               # This file (Repository overview)
```
