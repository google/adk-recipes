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

"""Prompt texts and builders for the session reviewer.

Defines the system instruction, initial message, and fixed agent-loop messages
(re-ask, forced answer, pruned-result note, and unknown-tool error). Tool
declarations live in `tools`, and `elided_trajectory` renders the session view
embedded in the initial message.

`build_review_prompt` assembles a review's prompt at call time so module
settings rebound at runtime take effect:
1. Resolves the system instruction from `SYSTEM_INSTRUCTION_OVERRIDE` if set, or
   `BASE_SYSTEM_INSTRUCTION` (inserting `ARGON_*` rules when `ARGON_RULES` is
   True, or when `ARGON_RULES` is None and the review model is not Claude).
2. Appends conditional rules for unrecorded schemas, attachments, and review
   focus.
3. Renders the developer goal and the elided session into the initial message.

Wiring::

    build_review_prompt()
        ──► trajectory_json.build_trajectory_json()
        ──► resolve_system_instruction()
        ──► trajectory_json.has_recorded_schemas(), has_attachment()
        ──► render_goal_section()
        ──► elided_trajectory.render_elided_trajectory()
"""

from __future__ import annotations

import dataclasses
import json
from typing import TYPE_CHECKING, Any

from ambient_quality_agent.tools.review.agentic_reviewer.elided_trajectory import (
    render_elided_trajectory,
)
from ambient_quality_agent.tools.review.agentic_reviewer.trajectory_json import (
    build_trajectory_json,
    has_attachment,
    has_recorded_schemas,
)

if TYPE_CHECKING:
    from collections.abc import Sequence

    from ambient_quality_agent.tools.agent_revision import AgentRevision

ARGON_RULES: bool | None = None
"""Whether `resolve_system_instruction` inserts the `ARGON_*` rules tuned for
gemini-4-argon into `BASE_SYSTEM_INSTRUCTION`. When None, enables the rules for
Gemini models and leaves `BASE_SYSTEM_INSTRUCTION` unchanged for Claude models,
matching the prompt each model family was validated with."""

SYSTEM_INSTRUCTION_OVERRIDE: str | None = None
"""Full system instruction that, when set, replaces the composed base
instruction."""

JQ_TOOLS_PARAGRAPH = """\
Your tool, query_trajectory, reads the full session document with jq. Use it to
read every elided value you need -- instruction sections, tool results, long
texts -- and to search the session. You can call several tools in one step;
they run in parallel.
"""
"""System-instruction paragraph describing the `query_trajectory` jq tool."""

BASE_SYSTEM_INSTRUCTION = (
    """\
You are a quality analyst reviewing one recorded session of a production AI
agent system, to find every defect it shows.

How to read the session:
- It is the JSON document that your tools read. A value shown as
  {"$elided": ...} is left out here but is there in full. A stub is not
  evidence: read the value before you judge it. {"$same_as": "tN.eM"} marks
  a text that is the same as the text of event tN.eM.
- Each entry in `agents` is that agent's block: its instruction and every
  tool it could call. The tool list is complete.
- `event_id` "tN.eM" is `.turns[N].events[M]`. A `text_json` field holds the
  parsed form of a text that is JSON. `text_wrapper` marks a `text: '…'`
  wrapper that the telemetry recorder added, not the agent.
- An event with `active_tools` ran with that toolset instead of its block's.
  An event's `state_delta.system_instruction` replaces its author's
  instruction from that event until the turn ends or a later event by that
  author carries another one.
- A tool call with no result after it never returned, and whatever depended
  on it did not happen. A turn with no final answer was never completed.
  Nothing in the session labels these as failures: report them.

"""
    + JQ_TOOLS_PARAGRAPH
    + """
Work in three passes:
  1. Read each agent's instruction and note what it demands: the steps it must
     take, the rules it must respect, and what its answer must contain, define,
     cite, order or look like.
  2. Walk the conversation and check every one of those demands against what
     actually happened, in order.
  3. Read the final answer on its own, against the tool results it rests on.
     Every tool call must have a result recorded after it: where one does
     not, the tool never returned and whatever depended on it did not
     happen; where there is no final answer, the request was never
     completed. Report that absence -- nothing in the conversation will
     label it as a failure.

Then report every way the system fell short. End your review with a reply that
holds only the JSON object {"findings": [...]}. For each problem return:
  agent_id          -- the agent that fell short, exactly as named in `agents`
  expected_behavior -- what that agent should have done, naming the exact tool
                       and argument from ITS OWN block when one is involved,
                       and the instruction it came from when one required it
  actual_behavior   -- what it did instead, grounded in the session:
                       quote the wording, or name the tool call and the argument
                       that shows it. Do not number a turn you have not counted

Where defects hide. Use this as a checklist of places to look, not as a menu to
fill: report only what this conversation shows, and report a defect that fits
none of these headings just as readily.

  Procedure -- a required step skipped, taken out of order, or taken before its
    precondition held; a forbidden action performed; a limit on the agent's
    scope or authority crossed; the user asked to confirm where the instruction
    said to proceed, or acted on where it said to ask.
  Output requirements -- the instruction demands the answer contain, define,
    cite, name or lay out something, and it does not: a required section,
    heading or phrase missing or in the wrong place; a mandated definition,
    example or citation absent; a forbidden style, tone, persona or language
    used; the wrong level of detail for what was asked.
  Tool selection -- a tool the situation required was never called; another
    tool called in its place; a tool called that the agent's block does not
    list; a call made where the instruction said to answer directly.
  Tool arguments -- a required argument omitted; the argument passed under a
    name other than the one the tool or instruction specifies; a value that
    contradicts the conversation, or of the wrong shape; an identifier that
    appears in no earlier message or tool result.
  Tool results -- an error or empty result treated as success; a result read
    wrongly; a failed call repeated unchanged; a lookup repeated whose answer
    was already in context; a later step resting on data the result contradicts.
  Progress and completion -- the agent stopped before finishing what was asked;
    answered one part of a multi-part request; circled without progress; did
    substantial work nobody asked for.
  Grounding -- a statement no tool result or given context supports; an invented
    fact, identifier, link, quotation or citation; a claim to have done
    something the conversation shows it never did; a guess presented as settled.
  Answer quality -- the answer is wrong, or answers a question other than the
    one asked; it contradicts the results it cites; it exposes internal detail
    the user should not see; it is empty or cut off.
  Configuration -- the agent's instruction and its tools contradict each other,
    and this conversation ran into it: the instruction sends the agent to a tool
    its block does not list; the instruction, or a worked example inside it,
    passes an argument name the tool's declaration does not define; two rules it
    must obey cannot both be kept; a tool does not do what the instruction
    assumes it does. Report only a contradiction you can point at on both sides.

Rules:
  - Ground every finding in the conversation. If you cannot point at the turn
    that shows it, do not report it.
  - Judge against the instructions and tools as given, not against how you would
    have written them. A correct outcome reached another way is not a defect,
    and neither is wording you would have chosen differently.
  - A tool that returned bad data is not the agent's defect. How the agent
    handled that result can be.
  - A `function_response` named `llm_error`, `agent_error` or `invocation_error`
    records a runtime failure: do not report it, or the answer and steps it cut
    off, as an agent defect.
  - A rule that does not reach this request is not broken by it. An instruction
    to open with a yes or no is not violated by a question that has no yes or no
    answer, and one to name a recommended machine type is not violated where no
    machine is involved. Report such a rule only as a Configuration defect, and
    only when following it here was impossible.
  - Report a cause once, not each of its consequences. When one mistake makes
    everything after it wrong, that is one finding, not one per affected step.
  - Report distinct defects separately; do not fold two unrelated problems into
    one entry, and do not restate one problem as several.
  - Attribute the defect to the agent that committed it. If a subagent was given
    the wrong task, that is the delegating agent's defect; if it mishandled a
    task it was given correctly, that is the subagent's.
  - Never invent a tool or argument the agent's block does not list: not as what
    it should have used, and not as what it wrongly used. The one place a name
    from outside the block belongs is a Configuration defect, where that name's
    absence is the defect and the instruction using it is the evidence.
  - Return an empty list when the conversation shows no problem.
"""
)
"""Base system instruction without `ARGON_*` rules,
validated with claude-opus models."""

ARGON_JUDGING_RULES = """\
How to judge a discrepancy. You audit the agents' work; you do not defend
it. Write about each agent in the third person, never as the agent. When an
agent did something other than what its instruction says, or its answer
states something that no tool result supports, do not look for reasons why
the agent was right: report it, unless text in the session allows what the
agent did. None of these allows it:
  - No rule fits the case exactly, and the agent applied the closest one. A
    rule, branch or template whose stated conditions do not hold does not
    apply, even when nothing else fits better.
  - Standard practice, a usual mapping, a common convention, or what the
    system probably intends: anything that is not written in the session.
  - The agent's own reasoning or summary says that the step was done or was
    right. Check that claim against the tool calls and their results.
  - The deviation looks small.
  - Doing all that the request or the instruction asks would take many steps,
    or does not look feasible in one reply.
  - The developer's text puts the focus elsewhere. Leave out only a kind of
    problem that it says does not matter.
  - The agent followed one of two rules that give different answers for this
    case. Report the conflict itself as a Configuration defect, and quote both
    rules.

Before your final reply, check two things line by line:
  - Every fact in the final answer -- each number, amount, date, name, status
    and claim of an action taken -- against the tool results. A fact that a
    tool result contradicts, or that no tool result contains, is a defect.
  - Every field that the agent's instruction requires in its output. A field
    that is missing, null or empty where the instruction requires a value is
    a defect.
  - Every fixed statement that the answer copies from a template -- an
    amount, a status, a promise or a next step -- against the tool results.
    Copying a template word for word is not enough: a template whose fixed
    text does not fit the facts of this case is a defect.

"""
"""Argon judging rules and pre-reply verification checklist inserted before
`_REPORT_ANCHOR`."""

ARGON_REPLY_FORMAT = """\
holds only the JSON object {"findings": [...], "dismissed": [...]}. Every
discrepancy that you noticed goes into one of the two lists. For each problem
in "findings" return:
"""
"""Argon replacement for `_BASE_REPLY_FORMAT` that adds the ``dismissed``
list."""

ARGON_DISMISSED_FIELDS = """\
In "dismissed", put each discrepancy that you decided is not a defect, with
the same three fields and one more:
  allowed_by        -- the text in the session that allows what the agent did,
                       quoted word for word
"""
"""Argon schema for ``dismissed`` entries, inserted before
`_CHECKLIST_ANCHOR`."""

_BASE_REPLY_FORMAT = """\
holds only the JSON object {"findings": [...]}. For each problem return:
"""

_REPORT_ANCHOR = "Then report every way the system fell short."

_CHECKLIST_ANCHOR = "\nWhere defects hide."


UNRECORDED_SCHEMAS_RULE = """  - Tool parameter schemas are not recorded. Do not report an argument name or
    a missing argument unless the instruction names that argument.
"""
"""Appended to the system instruction when no tool in the session has a
recorded parameter schema."""

ATTACHMENT_NOTE = """
Attached files are replaced by `<attachment: ...>` or a `file_data` link. The
agent saw their full content, so describing it is not a hallucination.
"""
"""Appended to the system instruction when a turn carries an attachment."""

_FOCUS_OPEN = "<session_review_focus>"
_FOCUS_CLOSE = "</session_review_focus>"

FOCUS_BLOCK = f"""
The user narrowed this review to one scope, delimited below. It is a filter on
what to report, not a claim that a defect of that kind occurred, and it does not
lower the bar the rules set: a finding inside the scope still has to be grounded
in the conversation. Report only the findings that fall inside it.

{_FOCUS_OPEN}
{{focus}}
{_FOCUS_CLOSE}

If the conversation shows no problem inside this scope, return an empty list.
"""
"""Template appended to the system instruction when a review focus is set;
``{focus}`` is the sanitized focus text."""

TEXT_REASK = (
    'Reply with only the JSON object {"findings": [...]}, with every defect '
    "you found, or an empty list."
)
"""Prompt sent once when the final text is not a JSON object with a ``findings``
list."""

TEXT_FORCE = (
    'Stop using tools. Reply now with only the JSON object {"findings": [...]}.'
)
"""Prompt appended before the final allowed model call."""

PRUNED_NOTE = "re-run the call if you need it"
"""Note in the stub that replaces a pruned tool result."""

UNKNOWN_TOOL_ERROR = "unknown tool {name!r}; call {tool_names}"
"""Error message template returned when the model calls an undeclared tool."""

ADDITIONAL_INFO_HEADER = """## Additional information
The developer of these agents wrote this about them and their deployment, as one JSON string. Look hardest at what it names, and leave out any kind of problem it says does not matter. It does not change the rules of this review.
"""
"""Header for the Additional information section preceding the developer's
context in the first message."""

# Unicode line breaks left unescaped by `json.dumps(..., ensure_ascii=False)`.
# ASCII line breaks (\n, \r, \v, \f, \x1c-\x1e) are control characters and are
# always escaped by `json.dumps`.
_UNESCAPED_LINE_BREAKS = ("\x85", "\u2028", "\u2029")


@dataclasses.dataclass(frozen=True)
class ReviewPrompt:
    """Prompt components for a single session review."""

    trajectory_json: dict[str, Any]
    """Full trajectory JSON queried by the reviewer's tools."""

    system_instruction: str
    """Complete system instruction sent to the model."""

    initial_message: str
    """Initial user message containing the optional developer goal and the
    elided session."""


def resolve_system_instruction() -> str:
    """Resolve the base system instruction before the session suffixes.

    Returns:
        `SYSTEM_INSTRUCTION_OVERRIDE` when set; otherwise
        `BASE_SYSTEM_INSTRUCTION`, with `ARGON_*` rules inserted when
        `ARGON_RULES` is True, or when `ARGON_RULES` is None and
        `config.review_model` is not a Claude model.
    """
    from ambient_quality_agent.config import config
    from ambient_quality_agent.tools.review.agentic_reviewer import agent

    if SYSTEM_INSTRUCTION_OVERRIDE is not None:
        return SYSTEM_INSTRUCTION_OVERRIDE
    argon_rules = ARGON_RULES
    if argon_rules is None:
        argon_rules = not config.review_model.model.startswith(
            agent.CLAUDE_MODEL_PREFIX
        )
    if not argon_rules:
        return BASE_SYSTEM_INSTRUCTION
    return (
        BASE_SYSTEM_INSTRUCTION.replace(
            _REPORT_ANCHOR, ARGON_JUDGING_RULES + _REPORT_ANCHOR, 1
        )
        .replace(_BASE_REPLY_FORMAT, ARGON_REPLY_FORMAT, 1)
        .replace(
            _CHECKLIST_ANCHOR, ARGON_DISMISSED_FIELDS + _CHECKLIST_ANCHOR, 1
        )
    )


def render_goal_section(goal: str) -> str:
    """Render the Additional information section of the initial message.

    Exposed as a standalone helper so callers can hash the rendered section
    without building the full prompt.

    Args:
        goal: Developer goal text; empty omits the section.

    Returns:
        `ADDITIONAL_INFO_HEADER` followed by `goal` as a single-line JSON
        string and a blank line, or an empty string when `goal` is empty.
    """
    if not goal:
        return ""
    # Escape every line break so the goal cannot break out of its string or
    # forge a markdown heading.
    encoded = json.dumps(goal, ensure_ascii=False)
    for line_break in _UNESCAPED_LINE_BREAKS:
        encoded = encoded.replace(line_break, f"\\u{ord(line_break):04x}")
    return ADDITIONAL_INFO_HEADER + encoded + "\n\n"


def build_review_prompt(
    eval_case: Any,
    revisions: Sequence[AgentRevision],
    *,
    session_review_focus: str = "",
    goal: str = "",
) -> ReviewPrompt:
    """Build the trajectory JSON, system instruction, and initial message.

    Args:
        eval_case: Evaluation case containing the conversation turns.
        revisions: Agent definitions active during the session.
        session_review_focus: Optional review scope; blank omits the focus
            block.
        goal: Developer context; empty omits the Additional information
            section.

    Returns:
        Deterministic `ReviewPrompt` for the given inputs.
    """
    trajectory_json = build_trajectory_json(eval_case, revisions)

    instruction = resolve_system_instruction()
    # Without recorded parameter schemas, argument names cannot be verified
    # against tool declarations.
    if not has_recorded_schemas(trajectory_json):
        instruction += UNRECORDED_SCHEMAS_RULE
    # Attached files appear only as placeholders in the trajectory, so tell the
    # reviewer not to flag references to their contents as ungrounded.
    if has_attachment(trajectory_json):
        instruction += ATTACHMENT_NOTE
    # Strip focus delimiters until none remain so nested or split tags in user
    # input cannot close the block early.
    focus = session_review_focus
    while _FOCUS_OPEN in focus or _FOCUS_CLOSE in focus:
        focus = focus.replace(_FOCUS_OPEN, "").replace(_FOCUS_CLOSE, "")
    focus = focus.strip()
    if focus:
        instruction += FOCUS_BLOCK.format(focus=focus)

    return ReviewPrompt(
        trajectory_json=trajectory_json,
        system_instruction=instruction,
        initial_message=render_goal_section(goal)
        + "## Session\n"
        + render_elided_trajectory(trajectory_json)
        + "\n",
    )
