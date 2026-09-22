# Permissions that narrow across a transfer

A customer-service coordinator transfers a billing question to a
`billing_agent` sub-agent. The coordinator is allowed to issue refunds.
The sub-agent is not: it is delegated read access to billing and nothing
else. When it calls `issue_refund` anyway, the call is refused at the
plugin callback and the function is never entered.

The enforcement comes from [attenu-guard](https://github.com/attenu-io/attenu-guard),
an Apache-2.0 library. It ships an ADK plugin,
`attenu_guard.adapters.google_adk.DelegationGuardPlugin`, registered once
on the `App`. ADK itself is unmodified.

## What this recipe teaches

- **A sub-agent's permission set is computed from its parent's.** The
  child receives the meet of what it requests and what the parent holds,
  so a hand-off can only ever narrow. Asking for `admin.root` when the
  parent does not hold it yields nothing, not an error you have to
  remember to check.
- **Refusal happens before the tool body runs.** `before_tool_callback`
  returns the refusal as the tool's result, so ADK skips the call
  entirely (`google/adk/flows/llm_flows/functions.py`). Every tool in
  `app/tools.py` records its own entry as its first statement, which is
  how the tests tell "refused" apart from "ran, then reported".
- **Undeclared means denied.** An agent with no entry in `DELEGATIONS`
  holds nothing; a tool with no entry in `TOOLS` is checked against a
  scope no permission set grants. Both fail closed and both land in the
  ledger with a reason code.
- **The hand-off itself is checked.** `delegation_scope` makes each
  transfer an authorization decision of its own,
  `agent.delegate.<target>`, against the permissions of the agent doing
  the handing off. The coordinator holds `agent.delegate.*`; the billing
  agent holds no delegate scope, so it cannot pass the task on.
- **The run leaves evidence.** Decisions append to a hash-chained
  ledger. `demo.py` exports a signed bundle and verifies it with the
  packaged `attenu-guard verify` command — integrity, child-within-parent
  and containment, with no engine, no service and no network involved.
  The ledger is tamper-evident, not tamper-proof: a verifier detects an
  edit, it does not prevent one.

## Prerequisites

- Python 3.11 or newer
- [uv](https://github.com/astral-sh/uv)
- No API key and no cloud project for the default run. A live run needs
  a model and credentials; see `.env.example`.

## Setup

```bash
uv sync
cp .env.example .env   # only needed for a live run
```

## Run

Offline, with a scripted model that replays a fixed list of function
calls. The `Runner`, the flows, the callbacks and the plugin manager are
all the real ones; only the model is substituted.

```bash
uv run python demo.py
```

Tests:

```bash
uv run pytest
```

Live, against a real model. Set `MODEL_NAME` and your credentials in
`.env` first (required for live runs; the offline demo never reads them).

```bash
uv run python demo.py --live
uv run adk run app        # or: uv run adk web
```

`adk run` and `adk web` load the module-level `app` object, which is the
`App` with the plugin already attached, so a CLI-driven run is guarded
too. That object is built once, at import, so every session `adk web`
serves shares one root permission set and one ledger. Fine for trying the
recipe out; call `build_app()` per run where one caller's decisions
should not be another's evidence, as `demo.py` and the tests do.

## Expected output

Abridged; the run prints the full transcript.

```text
1. one turn, two agents
    [coordinator] calls lookup_order({'order_id': 'ORD-8812'})
    [coordinator] calls transfer_to_agent({'agent_name': 'billing_agent'})
    [billing_agent] calls get_invoice({'invoice_id': 'INV-4471'})
    [billing_agent] <- DENIED issue_refund
    (ok responses elided)

2. what each agent holds
    coordinator : Authority(scopes=['agent.delegate.*', 'billing.*', 'mail.send', 'orders.read'], ceilings=[...], ttl=3600)
    billing     : Authority(scopes=['billing.read'], ceilings=[...], ttl=900)
    billing is narrower than coordinator: True

3. the refusal
    tool bodies that ran: [('lookup_order', 'ORD-8812'), ('get_invoice', 'INV-4471')]
    issue_refund response: {'error': 'authority_denied', ... 'reasons': ['scope_not_granted', 'ceiling_exceeded'], ...}

4. asking for more does not produce more
    requested: Authority(scopes=['admin.root', 'billing.*'], ...)
    granted  : Authority(scopes=['billing.*'], ...)
    granted is narrower than coordinator: True

5. the ledger, checked without this process
    7 events, hash chain: True
    integrity=True monotonicity=True containment=True anchor=verified nodes=2 actions_checked=3
OK

RESULT: OK
```

An offline run prints a few ADK advisory warnings before section 1; they are expected.

`issue_refund` is absent from the list of tool bodies that ran.

## Trust boundary

The adversary this addresses is the agent itself — a sub-agent steered
by a poisoned document, a confused plan, or a bad tool description into
asking for something outside its remit. The enforcement point runs
in-process, at the ADK callback, and holds:

- as long as the plugin is attached to the `App`. `app/agent.py` calls
  `require_guard()`, which turns a removed plugin into a startup failure
  rather than a silent downgrade.
- for anything routed through ADK's tool dispatch. Code that reaches a
  side effect without going through a tool call is outside the checked
  path.
- against permissions, not against content. The library takes no view on
  whether the refund is a good idea; it holds the billing agent to what
  it was delegated.

It does not defend against an attacker with code execution in the same
process, who can edit the permission sets in `app/permissions.py` before
they are loaded. Exported evidence is verified against a public key, so
a bundle altered after export fails verification with the key alone.

Writing the permission sets is your job, deliberately: `app/permissions.py`
is a short, reviewable file, and the library enforces exactly what it
says.

## Files

| Path | What it holds |
|---|---|
| `app/permissions.py` | The three declarations: what the root holds, what each sub-agent may request, what each tool needs |
| `app/agent.py` | The agent tree, the plugin registration, `require_guard()` |
| `app/tools.py` | Four tools over a small in-memory order book |
| `app/prompt.py` | Instructions for both agents |
| `demo.py` | The scripted-model run, the evidence export, the offline verification |
| `tests/` | Runnability plus the enforcement assertions |

Versions this was checked against: `google-adk` 2.7.1, `attenu-guard`
0.10.0, Python 3.11.

## License

Apache-2.0, matching this repository and the library.
