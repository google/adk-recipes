# NYC CRE Assistant

NYC CRE Assistant is an ADK recipe for New York City commercial real estate
property intelligence. It interprets user-provided building addresses, resolves
them to Borough Block Lot identifiers through NYC Geoclient, finds public
ownership evidence, and summarizes recorded mortgage or debt document metadata.

The recipe demonstrates a narrow expert-agent pattern: one root agent routes
requests to three file-based ADK skills, while deterministic Python tools do
the public-record lookup work. The model handles routing, high-confidence
address interpretation, and summaries; the tools return auditable data from NYC
Geoclient, NYC Open Data, ACRIS metadata, PLUTO, and NYS Department of State
entity records where available.

## Overview

This agent helps users answer three focused property questions:

- What is the BBL for this NYC building address?
- What public-record evidence identifies the property owner?
- What recorded mortgage, lender, borrower, or debt evidence exists?

The recipe is intentionally conservative about evidence. It does not infer
hidden beneficial owners, current loan balance, payoff status, or actual
maturity dates when public metadata does not prove those facts.

## BBL Background

A Borough Block Lot identifier, usually called a BBL, is New York City's
parcel identifier for real property. It combines the borough code, tax block,
and tax lot into a single 10-digit value. Public datasets such as PLUTO and
ACRIS use BBL components to associate buildings, tax lots, deeds, mortgages,
and other recorded documents with the same property.

The BBL is needed because street addresses are not stable enough for reliable
public-record lookup. A building may have alternate addresses, vanity names,
unit suffixes, abbreviations, or spelling variations. The agent may interpret a
user's address text when the address is clear, but ambiguous addresses should be
clarified. The recipe uses NYC Geoclient to resolve structured address fields to
a BBL, then uses that BBL as the stable key for owner and recorded debt
evidence.

## Agent Details

| Feature | Description |
| --- | --- |
| **Interaction Type** | Conversational lookup |
| **Complexity** | Intermediate |
| **Agent Type** | Single Agent |
| **Components** | ADK Skills, Function Tools, NYC public data APIs |
| **Vertical** | Commercial Real Estate |

## Component Details

**Agent**

- `nyc_cre_assistant`: root agent that selects exactly one route for each
  user request.

**Skills**

- `bbl-address`: interprets one clear NYC address and resolves it to a BBL.
- `find-owner`: summarizes public owner evidence for one BBL.
- `find-debt`: summarizes recorded mortgage and debt evidence for one BBL.

**Tools**

- `get_bbl_from_normalized_address`: calls NYC Geoclient with structured
  address fields.
- `find_owner_by_bbl`: combines PLUTO, ACRIS, and NYS DOS evidence.
- `find_debt_by_bbl`: retrieves ACRIS mortgage and debt document metadata.

## Setup and Installation

### Prerequisites

- Python 3.11+
- [uv](https://docs.astral.sh/uv/getting-started/installation/)
- Google API key for the ADK model
- NYC Geoclient V2 primary key for address-to-BBL lookup

### Installation

From the recipe root:

```bash
cd contrib/python/nyc-cre-assistant
uv sync
```

### Configuration

Copy the environment template:

```bash
cp .env.example .env
```

For local use, only two keys are required:

```bash
GOOGLE_API_KEY=<your-google-api-key>
GEOCLIENT_V2_PK=<your-nyc-geoclient-primary-key>
```

Get `GOOGLE_API_KEY` from [Google AI Studio](https://aistudio.google.com/apikey).

Get `GEOCLIENT_V2_PK` from the public, free
[NYC API Developer Portal](https://api-portal.nyc.gov/):

1. Create an account or sign in.
2. Open **Products**.
3. Subscribe to **Geoclient V2 User**.
4. Open your profile and copy the **Primary key** for that subscription.

The recipe sends this value as the `Ocp-Apim-Subscription-Key` header when it
calls NYC Geoclient.

## Running the Agent

### Web Interface Recommended

```bash
uv run adk web
```

Open the printed local URL and select `app` from the agent menu.

### Command Line Interface

```bash
uv run adk run app
```

Use CLI mode for quick smoke tests while changing tools or instructions.

## Example Interactions

```text
What is the BBL for 200 Park Avenue, Manhattan?
```

```text
Who owns BBL 1013000001?
```

```text
Find recorded mortgage or debt evidence for 200 Park Avenue.
```

## Project Structure

```text
nyc-cre-assistant/
|-- app/
|   |-- agent.py              # Root agent, tools, and SkillToolset wiring
|   |-- skills/               # Route-specific ADK skill instructions
|   `-- tools/                # Deterministic public-record lookup tools
|-- tests/
|   |-- test_runnability.py   # Import and ADK entry point smoke tests
|   `-- unit/                 # Offline deterministic tool tests
|-- .env.example
|-- manifest.yaml
|-- pyproject.toml
`-- uv.lock
```

## Running Tests

```bash
uv run pytest
```

The test suite is offline. It validates tool contracts and importability without
requiring live API credentials.

## Customization

- Add more public-record sources by extending the tool modules in `app/tools/`.
- Adjust routing or evidence rules by editing the corresponding `SKILL.md`.
- Keep source limitations explicit in both tool outputs and skill
  instructions so the agent does not overstate what public metadata proves.

## Disclaimer

This recipe is for demonstration purposes. Public-record metadata can be
incomplete, stale, or ambiguous. Review source records directly before making
business, legal, credit, or investment decisions.
