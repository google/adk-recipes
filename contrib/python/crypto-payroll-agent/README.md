# Crypto Payroll Agent

A Google ADK recipe that batch-pays multiple recipients in ETH or
ERC-20 tokens on [Base](https://base.org) in a single transaction.

Built on the [Spraay batch payment tools](https://github.com/google/adk-python-community/pull/95)
in `google-adk-community`. Demonstrates a complete payroll, airdrop, and
revenue-split workflow with built-in safety gates.

## What this agent does

A treasury operator says something like:

> *"Pay 250 USDC each to 0x9f2c…81a3, 0x4ab7…d109, and 0x742d…f44e."*

Recipients must be 0x addresses. ENS names are not supported: the
guardrail described under [Safety gates](#safety-gates) refuses any
recipient that is not a 0x-prefixed, 40-hex-character address, and
nothing in this recipe resolves names to addresses. Resolve them
yourself before handing the list to the agent.

…or:

> *"Split this 10,000 USDC airdrop across these 50 addresses by their
> contribution weights."*

The agent:

1. Identifies whether the payment is **equal** (use `spraay_batch_eth` /
   `spraay_batch_token`) or **variable** (use the `_variable` siblings).
2. Looks up token metadata (decimals, canonical address) via the local
   token registry so the user never has to know that USDC has 6 decimals.
3. For variable distributions, splits a pool proportionally with the
   `split_pool_proportionally` helper.
4. Presents a single-screen plan: recipient count, amounts, total,
   estimated gas savings vs. individual transfers.
5. Waits for explicit user confirmation, then executes the batch on Base
   through the Spraay protocol.

## Why this is interesting

Most recipes demo one or two narrow capabilities. This one shows
ADK's tool-selection in action: the model must pick the correct one of
four batch tools (eth vs. token × equal vs. variable) from free-form user
input, plus chain a helper tool when the user provides weights instead of
explicit per-recipient amounts. It's a small but realistic showcase of
multi-tool composition.

## Prerequisites

- Python 3.11+
- `uv` package manager: `pip install uv`
- A Base wallet with a small amount of ETH for gas and the token(s) you
  want to send. Base testnet (Sepolia) works for development; set
  `SPRAAY_RPC_URL` to a Sepolia endpoint.

## Setup

```bash
git clone https://github.com/google/adk-samples.git
cd adk-samples/contrib/python/crypto-payroll-agent
uv sync
cp .env.example .env
# Edit .env: at minimum, set SPRAAY_PRIVATE_KEY and your Google Cloud project
```

> **Note:** the Spraay batch tools merged in
> [adk-python-community#95](https://github.com/google/adk-python-community/pull/95)
> ship from PyPI as of `google-adk-community` 0.5.0, which is the version
> `uv.lock` resolves and the tests run against. The dependency is declared
> as `google-adk-community[spraay]` — the extra is required, not
> cosmetic: `web3` is an optional dependency declared under it, and the
> batch tools import it at call time.

After `uv sync`, verify the four Spraay tools import correctly:

```bash
uv run python -c "from google.adk_community.tools.spraay import \
  spraay_batch_eth, spraay_batch_token, \
  spraay_batch_eth_variable, spraay_batch_token_variable; \
  print('Spraay tools ready')"
```

## Run the agent

```bash
uv run adk web
```

Then open `http://localhost:8000`, choose **crypto_payroll_agent**, and
try the example prompts below.

## Example interactions

### 1. Equal-amount payroll (`spraay_batch_token`)

> *Pay 250 USDC each to 0x9f2c…81a3, 0x4ab7…d109, and 0x8eee…2017 on Base.*

The agent looks up USDC's address and decimals, summarizes the plan, and
on `confirm` calls `spraay_batch_token` with `amount_per_recipient="250"`
and `token_decimals=6`.

### 2. Equal-amount ETH bounties (`spraay_batch_eth`)

> *Send 0.01 ETH each to these 12 bounty hunters: [list of 12 addresses].*

The agent calls `spraay_batch_eth` with
`amount_per_recipient_eth="0.01"`, unless the total breaches
`PAYROLL_MAX_BATCH_ETH` — see [Safety gates](#safety-gates).

### 3. Variable token amounts (`spraay_batch_token_variable`)

> *Pay this month's contractors. Alice gets 1500 USDC, Bob 2200, Carol
> 800.*

The agent extracts the per-recipient amounts and calls
`spraay_batch_token_variable`.

### 4. Proportional split (`split_pool_proportionally` + `spraay_batch_token_variable`)

> *Distribute 10,000 USDC to these 5 contributors by their commit counts:
> 120, 80, 45, 30, 25.*

The agent calls `split_pool_proportionally` first to compute per-recipient
amounts (rounding-safe so the total is exact), then executes the variable
batch.

## Configuration

| Variable | Required | Description |
| --- | --- | --- |
| `SPRAAY_PRIVATE_KEY` | yes | Private key of the sending wallet on Base |
| `SPRAAY_RPC_URL` | no | Base RPC endpoint (default: `https://mainnet.base.org`) |
| `SPRAAY_CONTRACT_ADDRESS` | no | Override the Spraay batch contract |
| `GOOGLE_CLOUD_PROJECT` | yes¹ | GCP project for Vertex AI |
| `GOOGLE_CLOUD_LOCATION` | yes¹ | GCP region (e.g. `us-central1`) |
| `GOOGLE_GENAI_USE_VERTEXAI` | yes¹ | `1` for Vertex AI, `0` for AI Studio |
| `GOOGLE_API_KEY` | yes² | AI Studio key (alternative to Vertex AI) |
| `GOOGLE_CLOUD_STAGING_BUCKET` | yes³ | GCS bucket for Agent Engine staging (`gs://…`) |
| `PAYROLL_AGENT_MODEL` | yes | Gemini model. No code default — `config.py` raises if it is unset; `.env.example` ships `gemini-3.5-flash` |
| `PAYROLL_MAX_BATCH_USD` | yes | Refuse $1-pegged token batches above this total (default: `10000`) |
| `PAYROLL_MAX_BATCH_ETH` | yes | Refuse ETH batches above this total, in ETH (default: `5`) |

¹ if using Vertex AI · ² if using AI Studio · ³ for `deployment/deploy.py` only

## Safety gates

The spend ceilings and batch limits are enforced in
`crypto_payroll_agent/guardrails.py`, wired in as the agent's
`before_tool_callback`. It runs before any `spraay_batch_*` tool can
sign, so a call that breaches a limit never reaches the chain — the
callback returns an error dict and ADK hands that back to the model in
place of the tool result. The system instruction states the same limits,
but only the callback enforces them: prose alone would not survive a
prompt-injected recipient list or an ordinary model mistake.

Every `spraay_batch_*` call is checked for:

| Check | Limit |
| --- | --- |
| Recipient count | ≤ 200 (Spraay protocol limit) |
| Recipient addresses | every entry `0x` + 40 hex characters |
| Amounts | present, numeric, total > 0 |
| ETH batches | total ≤ `PAYROLL_MAX_BATCH_ETH` |
| $1-pegged token batches | total ≤ `PAYROLL_MAX_BATCH_USD` |
| Any other token | **blocked** |

The ceilings bound the amount reaching recipients. Spraay's 0.3%
protocol fee is charged on top of that, so actual spend can exceed the
ceiling by up to 0.3%.

**Why non-pegged tokens are blocked.** Valuation is deliberately
offline. `USDC`, `USDbC` and `DAI` are valued at $1 each, which needs no
price feed. ETH is bounded by its own ceiling in ETH rather than
converted. Every other ERC-20 — `WETH`, `cbETH`, `cbBTC`, `AERO`, or any
address outside the bundled registry — has no offline USD value, so the
USD ceiling cannot be applied to it and the batch is refused rather than
waved through unchecked.

That is a deliberate trade: this recipe would rather refuse a legitimate
`cbBTC` payroll run than let an unbounded one through. If you need those
tokens, `enforce_batch_limits` is the single customization point — add a
price source there and value the batch before comparing it to the
ceiling.

Two gates remain the model's alone, because no code can check them:
showing a plan and obtaining explicit confirmation before executing, and
re-presenting the plan when an input changes mid-conversation.

## Agent structure

```
crypto_payroll_agent/
├── agent.py          # LlmAgent wired to 4 Spraay tools + 2 helpers
├── prompt.py         # System instruction governing tool selection
├── config.py         # Model + safety + token registry
├── guardrails.py     # before_tool_callback enforcing the spend ceilings
└── tools/
    └── helpers.py    # lookup_token_info, split_pool_proportionally
```

The agent has six tools total:

| Tool | Source | Purpose |
| --- | --- | --- |
| `spraay_batch_eth` | `google.adk_community.tools.spraay` | Equal ETH to N recipients |
| `spraay_batch_token` | same | Equal ERC-20 to N recipients |
| `spraay_batch_eth_variable` | same | Variable ETH amounts |
| `spraay_batch_token_variable` | same | Variable token amounts |
| `lookup_token_info` | local | Resolve symbol → {address, decimals} |
| `split_pool_proportionally` | local | Divide a pool by weights (rounding-safe) |

## Evaluation

```bash
uv run adk eval crypto_payroll_agent eval/crypto_payroll_eval_set.evalset.json
```

The evalset covers all four batch-tool selection paths plus a safety-gate
refusal case.

## Testing

```bash
uv run pytest tests/ -v
```

All tests are offline, and nothing is mocked. The local helpers and the
`before_tool_callback` guardrail are called directly — both are pure
functions of their arguments, so they need no chain, RPC endpoint, or
signing key. The Spraay batch tools themselves are never exercised here;
they are covered upstream in `google-adk-community`. The one test that
touches them only asserts the assembled agent lists them as tools.

## Deployment

See `deployment/deploy.py` for a reference Vertex AI Agent Engine
deployment script.

> **Note:** the script passes `PAYROLL_AGENT_MODEL`,
> `PAYROLL_MAX_BATCH_USD` and `PAYROLL_MAX_BATCH_ETH` to the engine —
> every variable `config.py` requires, which is enough for the agent to
> start. It does **not** pass `SPRAAY_PRIVATE_KEY`, which the Spraay
> tools need at tool-call time — so a deployed engine imports fine but
> fails the moment it tries to sign a batch. Supply that key from
> [Secret Manager](https://cloud.google.com/secret-manager) rather than
> as a plaintext `env_vars` entry: an Agent Engine env var is readable by
> anyone who can describe the resource, and this key controls real funds.

## Protocol details

- Spraay batch contract: `0x1646452F98E36A3c9Cfc3eDD8868221E207B5eEC`
  on Base mainnet
- Up to 200 recipients per transaction
- ~80% gas savings vs. individual transfers
- 0.3% protocol fee
- More info: [spraay.app](https://spraay.app)

This is a community recipe. Spraay is not an official Google product.

## License

Apache 2.0 — see the repository root LICENSE.
