---
name: approvals
description: What to do when a tool call is paused for the user's approval or declined (sending, inviting, deleting, posting, merging), and when you are resumed after one.
---

# Approvals

Gated MCP calls that reach other people or delete data trigger `request_permission` on the `voice_agent` gateway, move the thread to `awaiting_approval`, and return `"Paused for the user's approval of: …"`. Prepare everything first so the gated call comes last, then call the tool directly.

## When a call is paused

1. Stop immediately. Do not retry or work around the gate via another tool, CLI, `curl`, or API.
2. End your turn with one sentence stating what is waiting for approval.

## When resumed

- **Approved:** call the exact same tool with the exact same arguments once. Changed arguments require a new approval.
- **Declined:** do not perform the action; continue without it or explain what it blocks.

## Guidelines

- Prefer a draft (Gmail draft, Markdown file) unless the user explicitly asked to send, invite, post, or merge.
- Shell commands denied by policy are final—do not bypass them; report what was blocked.
