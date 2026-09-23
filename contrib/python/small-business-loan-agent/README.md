# Small Business Loan Agent

A multi-agent system built with the [Google Agent Development Kit (ADK)](https://google.github.io/adk-docs/) that automates small business loan processing. It demonstrates sequential multi-agent orchestration, human-in-the-loop approval, LLM-as-Judge validation, and Firestore-backed repair & resume.

## A. Overview & Functionalities

### Agent Details

| Property             | Value                                       |
| -------------------- | ------------------------------------------- |
| **Interaction Type** | Workflow                                    |
| **Complexity**       | Advanced                                    |
| **Agent Type**       | Multi-Agent (1 orchestrator + 4 sub-agents) |
| **Vertical**         | Financial Services                          |
| **Framework**        | ADK                                         |
| **Model**            | Gemini 3.8 Flash                            |

### Key Features

| Feature                            | Description                                                                                             |
| ---------------------------------- | ------------------------------------------------------------------------------------------------------- |
| **Multi-Agent Orchestration**      | Orchestrator coordinates 4 specialized sub-agents via `AgentTool` in a sequential workflow              |
| **Multimodal Document Extraction** | Gemini 3.8 Flash reads loan application PDFs natively                                                   |
| **Structured Output**              | Each sub-agent returns validated Pydantic models via `output_schema` / `output_key`                     |
| **Human-in-the-Loop (HITL)**       | Orchestrator pauses after pricing to present results and wait for explicit user approval                |
| **LLM-as-Judge Gate**              | After-agent callback validates trajectory correctness and data grounding before showing responses       |
| **Repair & Resume**                | Firestore workflow management tracks each step; workflow can pause on errors and resume from checkpoint |
| **Before/After Callbacks**         | State checks before each sub-agent; state logging and issue detection after each sub-agent              |
| **Before-Tool Callback**           | Process halt check prevents agents from executing when workflow is in error/pending state               |

### Example Interaction

**Complete loan application example** (using `data/sample_applications/sample_application_complete.pdf`):

If testing from Agent Runtime Playground UI ask:

```
Process sample_application_complete.pdf from GCS for loan SBL-2025-00142
```

The complete loan application example interaction is as follows (if you are running locally, you can upload your file through the UI instead of using GCS). The rest of the flow applies to both local and Agent Runtime Playground.

```
User: Process this loan application for SBL-2025-00142
      [uploads sample_application_complete.pdf]

Agent: [Calls check_process_status -> initializes new process]
       [Calls DocumentExtractionAgent -> extracts data from PDF]
       [Calls UnderwritingAgent -> validates against internal records, checks eligibility]
       [Calls PricingAgent -> calculates rate based on risk tier]

       Loan Application Summary:
       - Business: Coffee Roasters LLC
       - Owner: Jane Doe
       - Loan Amount: $150,000
       - Annual Revenue: $850,000
       - Eligibility: ELIGIBLE
       - Risk Tier: Tier 1 - Low Risk
       - Interest Rate: 6.50%
       - Monthly Payment: $2,940.97
       - Total Interest: $26,458.20

       Do you approve this loan? (yes/no)

User: yes

Agent: [Calls LoanDecisionAgent -> finalizes decision]

       Loan SBL-2025-00142 has been approved.
       Decision letter DL-2025-02142-001 has been generated.
```

**Pause, Repair & Resume example** (using `data/sample_applications/sample_application_incomplete.pdf` which has missing fields):

A. submit the incomplete application

```
User: Process this application for SBL-2025-00391
      [uploads sample_application_incomplete.pdf]

Agent: [check_process_status -> initializes new process]
       [DocumentExtractionAgent -> extracts data, detects missing fields]
       [UnderwritingAgent -> before_tool_callback halts execution]

       I encountered an error while processing your application.
       Cannot proceed to UnderwritingAgent: Pending approval
       - Missing 1 critical field(s): loan_amount_requested

       Please provide a complete document or update the application
       with the required field before we can proceed.
       The workflow has been stopped.

       Reference: SBL-2025-00391
```

B. Repair the data in Firestore:

1. Open the [Firestore Console](https://console.cloud.google.com/firestore) and select the `small-business-loan-states` database
2. Navigate to **`process_states`** collection → document **`SBL-2025-00391`**
3. Under `steps.DocumentExtractionAgent.data`, fill in the missing field:

- Set `loan_amount_requested` to the correct value (e.g., `150000`)

4. Update `steps.DocumentExtractionAgent.status` from `pending_approval` → `completed`
5. Update the root `overall_status` from `pending_approval` → `active`

C. Resume the workflow

```
User: Resume processing for SBL-2025-00391

Agent: [check_process_status -> detects DocumentExtractionAgent completed]
       [Skips DocumentExtractionAgent, resumes from UnderwritingAgent]
       [UnderwritingAgent -> validates against internal records, checks eligibility]
       [PricingAgent -> calculates rate based on risk tier]
       ...continues normal workflow...
```

## B. Architecture Visuals

![Agent Architecture](agent_pattern.webp)

**Complete loan application**

```mermaid
sequenceDiagram
    actor User
    participant Orch as SmallBusinessLoan<br/>OrchestratorAgent
    participant FS as Firestore<br/>process_states
    participant DE as DocumentExtraction<br/>Agent
    participant UW as Underwriting<br/>Agent
    participant PR as Pricing<br/>Agent
    participant LD as LoanDecision<br/>Agent
    participant Judge as LLM-as-Judge<br/>Gate

    User->>+Orch: Message + PDF upload
    Note over Orch: before_agent_callback<br/>extract_request_id

    Orch->>+FS: check_process_status
    FS-->>-Orch: New process initialized

    Note over Orch: before_tool_callback<br/>checks process state<br/>before each agent call

    Orch->>+DE: Extract loan application data
    Note over DE: Gemini reads PDF natively
    DE-->>FS: state_logging (step data)
    DE-->>-Orch: LoanApplicationData (Pydantic)

    Orch->>+UW: Validate & check eligibility
    UW->>UW: get_internal_business_data
    UW->>UW: Check eligibility_rules.json
    UW-->>FS: state_logging (step data)
    UW-->>-Orch: UnderwritingReport (Pydantic)

    Orch->>+PR: Calculate pricing
    PR->>PR: calculate_loan_pricing
    PR-->>FS: state_logging (step data)
    PR-->>-Orch: PricingResult (Pydantic)

    Orch-->>User: Present summary & ask approval
    Note over User,Orch: HUMAN-IN-THE-LOOP GATE

    alt User approves
        User->>Orch: "yes"
        Orch->>+LD: Finalize decision
        LD->>LD: finalize_loan_decision
        LD-->>FS: state_logging (step data)
        LD-->>-Orch: LoanDecisionResult (Pydantic)
    else User rejects
        User->>Orch: "no"
        Orch-->>User: Application will not proceed
    end

    Orch->>+Judge: after_agent_callback
    Note over Judge: Validates trajectory,<br/>grounding, completeness
    alt Valid
        Judge-->>-Orch: Pass
        Orch-->>-User: Final response
    else Blocked
        Judge-->>Orch: Block response
        Orch-->>User: Verification needed
    end
```

**Pause, Repair & Resume:**

```mermaid
sequenceDiagram
    actor User
    actor Operator as Human Operator<br/>(Firestore Console)
    participant Orch as SmallBusinessLoan<br/>OrchestratorAgent
    participant FS as Firestore<br/>process_states
    participant DE as DocumentExtraction<br/>Agent
    participant UW as Underwriting<br/>Agent

    Note over User,UW: PHASE 1 — Pause on missing data

    User->>+Orch: Incomplete PDF + SBL-2025-00391
    Orch->>FS: check_process_status
    FS-->>Orch: New process initialized

    Orch->>+DE: Extract loan application data
    DE-->>FS: state_logging (data with missing fields)<br/>status: pending_approval
    Note over DE,FS: Missing: loan_amount_requested
    DE-->>-Orch: LoanApplicationData (incomplete)

    Orch->>+UW: Validate & check eligibility
    Note over Orch,UW: before_tool_callback<br/>reads Firestore status
    UW->>FS: Check overall_status
    FS-->>UW: pending_approval
    UW-->>-Orch: HALT — Cannot proceed
    Orch-->>-User: Workflow stopped<br/>Missing field: loan_amount_requested

    Note over User,UW: PHASE 2 — Offline repair in Firestore

    Operator->>FS: Fix loan_amount_requested = 150000
    Operator->>FS: Set DocumentExtractionAgent status → completed
    Operator->>FS: Set overall_status → active

    Note over User,UW: PHASE 3 — Resume from checkpoint

    User->>+Orch: Resume processing for SBL-2025-00391
    Orch->>+FS: check_process_status
    FS-->>-Orch: Resume from UnderwritingAgent<br/>Load completed step data into session

    Note over Orch: Skips DocumentExtractionAgent<br/>(already completed)

    Orch->>+UW: Validate & check eligibility
    UW-->>FS: state_logging (step data)
    UW-->>-Orch: UnderwritingReport (Pydantic)
    Note over Orch: Continues normal workflow...
```

**State Management (Firestore):**

```
Process State (per loan_request_id)
  |-- overall_status: active | pending_approval | completed | failed
  |-- steps:
  |     |-- DocumentExtractionAgent: { status, data, completed_at }
  |     |-- UnderwritingAgent:       { status, data, completed_at }
  |     |-- PricingAgent:            { status, data, completed_at }
  |     |-- LoanDecisionAgent:       { status, data, completed_at }
  |-- issues: [ { step, description, resolved } ]
```

## C. Setup & Execution

### Prerequisites

- Python 3.11+
- uv
  - For dependency management and packaging. Please follow the
    instructions on the official
    [uv website](https://docs.astral.sh/uv/) for installation.

  ```bash
  curl -LsSf https://astral.sh/uv/install.sh | sh
  ```

- Google Cloud project with Vertex AI API and Firestore enabled
- `gcloud` CLI authenticated

### Google Cloud Setup

You can use the provided `Makefile` to automate the Google Cloud setup.

```bash
# Login
gcloud auth application-default login
export PROJECT_ID=your-project-id
gcloud config set project $PROJECT_ID
```

Run steps below:

- `make enable-services`: Enable required Vertex AI, Firestore, and Storage APIs.
- `make create-firestore`: Create the Firestore database.
- `make setup-gcs`: Create GCS bucket and upload sample files (uses `BUCKET_NAME` environment variable or defaults to `<project-id>-small-business-loan-data`).
- `make grant-permissions`: Grant necessary IAM roles to the service account.

### Installation

```bash
# Clone the repository
git clone <repo-url>
cd contrib/python/small-business-loan-agent

# Install dependencies
uv sync

# Configure environment
cp .env.example .env
# Edit .env with your GCP project details
```

### Environment Variables

These mirror [`.env.example`](.env.example) — copy that file to `.env` and replace the placeholders.

```bash
# Required
GOOGLE_GENAI_USE_VERTEXAI=TRUE
GOOGLE_CLOUD_PROJECT=your-gcp-project-id
GOOGLE_CLOUD_LOCATION=global
MODEL_NAME=gemini-3.8-flash

# Optional
GCS_DATA_BUCKET=your-bucket-name   # defaults to <project>-small-business-loan-data
JUDGE_MODEL=gemini-3.8-flash       # defaults to MODEL_NAME
```

### Sample Documents

We have provided two sample PDFs in `data/sample_applications/`:

- `sample_application_complete.pdf` -- Happy path (all fields present, strong financials)
- `sample_application_incomplete.pdf` -- Same application with missing fields (triggers repair & resume)

Both represent the same fictional business (Coffee Roasters LLC / Jane Doe). The incomplete version is missing the loan amount requested to demonstrate the pause, repair & resume flow.

If you want to generate them yourself, use:

```bash
uv run python data/generate_sample_applications.py
```

### Running the Agent

```bash
# Run with ADK web UI
uv run adk web
```

Then open `http://localhost:8000`, select `small_business_loan_agent`, upload a sample PDF, and send:

```
Process this loan application for SBL-2025-00142
```

OR (if file is in GCS)

```
Process sample_application_complete.pdf from GCS for loan SBL-2025-00142
```

## D. Customization & Extension

### Modifying the Flow

- **Prompts:** Each sub-agent has a `prompt.py` in its directory. Modify these to change agent behavior.
- **Orchestrator flow:** Edit `prompt.py` to change the step sequence, add/remove agents, or alter the HITL approval point.
- **Eligibility rules:** Edit `sub_agents/underwriting/eligibility_rules.json` to add or modify business lending criteria.

### Adding Sub-Agents

1. Create a new directory under `sub_agents/` with `agent.py`, `models.py`, `prompt.py`, and optionally `tools.py`
2. Add the agent to the orchestrator's tools list in `agent.py` as an `AgentTool`
3. Update `state_service.py` ALL_STEPS list and `state_callbacks.py` AGENT_OUTPUT_KEY_MAP
4. Update the orchestrator prompt to include the new step

### Connecting Real Data Sources

The mock tools in each sub-agent's `tools.py` are designed to be replaced:

- **`get_internal_business_data`** (underwriting) -- Replace `MOCK_INTERNAL_RECORDS` with calls to your bank's internal API, database, or CRM
- **`calculate_loan_pricing`** (pricing) -- Replace `_determine_risk_tier` with calls to your pricing engine or rate sheet API
- **`finalize_loan_decision`** (loan_decision) -- Replace with calls to your loan origination system

### Changing the Document Type

The `DocumentExtractionAgent` uses Gemini's native multimodal capabilities to read PDFs. To process a different document type:

1. Update `sub_agents/document_extraction/models.py` with new Pydantic fields
2. Update `sub_agents/document_extraction/prompt.py` to describe the new document structure
3. Generate new sample documents in `data/`

### Adding Document AI for Production Extraction

For high-volume production workloads requiring precise, consistent extraction with confidence scores and bounding boxes, you can integrate [Google Document AI](https://cloud.google.com/document-ai) alongside or instead of Gemini's native PDF reading:

1. Create a Document AI custom extractor processor configured for your document type
2. Add a `before_agent_callback` or tool to the `DocumentExtractionAgent` that calls the Document AI API to extract entities from the uploaded PDF
3. Pass the extracted entities to the agent prompt for structured mapping into the Pydantic model
4. Add `google-cloud-documentai` to your dependencies

## E. Tests

For running tests and evaluation, install the extra dependencies:

```bash
uv sync --dev
```

Then run tests from the `small-business-loan-agent` directory:

```bash
# Unit tests (no GCP required)
uv run pytest tests/unit

# Integration tests (requires GCP credentials and takes 1 to 2 min to run)
uv run pytest tests/integration
```

## F. Evaluation

Ensuring the reliability and accuracy of the Small Business Loan Agent is critical before deploying it in a live lending environment. We use the [ADK evaluation framework](https://google.github.io/adk-docs/evaluate/) with an LLM-as-Judge approach to validate both the intermediate steps (sub-agent orchestration) and the final outcome (response quality).

### Evaluation Methodology

Our evaluation treats the multi-agent system as a complete pipeline, measuring its performance against a curated dataset of loan application scenarios. We evaluate both the tool call trajectory (did the orchestrator call the right agents in the right order?) and the final response (is the output complete, clear, and semantically correct?).

**The process involves:**

1. **Dataset Ingestion**: Feeding test cases — complete applications, incomplete applications, and resume-after-repair scenarios — into the agent as multi-turn conversations.
2. **Execution Tracing**: Logging the orchestrator's routing decisions, sub-agent calls, and tool invocations for each turn.
3. **LLM-as-Judge Assertion**: Using Gemini as a judge model to evaluate tool ordering against rubrics and compare final responses against expected outputs.

### Test Cases

| Test Case                            | Description                                                                                                                            | Turns |
| ------------------------------------ | -------------------------------------------------------------------------------------------------------------------------------------- | ----- |
| `happy_path_with_approval`           | Full end-to-end flow: submit complete PDF, process through all 4 sub-agents, user approves, loan decision finalized                    | 2     |
| `stop_for_reparation_missing_fields` | Incomplete PDF (missing `loan_amount_requested`): agent stops after DocumentExtraction, reports missing fields, halts workflow         | 1     |
| `resume_after_repair`                | Pre-repaired state in Firestore: agent detects completed DocumentExtraction, resumes from UnderwritingAgent, processes through Pricing | 1     |

### Evaluation Criteria

| Criterion                                | Purpose                                                         | Threshold | Reference Required |
| ---------------------------------------- | --------------------------------------------------------------- | --------- | ------------------ |
| `rubric_based_tool_use_quality_v1`       | Validates tool call ordering using LLM judge against rubrics    | 0.8       | No                 |
| `rubric_based_final_response_quality_v1` | Evaluates response completeness and clarity using LLM judge     | 0.8       | No                 |
| `final_response_match_v2`                | Semantic equivalence of response to expected output (LLM-based) | 0.7       | Yes                |

### Key Metrics

- **Routing Accuracy (Tool Use Rubrics)**: Did the orchestrator call sub-agents in the correct order? The following ordering rules are enforced:

  | Rubric                           | Rule                                                                           |
  | -------------------------------- | ------------------------------------------------------------------------------ |
  | `status_first`                   | `check_process_status` is called before any agent tools on the initial request |
  | `extraction_before_underwriting` | `DocumentExtractionAgent` is called before `UnderwritingAgent`                 |
  | `underwriting_before_pricing`    | `UnderwritingAgent` is called before `PricingAgent`                            |
  | `pricing_before_decision`        | `PricingAgent` is called before `LoanDecisionAgent`                            |
  | `approval_required`              | `LoanDecisionAgent` is only called after user approval                         |

- **Response Quality (Final Response Rubrics)**: Is the agent's output complete and actionable?

  | Rubric                      | Rule                                                                                                                     |
  | --------------------------- | ------------------------------------------------------------------------------------------------------------------------ |
  | `loan_summary_completeness` | Response includes business name, owner, loan amount, revenue, eligibility, risk tier, rate, and payment                  |
  | `clear_next_step`           | Response clearly indicates next action: approval prompt, completion confirmation, status report, or missing info request |
  | `error_handling_clarity`    | When data is missing or an error occurs, the response clearly identifies what is missing or wrong                        |

- **Semantic Response Match**: Does the agent's final response convey the same information as the expected reference response? Threshold set to 0.7 to account for natural LLM wording variation.

### Design Decisions

1. **`rubric_based_tool_use_quality_v1` over `tool_trajectory_avg_score`**: When using `AgentTool` wrappers, the orchestrator LLM dynamically generates `request` args containing context from previous steps. Since these args are LLM-generated and unpredictable, we use rubric-based LLM judging to validate tool ordering semantically rather than exact argument matching.

2. **`final_response_match_v2` for semantic matching**: More flexible than exact string matching. The LLM judge evaluates whether the actual response is semantically equivalent to the expected reference, accommodating natural variation in phrasing while catching meaningful omissions.

3. **`rubric_based_final_response_quality_v1` for reference-free evaluation**: Evaluates quality using custom rubrics without requiring a reference response. This catches issues like missing summary fields or unclear next steps even when the overall meaning is correct.

### Building Your Own Eval Set

To implement evaluation for this agent:

1. **Use the sample documents as test inputs.** The synthetic PDFs in `data/sample_applications/` are designed for evaluation:
   - `sample_application_complete.pdf` — all fields present, use for happy-path and approval test cases
   - `sample_application_incomplete.pdf` — missing `loan_amount_requested`, use for repair-flow test cases

2. **Pass documents as `inline_data` in user content.** Gemini reads PDFs natively — base64-encode the file and include it alongside the text prompt in the eval case's `user_content.parts` array.

3. **Use randomly generated SBL IDs** (e.g., `SBL-2025-XXXXX`) for each eval run to avoid Firestore state collisions between runs.

4. **For resume-after-repair test cases**, pre-populate Firestore with a repaired process state (DocumentExtractionAgent marked `completed` with filled-in data, `overall_status` set to `active`) before running the eval. The agent will then skip DocumentExtraction and resume from UnderwritingAgent.

5. **Define expected tool sequences in `intermediate_data.tool_uses`.** For a complete application, the expected sequence is:

   ```
   check_process_status → DocumentExtractionAgent → UnderwritingAgent → PricingAgent
   ```

   For a second turn with user approval:

   ```
   LoanDecisionAgent
   ```

6. **Set the judge model** in your eval config (e.g., `gemini-3.1-pro-preview`) for rubric-based criteria. Both tool use and response quality rubrics use this judge.

See [ADK Evaluation docs](https://google.github.io/adk-docs/evaluate/) and [Evaluation Criteria](https://google.github.io/adk-docs/evaluate/criteria/) for the full evalset schema and available criteria.

## G. Deploy

Use the [Google Agents CLI](https://github.com/google/agents-cli) to create a production-ready version of this agent with deployment options.

**Install the CLI** (one-time):

```bash
uvx google-agents-cli setup
```

**Create the project from this recipe** (run from the root of the `adk-recipes` repository, replace `my-loan-agent` with your project name):

```bash
agents-cli create my-loan-agent -a local@contrib/python/small-business-loan-agent --auto-approve -o target
```

The Google Agents CLI will prompt you to select deployment options and provides additional production-ready features including automated CI/CD deployment scripts. (for more options, run the above command without `--auto-approve -o target` flags)

When deploying to Agent Runtime, ensure your `.env` file is configured with required variables. You can then install, test, and complete the full setup and deployment using the Makefile provided by the starter pack:

```bash
cd target/my-loan-agent && make install && make test && make backend
```

Note: File upload is not supported in the Agent Runtime Playground currently, so use the GCS to fetch the test files (cf example interaction above)

To register the agent to Gemini Enterprise, do this:

```bash
uvx google-agents-cli publish gemini-enterprise
```

## License

Copyright 2026 Google LLC. Licensed under the Apache License, Version 2.0.
