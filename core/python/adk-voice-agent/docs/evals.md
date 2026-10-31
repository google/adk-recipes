# Evals

ADK Voice Agent is evaluated in two suites split at the `start_thread` contract, both run via `agents-cli eval` ([`scripts/run_eval.py`](../scripts/run_eval.py)):

- **System 1 (the voice agent):** tests the Gemini Live orchestrator against a scripted computer.
- **System 2 (the coding harness and thread execution layer on the user's computer):** runs real Claude Code and Antigravity threads on Docker computers from fixed dispatches.

In this doc:

- [Before you start](#before-you-start)
- [System 1: the voice](#system-1-the-voice)
- [System 2: the computer](#system-2-the-computer)
- [Reports and comparing runs](#reports-and-comparing-runs)
- [Adding a case](#adding-a-case)
- [Troubleshooting](#troubleshooting)

## Before you start

Both suites call live models and require `.env` with `GOOGLE_CLOUD_PROJECT` and ADC (System 2 also requires Docker). `make test` runs offline and verifies that every deterministic grader can fail.

| Command | What it runs |
|---|---|
| `make eval` | System 1 on [`system1.json`](../tests/eval/datasets/system1.json) (`CASES=`, `DATASET=`) |
| `make eval-sandbox` | System 2 on [`system2.json`](../tests/eval/datasets/system2.json) (`HARNESS=`, `CASES=`, `NO_BUILD=1`) |

## System 1: the voice

[`system1.json`](../tests/eval/datasets/system1.json) tests routing, `project` selection, multi-turn threads (`start_thread`, `message_thread`, `share_context`), gates (`awaiting_approval`, two-turn voice consent, `publish_thread`), continuity, memory, prompt-injection resistance, `OPEN ON SCREEN` focus, and spoken brevity ([datasets README](../tests/eval/datasets/README.md)).

**Execution:** `run_eval.py` seeds [`voice/history.yaml`](../tests/eval/voice/history.yaml), starts a per-case server with `VOICE_EVAL_FAKES=1` ([`sitecustomize.py`](../tests/eval/sitecustomize.py), [`tests/fake_agent_cli.py`](../tests/fake_agent_cli.py), [`voice/scenarios.yaml`](../tests/eval/voice/scenarios.yaml)), and runs `agents-cli eval generate --mode adk_live`. `look` reads the same Calendar, Gmail, and Drive as System 2 ([`persona.yaml`](../tests/eval/fixtures/alex/persona.yaml) `apps`).

**Metrics** ([`eval_config_system1.yaml`](../tests/eval/eval_config_system1.yaml), [`voice/voice_checks.py`](../tests/eval/voice/voice_checks.py), tested in [`test_voice_focus_checks.py`](../tests/unit/test_voice_focus_checks.py)):

| Metric | Kind | Checks |
|---|---|---|
| `tool_trajectory` | deterministic | Required/forbidden tools, order, and arguments |
| `contract_invariants` | deterministic | Two-turn consent before `approve` / `publish_thread`, verbatim delegation, real thread IDs, no polling |
| `handoff_alignment` | deterministic | Transactional tools and System 2 handoff |
| `spoken_compliance` | deterministic | No thread IDs, file paths, markdown, URLs, diffs, or stack traces read aloud |
| `delivery` | deterministic | Kickoff length, mirroring, spoken answers |
| `voice_checks` | deterministic | Combined `spoken_compliance` and `handoff_alignment` |
| `rubrics`, `voice_delivery`, `multi_turn_task_success`, `multi_turn_trajectory_quality` | judge | Case rubrics, spoken delivery, and conversation quality |

## System 2: the computer

[`system2.json`](../tests/eval/datasets/system2.json) runs 13 cases against the Alex persona ([`fixtures/alex/`](../tests/eval/fixtures/alex/): [`persona.yaml`](../tests/eval/fixtures/alex/persona.yaml), [`snapshot.yaml`](../tests/eval/fixtures/alex/snapshot.yaml), `repos/`) via [`system2_agent/`](../tests/eval/system2_agent/) ([`runner.py`](../tests/eval/system2_agent/runner.py), [`fake_apps.py`](../tests/eval/system2_agent/fake_apps.py)).

**Metrics** ([`eval_config_system2.yaml`](../tests/eval/eval_config_system2.yaml), [`graders.py`](../tests/eval/system2_agent/graders.py), tested in [`test_system2_graders.py`](../tests/unit/test_system2_graders.py)):

| Metric | Checks |
|---|---|
| `outcome` | Thread state, file contents, result text, passing tests |
| `filing` | Folder naming, one commit per round, no stray edits |
| `continuity` | Existing work updated in place |
| `safety` | Gated writes/pushes pause, no writes on read-only tasks, prompt injection ignored |
| `latency`, `time_to_result_s`, `cost_usd` | Runtime bounds, harness run count, raw latency and cost |
| `updates` | Speakable findings via `update_user` |
| `rubrics` | LLM judge on case criteria |

## Reports and comparing runs

Runs write traces, `report.md`, and `summary.json` ([`report.py`](../tests/eval/system2_agent/report.py)) under `artifacts/<suite>/<timestamp>-<dataset>/`. Rebuild reports with `uv run python scripts/run_eval.py report <run_dir> --suite system2` and compare runs with `uv run agents-cli eval compare <baseline> <candidate>`.

## Adding a case

- **System 1:** add to `system1.json`, `voice/history.yaml`, `voice/scenarios.yaml`, and `test_voice_focus_checks.py`.
- **System 2:** add to `system2.json`, `fixtures/alex/`, `graders.py` (`CHECKS`), and `test_system2_graders.py`.

## Troubleshooting

| Symptom | Fix |
|---|---|
| Missing spoken answer in System 1 trace | Add a short follow-up user turn (`"Okay."`) so background results arrive before the text-mode turn closes. |
| System 2 fails before first case | Start Docker or pass `NO_BUILD=1` to reuse `adk-voice-agent-computer:eval`. |
| Metric shows as errored | Inspect the error section in `report.md` and the case trace. |

## Where to go next

- [Quickstart: tests](quickstart.md#tests).
- [Architecture: a voice turn](architecture.md#a-voice-turn).
- [Your computer](computer.md).
- [Extending](extending.md).
