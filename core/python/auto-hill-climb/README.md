# Auto Hill Climbing With Agents CLI

This repo starts with a customer support agent that breaks its own policy. You hand it to your coding agent with one prompt, and the coding agent fixes it by running evals, reading the failures, and editing the agent until every case passes.

You learn three things by running it.

- **Write the contract before the evals**
  - [`.agents-cli-spec.md`](.agents-cli-spec.md) states four rules the agent must follow. Every eval in `tests/eval/` checks one of them.
- **Grade with rules first, judges second**
  - Four metrics are plain Python checks on the trace. One binary judge grades tone and policy.
- **Let the coding agent climb, and check it did not cheat**
  - It edits only `app/agent.py`, never the evals. A held out set it never trains on proves the fix generalizes.

&nbsp;

# The Agent and Its Contract

The agent handles subscription cancellations, retention offers, and refunds with four tools.

| Rule | What the eval checks |
| :---- | :---- |
| Offer `FREE_MONTH` before cancelling | `offer_retention` is called on every eligible cancellation |
| Never discount an account that already has one | `offer_retention` is never called for `ACC-123` |
| Quote the exact cancellation date | The reply contains `2026-11-15`, the date the tool returns |
| Escalate every refund | `escalate_to_human` is called on any refund request |

&nbsp;

The starting instruction is one sentence: `Help the user. If they want to cancel, maybe offer them a discount.` It fails 3 of 8 training cases.

&nbsp;

# Run It

## Prerequisites

- A Google Cloud project with the Vertex AI API enabled, and `gcloud auth application-default login` done.
- [`uv`](https://docs.astral.sh/uv/) installed.
- A coding agent that can run shell commands, such as Antigravity, Claude Code, or Codex.

&nbsp;

## Set Up

```bash
git clone https://github.com/google/adk-recipes.git
cd adk-recipes/core/python/auto-hill-climb
cp .env.example .env   # then set GOOGLE_CLOUD_PROJECT
uv tool install google-agents-cli
uvx google-agents-cli setup
agents-cli install
```

`agents-cli setup` installs the eval skill your coding agent uses to run the loop.

&nbsp;

## Grade the Starting Agent

```bash
make baseline
```

Expect 5 of 8 training cases to pass. The report in `artifacts/grade_results/baseline/` names the failing case and metric for each miss.

&nbsp;

## Start the Hill Climb

Open your coding agent in the repo and paste the prompt in [`KICKOFF_PROMPT.md`](KICKOFF_PROMPT.md). Then let it run.

&nbsp;

## What to Watch

- **Each round fixes one root cause**
  - The coding agent reads a failure, edits the instruction in `app/agent.py`, runs `make round`, and compares against the previous run.
- **The evals never change**
  - `git diff tests/eval` stays empty for the whole run. If it changes, the coding agent moved the bar instead of fixing the agent.
- **Held out runs once, at the end**
  - `make heldout` grades 4 cases the coding agent never trained on.
- **The report lands in `agent_report.md`**
  - It lists the pass rate per round and what changed.

&nbsp;

# Compare With Our Run

`solution/` holds the agent after a coding agent climbed it, plus its report. It went from 5 of 8 to 8 of 8 on training in three rounds and passed 4 of 4 held out cases.

```bash
diff app/agent.py solution/agent.py
```

&nbsp;

# Layout

| Path | Purpose |
| :---- | :---- |
| `app/agent.py` | The agent, its tools, and its instruction |
| `.agents-cli-spec.md` | The contract |
| `tests/eval/eval_config.yaml` | Four rule metrics and one judge |
| `tests/eval/metrics/` | The rule metrics, one Python function each |
| `tests/eval/datasets/train.json` | 8 training cases |
| `tests/eval/datasets/heldout.json` | 4 held out cases |
| `KICKOFF_PROMPT.md` | The one prompt you give your coding agent |
| `Makefile` | `baseline`, `round`, `heldout`, and `compare` targets |
| `solution/` | The agent after one autonomous climb, and its run report |
