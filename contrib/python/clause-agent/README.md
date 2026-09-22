# ClauseIQ

An ADK multi-agent that resolves contract hierarchy/precedence conflicts,
extracts billing-relevant clauses with citations, and treats human
corrections (Legal + Billing) as durable, scoped memory instead of one-off
answers.

## Architecture

- `root_agent` (Orchestrator) -- routes each question to a sub-agent via
  the built-in `transfer_to_agent`; sub-agents always hand control back to
  `root_agent` once they've delivered a final answer for the current
  question (verified in `tests/test_end_to_end_trace.py`).
- `hierarchy_resolver` -- resolves which contract/clause controls when
  documents conflict. Escalates precedent-setting/low-confidence rulings to
  a human Legal Reviewer via a `LongRunningFunctionTool`
  (`request_legal_review`) and only writes a ruling to Memory Bank once
  approved.
- `clause_extractor` -- extracts a specific billing field's value with a
  citation; accepts corrections from Billing/AR users and generalizes
  lessons into reusable Memory Bank rules.
- Memory Bank (`clause_agent/tools/memory_bank.py`) -- structured, scoped
  facts/rules behind a swappable backend (`LocalJsonMemoryBankBackend` for
  the POC; implement `MemoryBankBackend` against real Vertex AI Memory
  Bank for production without touching agent/tool code).
- SAP connector -- mocked per PRD scope.

See `clause_agent/prompt.py` for the exact guardrail language each agent
follows, and `clause_agent/tools/` for how each guardrail is *structurally*
enforced (not just prompted), e.g. `memory_bank_create` hard-refuses an
unapproved precedence ruling.

## Setup and Installation

Prerequisites and environment configuration:

```bash
# 1. Install dependencies
uv sync --dev
source .venv/bin/activate

# 2. Configure environment variables
cp .env.example .env  # fill in GOOGLE_API_KEY (or Vertex project config)
```

## Running the Agent

Start the agent using the ADK CLI from the recipe root directory:

```bash
uv run adk web .   # or: uv run adk run .
```

### Resolving a pending Legal review

When `hierarchy_resolver` escalates, it stays paused (waiting) rather than
guessing. List and resolve pending tasks with:

```bash
python scripts/legal_review_cli.py list
python scripts/legal_review_cli.py approve LR-a1b2c --approver legal@example.com \
    --comment "Confirmed."
```

Then, in the `adk web`/`adk run` chat, tell the agent the task was
resolved (e.g. *"LR-a1b2c was approved by legal@example.com: Confirmed."*)
so it can look it up and continue -- the CLI script only updates the local
queue; it does not talk to a live agent session directly (see the script's
docstring for why).

### Resetting to a blank slate

Memory Bank, the audit log, and the Legal-review queue are all file-backed
and persist across `adk web`/`adk run` restarts by design (that's the
point -- ClauseIQ remembers). To replay a demo from scratch, stop the
server first, then:

```bash
python scripts/reset_state.py            # asks for confirmation
python scripts/reset_state.py --yes      # skip the prompt
python scripts/reset_state.py --dry-run  # show what would be deleted
python scripts/reset_state.py --keep-sessions  # keep ADK chat history,
                                                # only reset ClauseIQ's own
                                                # memory/audit/legal-queue state
```

## Tests

```bash
pytest tests -v
```

`tests/test_end_to_end_trace.py` drives the real agent tree (real tools,
real `Runner`, real long-running-tool pause/resume) through a scripted
fake model (`tests/fakes.py`) that reproduces the PRD's "Simulated
Session" Act 1 (TC1: precedence conflict + Legal loop) and Act 2 (TC3:
not-found -> correction -> cross-customer rule generalization) turn by
turn. Every other test module exercises one tool in isolation with no LLM
involved.

Note: `tests/fakes.py` implements its own minimal `BaseLlm` subclass
rather than using `google-adk`'s internal `MockModel`/`testing_utils` --
those live in the `adk-python` repo's test suite and are not shipped in
the installed `google-adk` package, so they aren't importable from a
downstream project like this one.

## POC scope and known simplifications

- **Document Search** is keyword/tag matching over a small synthetic
  corpus (`clause_agent/data/contracts/corpus.json`), not a production RAG
  pipeline -- explicitly out of scope per the PRD.
- **Memory Bank** defaults to a local JSON file with exact scope-dict
  matching, not real Vertex AI Memory Bank's semantic search. Swappable
  via `clause_agent.tools.memory_bank.set_backend`.
- **SAP connector** is mocked, per the PRD.
- **Confidence threshold** (`CLAUSE_AGENT_CONFIDENCE_THRESHOLD`, default
  `0.90`) is enforced via agent instructions, not a deterministic code
  gate -- a real LLM may not always comply exactly; the hard guardrail is
  `memory_bank_create` refusing an unapproved ruling, which IS
  code-enforced.
- TC4 (stretch: amendment-chain conflict) has corpus data
  (`2019_MSA_Initech.pdf` + two amendments) but no dedicated eval scenario
  yet.
