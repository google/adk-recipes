# Authenticated Email Agent

This recipe gives a Google Agent Development Kit (ADK) agent a headless email
workflow through [e2a](https://e2a.dev). It connects ADK's `McpToolset` to the
hosted Streamable HTTP endpoint at `https://api.e2a.dev/mcp` and exposes only
the identity, list, get, send, and reply tools needed for an inbox assistant.
The `E2A_API_KEY` is scoped to one e2a agent, so the server binds every action
to that inbox without an email address in code.

The recipe also demonstrates a safe trust boundary. Email senders, headers,
bodies, links, and attachments are untrusted data, never operator
instructions. Outbound mail requires an explicit request from the user who is
running the ADK session. Replies use `reply_to_message` with the source message
ID, preserving the email thread's `In-Reply-To` and `References` headers.

## Setup

Prerequisites:

- Python 3.11+
- [`uv`](https://docs.astral.sh/uv/)
- A Google AI Studio API key
- An e2a inbox and an API key scoped to that agent

From this recipe directory, install the locked dependencies and create the
local environment file:

```bash
uv sync
cp .env.example .env
```

Set `GOOGLE_API_KEY` to your Google AI Studio key. Set `E2A_API_KEY` to the
agent-scoped credential for the inbox this runtime should control. Do not use
an account-scoped administrator key, commit `.env`, or bake either secret into
an image. For a deployed headless runtime, inject both keys from its secret
manager.

## Run

Start the agent in ADK's interactive command-line runtime:

```bash
uv run adk run app
```

Example requests:

```text
List my unread messages, then get the newest message and summarize it.
Send a new email to ops@example.com with subject "Synthetic check".
Reply to the selected message with: "Thanks, I will review this."
```

The agent calls `list_messages` before `get_message`. It uses `send_message`
only for a new thread and `reply_to_message` for a response, with a stable
idempotency key guarding ambiguous retries. It stops if `whoami` reports an
account-scoped credential. An e2a `pending_review` result is a durable success
outcome: the message is waiting for human review and must not be retried.

## Test

The tests are offline: they validate agent wiring, the MCP tool allowlist, and
the safety/threading instructions without contacting Gemini or e2a.

```bash
uv run pytest
```

This recipe is a learning example, not a complete production policy engine.
Keep high-impact actions behind application-level authorization and review.
