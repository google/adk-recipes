# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Verify and describe candidate issue clusters against sampled conversation traces.

One LLM call per candidate cluster, after clustering and merging, that inspects up to
three member trajectories to answer two questions: is this cluster a real defect, and
what is the 30-70 word symptom explanation a triager needs.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import re
from concurrent import futures
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping, Sequence

    from ambient_quality_agent.tools.agent_revision import (
        AgentRevision,
        ToolDefinition,
    )
    from ambient_quality_agent.tools.insights.clustering import Cluster
    from ambient_quality_agent.tools.insights.models import RubricExample

from ambient_quality_agent.tools.agent_revision import UNRECORDED_PARAMETERS
from ambient_quality_agent.tools.insights.models import ClusterVerification

logger = logging.getLogger(__name__)

VERIFY_CONCURRENCY = 4
"""Concurrent verification calls, matching CHUNK_CONCURRENCY in clustering."""

MAX_VERIFIED_CLUSTERS = 50
"""Largest clusters verified per sweep, by item_count. Nothing caps cluster
count upstream -- validate_clusters checks labels and id ranges only -- so the
arithmetic ceiling is one cluster per finding."""

MAX_EVIDENCE_TRACES = 3
"""Maximum conversation trajectories shown per cluster."""

MAX_TRACE_CHARS = 250_000
"""Character ceiling on one rendered trajectory JSON string."""

MAX_EVIDENCE_CHARS = 500_000
"""Character ceiling on all trajectories in one prompt. Two traces at the
per-trace ceiling already exceed what the per-trace limit alone would allow, so
the block needs its own bound."""

ANALYSIS_STEP = "verification"
"""Key this pass's verdict is stored under in `InsightOccurrence.analyses`."""

FIELD_ORDER = (
    "valid",
    "proposed_label",
    "description",
)


class VerifyInfo(BaseModel):
    """Outcome tally across candidate clusters evaluated in the verification pass."""

    verified: int = 0
    """Number of clusters confirmed to be real defects and described."""

    rejected: int = 0
    """Number of clusters rejected by the model as unsupported defects."""

    skipped: int = 0
    """Number of clusters skipped without model evaluation (e.g. no traces or beyond cap)."""

    failed: int = 0
    """Number of clusters where the model call failed open."""


_VERIFY_INSTRUCTIONS = r"""### OVERVIEW

You are validating one candidate issue cluster found in an evaluation of a
production AI agent. A set of failed quality checks was grouped together and
given a short label (an "insight") describing a single kind of problematic
agent behavior.

The cluster and its label were produced from the quality checks alone, by
someone who never saw the agent's system prompt or the actual trajectories.
Your job is to tell whether the cluster represents a real issue, and whether
its label is correct.

### INPUTS

You will base your answer on the following inputs:

- issue_label: a given (potentially inaccurate) insight - short description
of problematic agent behavior

- agent_definition: the analysed agent's configuration: its `system_prompt` and
its `tool_declarations`, whose `parameters` is a list of parameter names alone,
with no types and no per-parameter descriptions. Present only when every shown
trace ran under the same configuration.

- evidence: up to three traces that are examples of the issue, each entry naming
the conversation it came from in `eval_case_id` and pairing one `trajectory`
with the `quality_checks` that failed on it. When the shown traces ran under
different configurations there is no top-level `agent_definition` and each entry
carries its own `system_prompt` and `tool_declarations` instead.

A `trajectory` is a list of turns, each `{turn_index, events}`, whose events are
`{author, content}` with the content's `parts` holding `text`, a `function_call`
or a `function_response`. The worked examples below abbreviate it to flat turns
of `role`, `content` and `tool_calls`, so their shape is not the one you will
receive.

A missing `agent_definition`, `system_prompt` or `tool_declarations` means that
configuration was not recorded. It does not mean the agent had no such
instruction or no such tool. Never take that silence as evidence: a claim that
the agent lacked an instruction, or called a tool it was not given, is
unsupported when you were shown no configuration to check it against.

### YOUR TASK

Evaluate the cluster in this order:

1) Validate each failed quality check. Treat quality_checks as evaluator claims,
not ground truth. Verify that the claimed behavior is actually present in its
trajectory.

2) Validate the cluster. Determine whether the supported failures across the
evidence represent one coherent recurring behavior. If they represent materially
different issues, set `valid` to false.

3) Determine why the shared behavior is problematic. Ground this only in the
user's request, system_prompt, tool_declarations, tool results, and observed
assistant behavior.

4) Normalize the insight. Decide whether `issue_label` accurately frames the
behavior you found. Settle on the narrowest label and description that the
evidence actually supports.

### OUTPUT

Return three fields.

`valid` -- boolean, false when the evidence does not support the claim, or when
the cluster does not represent one coherent issue. Otherwise true.

`proposed_label` -- a specific evidence-supported replacement label when the
current label is incorrect or too vague, naming the observed behavior and not
your verdict on it. Return "" when no rename is needed. Do not make the label
more abstract in order to cover heterogeneous evidence.

`description` -- 30-70 words describing the recurring observed behavior and the
requirement or configuration conflict that makes it problematic. This
description should make the whole insight actionable to a human reviewer. It
should focus on the observed symptoms, and say what the evidence does not
support when you answered `valid` false. Do not propose any solutions.

### CONSTRAINTS

These apply to every field above.

- Treat quality checks as evaluator claims, not ground truth. Verify each
  claim against its associated trajectory before using it as evidence.
- A valid cluster must represent one coherent behavior across its supporting
  evidence. Do not broaden or abstract the label merely to make unrelated
  evidence fit under one issue.
- Ground every clause in the agent's configuration or in what the agent
  produced, so the reader can check each one against one of the two.
- Report the symptom: the call that was made, the argument that was sent, the
  text that was produced. Why the agent did it is not visible to you.
- Treat a configuration that contradicts itself as a real issue. Name both
  sides and the artefact each lives in, say what the call sent, and if it
  affected the user intent.
- Use "failed quality checks" whenever you refer to them
- Use backticks for tool names



### EXAMPLES

## EXAMPLE 1

{
  "issue_label": "submitted the expense report without attaching the receipt",
  "agent_definition": {
    "system_prompt": "You file and submit expenses on the user's behalf. An expense of more than 25 USD cannot be submitted until its receipt is attached.",
    "tool_declarations": [
      {
        "name": "create_expense",
        "description": "Create a draft expense."
      },
      {
        "name": "attach_receipt",
        "description": "Attach a receipt to a draft."
      },
      {
        "name": "submit_expense",
        "description": "Submit a draft for approval."
      }
    ]
  },
  "evidence": [
    {
      "trajectory": [
        {
          "role": "user",
          "content": "file the 62 dollar dinner at Rossi's from last night"
        },
        {
          "role": "assistant",
          "tool_calls": [
            {
              "id": "call_1",
              "name": "create_expense",
              "args": {
                "amount": 62,
                "currency": "USD",
                "category": "meals",
                "merchant": "Rossi's"
              }
            }
          ],
          "content": ""
        },
        {
          "role": "tool",
          "id": "call_1",
          "name": "create_expense",
          "error": false,
          "content": "{\"expense_id\": \"EXP-2214\", \"state\": \"draft\"}"
        },
        {
          "role": "assistant",
          "tool_calls": [
            {
              "id": "call_2",
              "name": "submit_expense",
              "args": {
                "expense_id": "EXP-2214"
              }
            }
          ],
          "content": ""
        },
        {
          "role": "tool",
          "id": "call_2",
          "name": "submit_expense",
          "error": false,
          "content": "{\"expense_id\": \"EXP-2214\", \"state\": \"submitted\"}"
        },
        {
          "role": "assistant",
          "content": "Filed and submitted -- EXP-2214 for 62 USD."
        }
      ],
      "quality_checks": [
        {
          "expected": "the agent calls attach_receipt to attach the receipt",
          "actual": "the agent called submit_expense directly on EXP-2214; attach_receipt was never called"
        }
      ]
    }
  ]
}

A good answer:

{
  "valid": true,
  "proposed_label": "submitted the expense report (>25 USD) without attaching the receipt",
  "description": "The system prompt requires `attach_receipt` before submission for anything over 25 USD, and the agent submits without it."
}

## EXAMPLE 2

{
  "issue_label": "omitted the return window from refund confirmations",
  "agent_definition": {
    "system_prompt": "You handle refund requests. Every refund confirmation must state how many days the customer has to ship the item back.",
    "tool_declarations": [
      {
        "name": "start_refund",
        "description": "Open a refund and return its amount and terms."
      }
    ]
  },
  "evidence": [
    {
      "trajectory": [
        {
          "role": "user",
          "content": "I want to send the blender back, it arrived faulty"
        },
        {
          "role": "assistant",
          "tool_calls": [
            {
              "id": "call_1",
              "name": "start_refund",
              "args": {
                "order_id": "55130",
                "reason": "faulty"
              }
            }
          ],
          "content": ""
        },
        {
          "role": "tool",
          "id": "call_1",
          "name": "start_refund",
          "error": false,
          "content": "{\"refund_id\": \"RF-771\", \"amount\": 89.0, \"currency\": \"USD\", \"return_days\": 14}"
        },
        {
          "role": "assistant",
          "content": "Your refund is approved -- send the blender back and the amount lands on your card once it arrives."
        }
      ],
      "quality_checks": [
        {
          "expected": "the reply states how long the customer has to return the item",
          "actual": "start_refund returned return_days 14 and the reply mentions no deadline"
        }
      ]
    },
    {
      "trajectory": [
        {
          "role": "user",
          "content": "the desk lamp I ordered is the wrong colour, I'd like my money back"
        },
        {
          "role": "assistant",
          "tool_calls": [
            {
              "id": "call_1",
              "name": "start_refund",
              "args": {
                "order_id": "55284",
                "reason": "wrong item"
              }
            }
          ],
          "content": ""
        },
        {
          "role": "tool",
          "id": "call_1",
          "name": "start_refund",
          "error": false,
          "content": "{\"refund_id\": \"RF-802\", \"amount\": 42.5, \"currency\": \"USD\", \"return_days\": 30}"
        },
        {
          "role": "assistant",
          "content": "That is approved -- post the lamp back to us and the 42.50 is released once it reaches the warehouse."
        }
      ],
      "quality_checks": [
        {
          "expected": "the reply states how long the customer has to return the item",
          "actual": "start_refund returned return_days 30 and the reply mentions no deadline"
        }
      ]
    },
    {
      "trajectory": [
        {
          "role": "user",
          "content": "these running shoes don't fit, can I return them"
        },
        {
          "role": "assistant",
          "tool_calls": [
            {
              "id": "call_1",
              "name": "start_refund",
              "args": {
                "order_id": "55391",
                "reason": "size"
              }
            }
          ],
          "content": ""
        },
        {
          "role": "tool",
          "id": "call_1",
          "name": "start_refund",
          "error": false,
          "content": "{\"refund_id\": \"RF-815\", \"amount\": 120.0, \"currency\": \"USD\", \"return_days\": 7}"
        },
        {
          "role": "assistant",
          "content": "Refund started for the shoes -- send them back and the 120.00 goes to your original payment method."
        }
      ],
      "quality_checks": [
        {
          "expected": "the reply states how long the customer has to return the item",
          "actual": "start_refund returned return_days 7 and the reply mentions no deadline"
        }
      ]
    }
  ]
}

A good answer:

{
  "valid": true,
  "proposed_label": "",
  "description": "The system prompt requires every refund confirmation to state the return window, and the observed replies omit it. In all cases the agent had this information in the tool call, but did not use it."
}

## EXAMPLE 3

{
  "issue_label": "called schedule_visit with a missing address",
  "agent_definition": {
    "system_prompt": "You book engineer visits. Confirm the slot is free with check_availability before calling schedule_visit.",
    "tool_declarations": [
      {
        "name": "check_availability",
        "description": "List free slots."
      },
      {
        "name": "schedule_visit",
        "parameters": {
          "properties": {
            "address": {
              "type": "string"
            },
            "slot": {
              "type": "string"
            }
          }
        }
      }
    ]
  },
  "evidence": [
    {
      "trajectory": [
        {
          "role": "user",
          "content": "can an engineer come to 14 Elm Row on Thursday afternoon?"
        },
        {
          "role": "assistant",
          "tool_calls": [
            {
              "id": "call_1",
              "name": "check_availability",
              "args": {
                "slot": "2026-03-05T14:00:00Z"
              }
            }
          ],
          "content": ""
        },
        {
          "role": "tool",
          "id": "call_1",
          "name": "check_availability",
          "error": false,
          "content": "{\"free\": true}"
        },
        {
          "role": "assistant",
          "tool_calls": [
            {
              "id": "call_2",
              "name": "schedule_visit",
              "args": {
                "address": "14 Elm Row",
                "slot": "2026-03-05T14:00:00Z"
              }
            }
          ],
          "content": ""
        },
        {
          "role": "tool",
          "id": "call_2",
          "name": "schedule_visit",
          "error": false,
          "content": "{\"visit_id\": \"V-6620\", \"state\": \"scheduled\"}"
        },
        {
          "role": "assistant",
          "content": "Booked -- an engineer will be at 14 Elm Row on Thursday afternoon."
        }
      ],
      "quality_checks": [
        {
          "expected": "visit is scheduled for 14 Elm Row, Thursday afternoon",
          "actual": "schedule_visit was called with no address parameter"
        }
      ]
    }
  ]
}

A good answer:

{
  "valid": false,
  "proposed_label": "called `schedule_visit` with the address the user gave",
  "description": "The agent confirmed the Thursday slot with `check_availability`, then called `schedule_visit` with address \"14 Elm Row\" and that slot, and reported the booking. The failed quality check states that `schedule_visit` was called with no address parameter, which the trajectory contradicts, so the evidence shows no departure from the system prompt."
}

## EXAMPLE 4

{
  "issue_label": "called book_shift with an invalid start timestamp",
  "agent_definition": {
    "system_prompt": "Book shifts in the worker's local time, for example book_shift(worker_id=\"W-88\", start=\"2026-03-04 09:00 PST\").",
    "tool_declarations": [
      {
        "name": "book_shift",
        "parameters": {
          "properties": {
            "worker_id": {
              "type": "string"
            },
            "start": {
              "type": "string",
              "description": "ISO-8601 timestamp in UTC, e.g. 2026-03-04T17:00:00Z"
            }
          }
        }
      }
    ]
  },
  "evidence": [
    {
      "trajectory": [
        {
          "role": "user",
          "content": "put Dana on the Wednesday morning shift, 9am her time"
        },
        {
          "role": "assistant",
          "tool_calls": [
            {
              "id": "call_1",
              "name": "book_shift",
              "args": {
                "worker_id": "W-88",
                "start": "2026-03-04 09:00 PST"
              }
            }
          ],
          "content": ""
        },
        {
          "role": "tool",
          "id": "call_1",
          "name": "book_shift",
          "error": false,
          "content": "{\"worker_id\": \"W-88\", \"booked\": \"2026-03-04T17:00:00Z\"}"
        },
        {
          "role": "assistant",
          "content": "Dana is booked for Wednesday at 9am."
        }
      ],
      "quality_checks": [
        {
          "expected": "start is sent as an ISO-8601 UTC timestamp",
          "actual": "the agent passed a local wall-clock string with a timezone abbreviation, which is not a valid start value"
        }
      ]
    }
  ]
}

A good answer:

{
  "valid": true,
  "proposed_label": "system prompt and `book_shift` declaration disagree on the start timestamp format",
  "description": "The system prompt demonstrates `book_shift` with a local wall-clock time and a timezone abbreviation, while the tool declaration defines start as an ISO-8601 UTC timestamp. When a shift is booked the agent sends the wall-clock form, which the tool accepts."
}

## EXAMPLE 5

{
  "issue_label": "handled the order status request poorly",
  "agent_definition": {
    "system_prompt": "Answer order questions from get_order_status. Open every reply with the customer's first name.",
    "tool_declarations": [
      {
        "name": "get_order_status",
        "description": "Look up an order by id."
      }
    ]
  },
  "evidence": [
    {
      "trajectory": [
        {
          "role": "user",
          "content": "where is order 90412?"
        },
        {
          "role": "assistant",
          "tool_calls": [
            {
              "id": "call_1",
              "name": "get_order_status",
              "args": {
                "order_id": "90412"
              }
            }
          ],
          "content": ""
        },
        {
          "role": "tool",
          "id": "call_1",
          "name": "get_order_status",
          "error": false,
          "content": "{\"state\": \"in transit\", \"carrier\": \"DHL\", \"tracking\": \"JD0142\", \"eta\": \"2026-03-06\", \"customer_first_name\": \"Priya\"}"
        },
        {
          "role": "assistant",
          "content": "Priya -- order 90412 is in transit with DHL and should arrive on 6 March."
        }
      ],
      "quality_checks": [
        {
          "expected": "the reply quotes the tracking number",
          "actual": "the tracking number JD0142 was returned by get_order_status and left out of the reply"
        }
      ]
    },
    {
      "trajectory": [
        {
          "role": "user",
          "content": "any update on 90755?"
        },
        {
          "role": "assistant",
          "tool_calls": [
            {
              "id": "call_1",
              "name": "get_order_status",
              "args": {
                "order_id": "90755"
              }
            }
          ],
          "content": ""
        },
        {
          "role": "tool",
          "id": "call_1",
          "name": "get_order_status",
          "error": false,
          "content": "{\"state\": \"out for delivery\", \"carrier\": \"UPS\", \"tracking\": \"JD0198\", \"eta\": \"2026-03-05\", \"customer_first_name\": \"Marcus\"}"
        },
        {
          "role": "assistant",
          "content": "Hi there -- order 90755 is out for delivery with UPS on tracking JD0198 and should arrive on 5 March."
        }
      ],
      "quality_checks": [
        {
          "expected": "the reply opens with the customer's first name",
          "actual": "get_order_status returned customer_first_name Marcus and the reply opens with \"Hi there\""
        }
      ]
    }
  ]
}

A good answer:

{
  "valid": false,
  "proposed_label": "",
  "description": "One reply drops the tracking number JD0142 that `get_order_status` returned. A second reply opens with \"Hi there\" although the same tool returned the customer first name Marcus and the system prompt requires opening with it. The failed quality checks describe two distinct defects rather than one recurring behavior, so no single label names them."
}

## EXAMPLE 6

{
  "issue_label": "failed to call list_currencies",
  "agent_definition": {
    "system_prompt": "Categorise every request and follow the assigned workflow. Category B (Pricing / Single Currency Question): e.g. \"what is this in euros\", \"do you bill in yen\", \"how do I change my plan\". Workflow: MANDATORY concurrent call to list_currencies and docs_search. You MUST NOT skip tools for any currency question.",
    "tool_declarations": [
      {
        "name": "list_currencies",
        "description": "List the billing currencies an account may be charged in."
      },
      {
        "name": "docs_search",
        "description": "Search the product documentation."
      }
    ]
  },
  "evidence": [
    {
      "trajectory": [
        {
          "role": "user",
          "content": "how do I add a second admin to my workspace?"
        },
        {
          "role": "assistant",
          "tool_calls": [
            {
              "id": "call_1",
              "name": "docs_search",
              "args": {
                "q": "add an admin to a workspace"
              }
            }
          ],
          "content": ""
        },
        {
          "role": "tool",
          "id": "call_1",
          "name": "docs_search",
          "error": false,
          "content": "{\"doc\": \"Admins > Add a member, then assign the Admin role.\"}"
        },
        {
          "role": "assistant",
          "content": "Open Admins, add the member and assign them the Admin role."
        }
      ],
      "quality_checks": [
        {
          "expected": "the agent calls list_currencies, as Category B mandates",
          "actual": "docs_search was called and list_currencies was not"
        }
      ]
    }
  ]
}

A good answer:

{
  "valid": true,
  "proposed_label": "currency workflow routes workspace admin requests",
  "description": "The system prompt mandates `list_currencies` for Category B and the agent answers from `docs_search` without it, but these requests do not need it: users ask how to administer a workspace -- adding admins, changing a plan -- which a list of billing currencies could not answer. Category B is named and closed on currency questions while its examples reach plan changes."
}

### THE INSIGHT TO DIAGNOSE
{inputs}
"""


def _build_verify_response_schema() -> Any:
    from google.genai import types

    return types.Schema(
        type=types.Type.OBJECT,
        properties={
            "valid": types.Schema(type=types.Type.BOOLEAN),
            "proposed_label": types.Schema(type=types.Type.STRING),
            "description": types.Schema(type=types.Type.STRING),
        },
        property_ordering=list(FIELD_ORDER),
        required=list(FIELD_ORDER),
    )


def call_verification_model(prompt: str) -> str:
    """Execute Gemini request for cluster verification using HIGH thinking budget.

    Args:
        prompt: Formatted verification prompt.

    Returns:
        Generated model response text.
    """
    from ambient_quality_agent.config import config
    from ambient_quality_agent.tools import genai_json
    from google.genai import types

    return (
        genai_json.call_gemini(
            prompt,
            _build_verify_response_schema(),
            thinking_level=types.ThinkingLevel.HIGH,
            model=config.verification_model,
        ).text
        or ""
    )


def _compute_label_key(label: str) -> str:
    """A label reduced to its alphanumeric words, lowercased and single-spaced.

    Args:
        label: Raw label text.

    Returns:
        Normalized key string.
    """
    return " ".join(re.findall(r"[a-z0-9]+", label.lower()))


_NON_PROPOSALS = frozenset(
    _compute_label_key(text)
    for text in (
        "none",
        "n/a",
        "na",
        "null",
        "same",
        "same as above",
        "unchanged",
        "no change",
        "no change needed",
        "keep",
        "keep the current label",
        "no proposal",
        "",
    )
)


def clean_proposed_label(proposed: str, current_label: str) -> str:
    """A proposed rename, or '' when nothing was really proposed.

    Args:
        proposed: Candidate replacement label from the model.
        current_label: Current label of the cluster.

    Returns:
        Cleaned replacement label, or empty string if no change was proposed.
    """
    cleaned = " ".join(str(proposed or "").split()).strip(" .")
    key = _compute_label_key(cleaned)
    if (
        not key
        or key in _NON_PROPOSALS
        or key == _compute_label_key(current_label)
    ):
        return ""
    return cleaned


_TRUNCATION_MARKER = "... (content truncated)"


def _clip_turns(
    turns: Sequence[dict[str, Any]], max_chars: int
) -> tuple[list[dict[str, Any]], bool]:
    """The turns, with the longest `content` clipped until they fit `max_chars`.

    The pass quotes argument values and field names back, so a clip is
    spent on `content` alone and never on a turn, a tool call or an argument:
    truncating the serialized JSON instead would hand the model a half-written
    object. A trace whose structure alone exceeds the ceiling is returned over
    it rather than not at all.

    Args:
        turns: Turn dictionaries representing a conversation trace.
        max_chars: Character limit for the serialized trace.

    Returns:
        Tuple of (clipped_turns, was_truncated).
    """
    clipped = [dict(turn) for turn in turns]

    def compute_size() -> int:
        return len(json.dumps(clipped, ensure_ascii=False, default=str))

    truncated = False
    while clipped and compute_size() > max_chars:
        over = compute_size() - max_chars
        biggest = max(clipped, key=lambda t: len(str(t.get("content") or "")))
        content = str(biggest.get("content") or "")
        # Swapping content this short for the marker cannot shrink the payload,
        # so the loop would otherwise spin.
        if len(content) <= len(_TRUNCATION_MARKER):
            break
        keep = max(0, len(content) - over - len(_TRUNCATION_MARKER))
        biggest["content"] = content[:keep].rstrip() + _TRUNCATION_MARKER
        truncated = True
    return clipped, truncated


def _build_evidence(examples: Sequence[RubricExample]) -> list[dict[str, Any]]:
    """Format up to MAX_EVIDENCE_TRACES sorted evidence entries, grouping rubrics per case.

    Args:
        examples: Rubric examples associated with the cluster.

    Returns:
        List of formatted evidence dictionaries for prompt injection.
    """
    by_case: dict[str, list[RubricExample]] = {}
    for ex in sorted(examples, key=lambda e: e.eval_case_id):
        if not ex.eval_case_id or not ex.trace:
            continue
        by_case.setdefault(ex.eval_case_id, []).append(ex)

    evidence: list[dict[str, Any]] = []
    spent = 0
    for case_id in sorted(by_case.keys())[:MAX_EVIDENCE_TRACES]:
        case_examples = by_case[case_id]
        first_ex = case_examples[0]
        # The block ceiling is what remains of it, so three traces cannot each
        # spend the per-trace ceiling.
        budget = min(MAX_TRACE_CHARS, max(0, MAX_EVIDENCE_CHARS - spent))
        trace_payload, _ = _clip_turns(first_ex.trace, budget)
        spent += len(json.dumps(trace_payload, ensure_ascii=False, default=str))
        quality_checks = []
        for ex in case_examples:
            pair = {
                "expected": ex.rubric.expected_behavior,
                "actual": ex.rubric.actual_behavior,
            }
            if pair not in quality_checks:
                quality_checks.append(pair)
        evidence.append(
            {
                "eval_case_id": case_id,
                "trajectory": trace_payload,
                "quality_checks": quality_checks,
            }
        )
    return evidence


def _build_agent_definition(
    revisions: Sequence[AgentRevision],
) -> dict[str, Any]:
    """Render one session's configuration as the specification it ran under.

    Empty when the session recorded none, so the model reasons from the
    trajectory alone instead of against a configuration it never saw.

    Args:
        revisions: Agent revisions for the evaluated session.

    Returns:
        Dictionary defining system_prompt and tool_declarations.
    """
    definition: dict[str, Any] = {}
    # Multi-agent instructions in one session are scoped rather than in conflict.
    # Attribute each block by agent to preserve those role boundaries rather than
    # presenting a single contradictory specification.
    blocks = [
        (rev.agent_id, rev.instruction.strip())
        for rev in revisions
        if rev.instruction
    ]
    if len(blocks) == 1:
        definition["system_prompt"] = blocks[0][1]
    elif blocks:
        definition["system_prompt"] = "\n\n".join(
            f"Agent {agent_id}:\n{instruction}"
            for agent_id, instruction in blocks
        )

    tools_by_name: dict[str, ToolDefinition] = {}
    for rev in revisions:
        for tool in rev.tools:
            if tool.name:
                tools_by_name.setdefault(tool.name, tool)
    if tools_by_name:
        definition["tool_declarations"] = [
            _build_tool_declaration(t) for t in tools_by_name.values()
        ]
    return definition


def _build_tool_declaration(tool: ToolDefinition) -> dict[str, Any]:
    """Build the declaration of one tool for ``tool_declarations``.

    Args:
        tool: Tool definition from an agent revision.

    Returns:
        Dictionary with the tool's name, description, and parameter names, or
        `UNRECORDED_PARAMETERS` when telemetry recorded no parameter schema so
        the model does not read the tool as taking no arguments.
    """
    return {
        "name": tool.name,
        "description": tool.description,
        "parameters": (
            tool.parameters
            if tool.parameters is not None
            else UNRECORDED_PARAMETERS
        ),
    }


def _build_prompt(
    cluster: Cluster,
    agent_definitions: Mapping[str, Sequence[AgentRevision]] | None = None,
) -> str:
    """Construct the verification prompt for a single cluster.

    Each shown trace is described by the configuration its own session ran
    under. One shared configuration is hoisted into ``agent_definition``;
    differing ones stay inside the evidence entry each governs, because joining
    them reads as a specification contradicting itself.

    A cluster no session of which recorded a configuration is shown no tools at
    all. That is the honest state -- nothing here knows what that agent could
    call -- and it is why no sweep-wide list stands in: a list the traces never
    ran under is what turns a real call into a hallucinated one.

    Args:
        cluster: Candidate cluster to verify.
        agent_definitions: Optional map of session ID to its agent revisions.

    Returns:
        Complete verification prompt string.
    """
    evidence = _build_evidence(cluster.examples)
    definitions = [
        _build_agent_definition(
            (agent_definitions or {}).get(entry["eval_case_id"], [])
        )
        for entry in evidence
    ]
    if all(one == definitions[0] for one in definitions[1:]):
        definition = dict(definitions[0]) if definitions else {}
    else:
        # A toolset varies per session as freely as an instruction, and a trace
        # judged against another session's declarations makes a real tool call
        # read as a hallucinated one.
        definition = {}
        for entry, one in zip(evidence, definitions, strict=True):
            entry.update(one)

    payload: dict[str, Any] = {"issue_label": cluster.label}
    if definition:
        payload["agent_definition"] = definition
    payload["evidence"] = evidence
    rendered = json.dumps(payload, ensure_ascii=False, indent=2)
    if "{inputs}" in _VERIFY_INSTRUCTIONS:
        return _VERIFY_INSTRUCTIONS.replace("{inputs}", rendered)
    return _VERIFY_INSTRUCTIONS.rstrip("\n") + "\n" + rendered + "\n"


def _parse_verify_response(response: str, label: str) -> tuple[bool, str, str]:
    """Parse raw JSON model response into a (category, debug, proposed_label, description) tuple.

    Raises on anything unreadable, so an unparseable response and a failed call
    are one kind of loss: both fail the cluster open, and both count toward the
    all-calls-failed outage that `verify_clusters` raises on.
    """
    try:
        decoded = json.loads(response or "")
    except (json.JSONDecodeError, TypeError) as exc:
        raise ValueError(f"verify response was not valid JSON: {exc}") from exc

    if not isinstance(decoded, dict):
        raise ValueError(
            f"verify response was not a dict: {type(decoded).__name__!r}"
        )

    if "valid" not in decoded:
        raise ValueError("verify response has no 'valid'")

    valid = bool(decoded.get("valid"))
    proposed_label = clean_proposed_label(
        decoded.get("proposed_label", ""), label
    )
    description = str(decoded.get("description") or "").strip()
    return valid, proposed_label, description


class ClusterVerificationOutcome(BaseModel):
    """One cluster's verdict, and which of its examples the model actually read.

    Returned rather than written onto the `Cluster`: the caller decides what to
    persist, so this module stays a pure function of its inputs and can be tested
    without a store.
    """

    verification: ClusterVerification
    examined_case_ids: list[str] = Field(default_factory=list)

    @property
    def rejected(self) -> bool:
        """True when the model judged the cluster no defect at all."""
        return not self.verification.valid


def verify_clusters(
    clusters: Sequence[Cluster],
    model_call: Callable[[str], str] | None = None,
    agent_definitions: Mapping[str, Sequence[AgentRevision]] | None = None,
) -> tuple[dict[int, ClusterVerificationOutcome], VerifyInfo]:
    """Judge the largest clusters and report what the model concluded.

    Keyed by position in ``clusters`` so a caller can pair a verdict with the
    candidate it came from; a cluster the pass skipped or failed on is simply
    absent. Absence is the lack of a verdict rather than a negative one, and
    what an unjudged candidate is then worth is the caller's decision --
    `insight_correlation` withholds its insight rather than presenting an
    unchecked finding as a verified one.

    ``agent_definitions`` maps a session id to the configurations that session
    ran under, as `FindingSet.agent_revisions` carries them.

    Raises the first exception when every attempted call failed, matching
    `clustering.cluster_and_label` and the session reviewer: everything failing is
    an outage, not a sweep with no verdicts.

    Args:
        clusters: Candidate clusters to evaluate.
        model_call: Optional prompt-to-text callable; `call_verification_model`
            when omitted.
        agent_definitions: Mapping of session ID to agent revisions.

    Returns:
        Tuple of (outcomes_dict, verify_info_tally).

    Raises:
        Exception: The first failure encountered if all evaluated calls fail.
    """
    if not clusters:
        return {}, VerifyInfo()

    call = model_call or call_verification_model
    outcomes: dict[int, ClusterVerificationOutcome] = {}
    tally = VerifyInfo()

    # Cap the pass to the largest clusters by item_count. Nothing bounds cluster
    # count upstream, and this is the most expensive call in the pipeline.
    sorted_indices = sorted(
        range(len(clusters)),
        key=lambda idx: clusters[idx].item_count,
        reverse=True,
    )
    eligible = set(sorted_indices[:MAX_VERIFIED_CLUSTERS])

    to_evaluate: list[int] = []
    for idx, cluster in enumerate(clusters):
        if idx not in eligible or not any(ex.trace for ex in cluster.examples):
            # No evidence is not a verdict: a cluster the model could not read
            # is left unjudged rather than rejected, because a judgement this
            # pass never reached has no business in the audit trail. The caller
            # withholds the insight either way; what the stored row has to keep
            # is which of the two happened.
            tally.skipped += 1
            continue
        to_evaluate.append(idx)

    if not to_evaluate:
        return outcomes, tally

    from ambient_quality_agent.config import config

    model_name = config.verification_model.model

    def verify_cluster_at(
        idx: int,
    ) -> tuple[int, tuple[bool, str, str] | Exception]:
        try:
            prompt = _build_prompt(
                clusters[idx], agent_definitions=agent_definitions
            )
            return idx, _parse_verify_response(
                call(prompt), clusters[idx].label
            )
        except Exception as exc:  # Reported per cluster; the pass fails open.
            return idx, exc

    with futures.ThreadPoolExecutor(max_workers=VERIFY_CONCURRENCY) as pool:
        call_results = list(pool.map(verify_cluster_at, to_evaluate))

    failures: list[Exception] = []
    for idx, result in call_results:
        if isinstance(result, Exception):
            failures.append(result)
            tally.failed += 1
            logger.warning(
                "insights: verify call failed for cluster %r; failing open: %s",
                clusters[idx].label,
                result,
            )
            continue

        valid, proposed_label, explanation = result
        if valid:
            tally.verified += 1
        else:
            tally.rejected += 1

        outcomes[idx] = ClusterVerificationOutcome(
            verification=ClusterVerification(
                model=model_name,
                valid=valid,
                explanation=explanation,
                refined_label=proposed_label,
                created_at=dt.datetime.now(dt.UTC),
            ),
            examined_case_ids=_list_examined_case_ids(clusters[idx]),
        )

    if len(failures) == len(to_evaluate):
        raise failures[0]

    return outcomes, tally


def _list_examined_case_ids(cluster: Cluster) -> list[str]:
    """The cases `_build_evidence` put in front of the model, in the same order.

    Kept in step with that function deliberately: a caller marks these examples
    `examined`, and a mark that does not match what was shown is worse than none.

    Args:
        cluster: Evaluated cluster.

    Returns:
        List of session IDs whose traces were examined.
    """
    with_trace = sorted(
        {e.eval_case_id for e in cluster.examples if e.trace and e.eval_case_id}
    )
    return with_trace[:MAX_EVIDENCE_TRACES]
