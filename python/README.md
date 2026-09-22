# Agent Development Kit (ADK) Python Recipes

> [!IMPORTANT]
> **This folder is retired.** `python/agents/` no longer accepts new
> recipes, or changes to the recipes already here. Recipes now live in
> **`contrib/python/`**.
>
> - **Contributing a new recipe?** Start with the
>   [recipe checklist](../docs/recipe-checklist.md).
> - **Already have a recipe here?** Move it to `contrib/python/<name>` and
>   follow the same checklist. See the
>   [contributor guide](../docs/README.md).
>
> Pull requests that add or modify files under `python/agents/` fail CI.

## Moved recipes

Recipes used to live at `python/agents/<recipe>`. Links to the old
paths return a 404 — GitHub does not redirect a moved directory —
so update any link you own:

| Old path | Current location |
| :--- | :--- |
| `python/agents/ambient-expense-agent` | [`core/python/ambient-expense-agent`](../core/python/ambient-expense-agent) |
| `python/agents/brand-search-optimization` | [`contrib/python/brand-search-optimization`](../contrib/python/brand-search-optimization) |
| `python/agents/deep-search` | [`core/python/deep-search`](../core/python/deep-search) |
| `python/agents/financial-advisor` | [`contrib/python/financial-advisor`](../contrib/python/financial-advisor) |
| `python/agents/genmedia-for-commerce` | [`core/python/genmedia-for-commerce`](../core/python/genmedia-for-commerce) |
| `python/agents/llm-auditor` | [`contrib/python/llm-auditor`](../contrib/python/llm-auditor) |
| `python/agents/multiformat-hybrid-rag` | [`contrib/python/multiformat-hybrid-rag`](../contrib/python/multiformat-hybrid-rag) |
| `python/agents/on-brand-genmedia` | [`contrib/python/on-brand-genmedia`](../contrib/python/on-brand-genmedia) |
| `python/agents/safety-plugins` | [`core/python/safety-plugins`](../core/python/safety-plugins) |
| `python/agents/software-bug-assistant` | [`contrib/python/software-bug-assistant`](../contrib/python/software-bug-assistant) |

Any other recipe that lived here has been removed. Browse
[`core/`](../core/) and [`contrib/`](../contrib/) for the current
collection.

[![License](https://img.shields.io/badge/License-Apache_2.0-blue.svg)](LICENSE)

<img src="https://github.com/google/adk-docs/blob/main/docs/assets/agent-development-kit.png" alt="Agent Development Kit Logo" width="150">

This collection provides ready-to-use recipes built on top of Python
[Agent Development Kit](https://github.com/google/adk-python). These agents
cover a range of common use cases and complexities, from simple conversational
bots to complex multi-agent workflows.

## 🚀 Getting Started with Python Recipes

Follow these steps to set up and run the recipes:

1.  **Prerequisites:**
    *   **Install Python ADK:** Ensure you have Python Agent
        Development Kit installed and configured. Follow the Python instructions in the
        [ADK Installation Guide](https://google.github.io/adk-docs/get-started/installation/#python).
    *   **Set Up Environment Variables:** Each agent example relies on a `.env`
        file for configuration (like API keys, Google Cloud project IDs, and
        location). This keeps secrets out of the code.
        *   You will need to create a `.env` file in each agent's directory you
            wish to run (usually by copying the provided `.env.example`).
        *   Setting up these variables, especially obtaining Google Cloud
            credentials, requires careful steps. Refer to the **Environment
            Setup** section in the [ADK Installation
            Guide](https://google.github.io/adk-docs/get-started/installation/#python)
            for detailed instructions.
    *   **Google Cloud Project (Recommended):** While some agents might run
        locally with just an API key, most leverage Google Cloud services like
        Vertex AI and BigQuery. A configured Google Cloud project is highly
        recommended. See the
        [ADK Quickstart](https://google.github.io/adk-docs/get-started/quickstart/#python)
        for setup details.


2.  **Clone this repository:**

    To start working with the ADK Python recipes, first clone the public `adk-recipes` repository:
    ```bash
    git clone https://github.com/google/adk-recipes.git
    cd adk-recipes/python
    ```

3.  **Explore the Agents:**

    *   Navigate to the `agents/` directory.
    *   The `agents/README.md` provides an overview and categorization of the available agents.
    *   Browse the subdirectories. Each contains a specific recipe with its own
    `README.md`.

4.  **Run an Agent:**
    *   Choose an agent from the `agents/` directory.
    *   Navigate into that agent's specific directory (e.g., `cd agents/llm-auditor`).
    *   Follow the instructions in *that agent's* `README.md` file for specific
        setup (like installing dependencies via `poetry install`) and running
        the agent.
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
├── python                      # Contains all the Python recipe code
│   ├── agents                  # Contains individual agent recipes
│   │   ├── agent1              # Specific agent directory
│   │   │   └── README.md       # Agent-specific instructions
│   │   ├── agent2
│   │   │   └── README.md
│   │   ├── ...
│   │   └── README.md           # Overview and categorization of agents
│   └── README.md               # This file (Repository overview)
```

## Local Contributor Pre-Check

Before submitting a Pull Request with changes to Python files, please run the following script locally to quickly validate your changes against our standards.

1.  Ensure you have **Python** installed and the script is executable (`chmod +x python-checks.sh`).
2.  Run the script from the repository root using a **run flag**, specifying the **relative path** to the agent or notebook folder you modified.

| Purpose | Command |
| :--- | :--- |
| **Run all checks** (Black, iSort, Flake8) | `./python-checks.sh --run-all agents/agent_directory_name` |
| **Run only `flake8`** (Linting) | `./python-checks.sh --run-lint agents/agent_directory_name` |
| **Run only `black`** (Formatting) | `./python-checks.sh --run-black notebooks/notebook_directory_name` |
| **Run only `isort`** (Import Sorting) | `./python-checks.sh --run-isort notebooks/notebook_directory_name` |
| **Get detailed usage and options** | `./python-checks.sh --help` |

> **Note:** The script requires the full relative path starting with `agents/` or `notebooks/` (e.g., `agents/academic-research`). This ensures checks are scoped strictly to the component you are working on.

## 📝 Code Quality Checks

We use automated checks to ensure high quality and consistency across all recipes.

This script will run `black`, `isort` and `flake8` to check for formatting and linting errors.

## ℹ️ Getting help

If you have any questions or if you found any problems with this repository,
please report through
[GitHub issues](https://github.com/google/adk-recipes/issues).

## 🤝 Contributing

We welcome contributions from the community! Whether it's bug reports, feature
requests, documentation improvements, or code contributions, please see our
[**Contributing Guidelines**](https://github.com/google/adk-recipes/blob/main/CONTRIBUTING.md)
to get started.

## 📄 License

This project is licensed under the Apache 2.0 License - see the
[LICENSE](https://github.com/google/adk-recipes/blob/main/LICENSE) file for
details.

## Disclaimers

This is not an officially supported Google product. This project is not eligible
for the
[Google Open Source Software Vulnerability Rewards Program](https://bughunters.google.com/open-source-security).

The agents in this project are intended for demonstration purposes only. They is
not intended for use in a production environment.
