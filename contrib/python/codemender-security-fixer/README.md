# codemender-security-fixer

**Work down an AppSec finding backlog with a CodeMender-style agent.**

Security scanners are good at producing findings and bad at closing them.
AppSec and platform teams end up with a backlog of SQL injections, unsafe
deserialization and shell injections that every service team has to triage
and fix by hand. This recipe points an ADK agent at that backlog and has it
**propose fixes that a deterministic validator must accept** before anyone
sees them, in the spirit of
[CodeMender](https://deepmind.google/blog/introducing-codemender-an-ai-agent-for-code-security/).

The agent never grades its own work. It runs a **detect → patch → validate**
loop:

1. **Detect** – `list_findings` runs [Bandit](https://bandit.readthedocs.io/)
   and the project's own tests on a throwaway working copy. Failing exploit
   tests are findings too, so issues a static analyzer cannot see (such as
   path traversal) are covered.
2. **Patch** – Gemini reads the vulnerable code and the exploit test that
   reproduces the attack, and writes a minimal, complete replacement for one
   file.
3. **Validate** – `propose_patch` copies the working copy, applies the patch
   and re-runs Bandit and every test in a resource-limited subprocess. The
   patch is kept only if it **resolves at least one finding, introduces no
   new Bandit finding and breaks no passing test**. Edits to tests and
   `# nosec` suppressions are refused. Rejections come back with the exact
   reason, within a bounded retry budget.

The output is a **unified diff plus evidence** for human review: which
findings were fixed, which exploit tests now pass, test results before and
after, and every attempt with its verdict. Nothing is committed or pushed.

```mermaid
flowchart LR
    U[AppSec engineer] --> A[Gemini agent]
    A -- list_findings / read_file --> W[(Working copy<br/>temp dir)]
    A -- propose_patch(file, content) --> V[Validator<br/>Bandit + tests<br/>in scratch copy]
    T[(Target repo<br/>read-only)] -- copied once --> W
    V -- accepted: apply --> W
    V -- rejected: reasons --> A
    A -- get_patch_report --> R[Diff + evidence<br/>for human review]
```

## Real-world scenario

The bundled target ([`app/sample_target/`](app/sample_target/)) is a small
internal **orders API** (stdlib `http.server` + SQLite) of the kind every
company has: list a customer's orders, download invoices, archive a month of
invoices for finance, restore a shopping cart from a cookie, protect admin
endpoints with a bearer token. It ships five realistic vulnerabilities and
the tests an AppSec reviewer would write for them:

| Vulnerability | Where | Bandit | Exploit test |
| --- | --- | --- | --- |
| SQL injection (f-string query) | `orders/db.py` | B608 | 3 tests |
| Path traversal in invoice download | `orders/invoices.py` | — | 1 test, 4 payloads |
| Command injection (`shell=True`) | `orders/invoices.py` | B602 | 1 test |
| Unsafe deserialization (pickle cookie) | `orders/cart.py` | B301, B403 | 1 test |
| Hardcoded admin token | `orders/auth.py` | B105 | 2 tests |

Before the agent runs: **5 Bandit findings, 9/17 tests passing** (all 9
functional tests pass, all 8 exploit tests fail). What the AppSec team gets
back is a patch that closes every finding with evidence attached, ready for a
normal code review.

> [!WARNING]
> `app/sample_target/` is intentionally vulnerable. Never deploy it or copy
> its patterns. Its lines carry `# noqa` markers only so the repository's own
> linter accepts the sample; Bandit still reports them.

### Example run

One live run against the bundled target with `MODEL_NAME=gemini-3.7-flash`
and the default budget of 8 patch attempts:

| Metric | Result |
| --- | --- |
| Bandit findings | 5 → **0** (B105, B301, B403, B602, B608) |
| Tests passing | 9/17 → **17/17** (all 8 exploit tests fixed, no regressions) |
| Files patched | 4 (`auth.py`, `cart.py`, `db.py`, `invoices.py`) |
| Patch attempts | 4 of 8, all accepted by the validator |
| Tool calls | 14 (1 scan, 8 file reads, 4 patches, 1 report) |
| Wall-clock time | 133.5 s, including every Bandit and test run |
| Tokens | 178.9k prompt, 4.3k output |

The run fixed path traversal and shell injection in one `invoices.py` patch
(real-path containment check, `YYYY-MM` validation, argv list without a
shell), parameterized both SQL filters, replaced the pickle cookie with JSON
and moved the admin token to `ORDERS_API_TOKEN` with a constant-time compare
that fails closed. The report also flagged the follow-up a human must do:
provision `ORDERS_API_TOKEN` in every environment. Review still matters: the
month check used `re.match(r"^\d{4}-\d{2}$", ...)`, which also accepts a
trailing newline; a reviewer would ask for `re.fullmatch`. Numbers vary
between runs.

### Use your own repository

Point `TARGET_DIR` at a Python project. It is copied to a temporary working
directory (without `.git`, virtualenvs or `.env`) and never modified:

```bash
TARGET_DIR=/path/to/payments-service \
TEST_COMMAND="python -m pytest -rA -q" \
uv run adk run app
```

- With no `TEST_COMMAND`, stdlib `unittest` tests under `tests/` are
  discovered and reported per test. For pytest, pass `-rA` so per-test
  results are visible; otherwise only the exit code is used.
- The tests run with this recipe's interpreter, so your project's test
  dependencies must be installed there (for example `uv add --dev pytest`).
- Add exploit tests for the findings you care about. They are what turns
  "Bandit is quiet" into "the attack no longer works".
- Files under `tests/`, `test_*.py`, `*_test.py` and `conftest.py` are
  evidence: the agent can read them but cannot change them.

## What you will learn

- Gating every model-written change with a **deterministic validator** that
  compares scanner and test results before and after, so the model cannot
  talk its way past a failing test.
- Keeping a working copy, the baseline, accepted patches and an **attempt
  budget in ADK session state** with `ToolContext`, reproducible on any
  server instance.
- Running untrusted project code with **process isolation**, timeouts,
  resource limits and an environment stripped of credentials.

## Requirements

Before you begin, ensure you have:
- **uv**: Python package manager - [Install](https://docs.astral.sh/uv/getting-started/installation/)
- Access to Gemini through Vertex AI or a Google AI Studio API key.
- `tar` on the `PATH` (used by the sample target's archive endpoint).

## Setup

> **Note**: All commands below must be run from the recipe root directory (`contrib/python/codemender-security-fixer/`).

1. Install required packages (this includes Bandit):
   ```bash
   uv sync
   ```

2. Set up environment variables. Copy `.env.example` to `.env` and configure
   either Vertex AI (`GOOGLE_CLOUD_PROJECT`, `GOOGLE_GENAI_USE_VERTEXAI=True`)
   or `GEMINI_API_KEY`:
   ```bash
   cp .env.example .env
   ```

   | Variable | Default | Purpose |
   | --- | --- | --- |
   | `MODEL_NAME` | `gemini-3.7-flash` | Model that writes the patches |
   | `TARGET_DIR` | bundled sample | Repository to fix (copied, never modified) |
   | `TEST_COMMAND` | `unittest` discovery | Test command (no shell) |
   | `BANDIT_SKIPS` | `B101,B404,B603,B607` | Bandit checks that flag usage, not vulnerabilities |
   | `MAX_PATCH_ATTEMPTS` | `8` | Validator calls allowed per session |
   | `VALIDATION_TIMEOUT_SECONDS` | `120` | Wall-clock and CPU limit per scan or test run |

## Run

Start the agent in the terminal and ask it to fix the backlog:

```bash
uv run adk run app
# > Fix the security findings in the orders service and give me a reviewable patch.
```

Or start the local web server and open the ADK dev UI:

```bash
uv run uvicorn app.fast_api_app:app --reload
```

The final report includes the diff, and `get_patch_report` also writes it to
a `codemender.patch` file next to the working copy. Apply it to a branch of
your repository with `git apply codemender.patch` and open a normal code
review. Working copies live under the system temp directory
(`codemender-work-*`) and are left in place so you can inspect them; delete
them when you are done.

## Deploy and use it where your team works

The recipe ships a `Dockerfile` and serves the standard ADK FastAPI app, so
it runs on Cloud Run. Bandit is installed in the image:

```bash
gcloud run deploy codemender-security-fixer --source . --region us-central1 \
  --set-env-vars MODEL_NAME=gemini-3.7-flash,GOOGLE_GENAI_USE_VERTEXAI=True
```

To use it on your own code in Cloud Run, bake the repository into the image
(or mount it) and set `TARGET_DIR`. Keep the service private: it executes the
target's tests.

To bring it to your team, make the agent available in your Gemini Enterprise
app (the recipe includes an Agent Engine adapter in `app/app_utils/`) and
expose it in Google Chat by following
[Build a Google Chat app with a Gemini Enterprise AI agent](https://developers.google.com/workspace/chat/quickstart-ge-agent).
Engineers can then ask for a validated patch from a Chat space and take the
diff into code review.

## Safety

- **Process isolation, not a sandbox.** Bandit and the target's tests run in
  a separate interpreter with a minimal environment (no API keys or cloud
  credentials), a timeout, and CPU, memory and file-size limits, inside a
  disposable copy. The tests are still arbitrary code with network and file
  system access. Run the recipe in its container and add a stronger sandbox
  (for example gVisor) before pointing it at code you do not trust.
- **The target is never modified.** All edits happen in a temporary working
  copy; validation happens in a second, scratch copy.
- **The agent never commits or pushes.** It has no git or network tools. Its
  only output is a diff and a report.
- **Human review is required.** Passing Bandit and the tests is necessary,
  not sufficient: tests can miss attack variants and Bandit has false
  negatives. Review every patch like any other change before merging.

## Running Tests

The unit tests cover the scanner and test harness, the validator (accepting
a correct fix, rejecting a broken one and an incomplete one, prechecks), the
attempt budget, `TARGET_DIR` handling and report generation. They run Bandit
and the sample's tests for real and do not call a model:

```bash
uv run pytest
```

Integration tests call Gemini and need credentials:

```bash
uv run pytest tests/integration
```

## Commands

| Command | Description |
| ------- | ----------- |
| `uv run adk run app` | Run the agent in interactive CLI mode |
| `uv run uvicorn app.fast_api_app:app --reload` | Start the local FastAPI development server |
| `uv run pytest` | Run the unit and runnability tests |
