# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Review prompts for the background self-improvement fork.

Two prompts, picked by ``review_fork._pick_prompt``:

* ``MEMORY_REVIEW_PROMPT`` — memory-only, when the session shows no
  skill activity.
* ``COMBINED_REVIEW_PROMPT`` — both axes, when the session touched
  skills at all.

Skill-curation tooling in these prompts targets horizon's ``load_skill``
tool (a skill's instructions, or one of its bundled files via
``resource=``), write with ``write`` / ``edit`` scoped to
``.agents/skills/<name>/...``, and call ``load_skill(action='reload')``
after any write so the parent picks up the change on its next turn.
"""

from __future__ import annotations

MEMORY_REVIEW_PROMPT = (
    "Review the conversation above and consider saving to memory if appropriate.\n\n"
    "Focus on:\n"
    "1. Has the user revealed things about themselves — their persona, desires, "
    "preferences, or personal details worth remembering?\n"
    "2. Has the user expressed expectations about how you should behave, their work "
    "style, or ways they want you to operate?\n\n"
    "If something stands out, save it using the memory tool. "
    "If nothing is worth saving, just say 'Nothing to save.' and stop."
)

COMBINED_REVIEW_PROMPT = (
    "Review the conversation above and consider updating two things:\n\n"
    "**Memory**: who the user is. Did the user reveal persona, "
    "desires, preferences, personal details, or expectations about "
    "how you should behave that would still be true a week from now? "
    "Save durable facts about the user with the memory tool.\n\n"
    "**Skills**: how to do this class of task. Only update when a "
    "durable, reusable lesson emerged. 'Nothing to save.' is a fully "
    "acceptable outcome — most sessions don't yield a skill update.\n\n"
    "Target shape of the skill library: CLASS-LEVEL skills with a rich "
    "SKILL.md and a `references/` directory for session-specific detail. "
    "Not a long flat list of narrow one-session-one-skill entries.\n\n"
    "Signals — necessary but not sufficient. Look for a PATTERN, not a "
    "single moment. A single complaint in isolation is rarely enough:\n"
    "  • User correction to style, tone, format, verbosity, or workflow "
    "that reads as a general preference (would apply to the next task "
    "of the same class), not a one-off reaction to this specific "
    "output. Phrases like 'stop doing X' or 'I hate when you Y' need "
    "corroborating context before they become a durable rule.\n"
    "  • Non-trivial technique, fix, workaround, or debugging path "
    "that a future session in the same class of task would benefit "
    "from. Skip if it was specific to today's particular bug.\n"
    "  • A skill that was loaded or consulted turned out wrong, "
    "missing, or outdated — patching the loaded skill is almost "
    "always the right move.\n\n"
    "Discovering what already exists: the `<available_skills>` block "
    "is auto-injected at the top of your context — scan it for an "
    "umbrella that fits. To read a skill's full body call "
    "`load_skill(skill_name='<name>')`; for a support file call "
    "`load_skill(skill_name='<name>', resource='references/foo.md')`.\n\n"
    "Preference order for skills — pick the earliest that fits:\n"
    "  1. UPDATE A CURRENTLY-LOADED SKILL. Check what skills were "
    "loaded via `load_skill(...)` in the conversation. If one of "
    "them covers the learning, EDIT it first via "
    "`edit(path='.agents/skills/<name>/SKILL.md', "
    "edits=[{'oldText': ..., 'newText': ...}])`.\n"
    "  2. UPDATE AN EXISTING UMBRELLA. Scan `<available_skills>` and "
    "`load_skill(...)` to confirm, then edit its SKILL.md.\n"
    "  3. ADD A SUPPORT FILE under an existing umbrella via "
    "`write(path='.agents/skills/<umbrella>/<dir>/<file>', content=...)`. "
    "Three kinds: `references/<topic>.md` for session-specific detail "
    "OR condensed knowledge banks (quoted research, API docs excerpts, "
    "domain notes) written concise and task-focused; `assets/<name>."
    "<ext>` for starter files meant to be copied and modified; "
    "`scripts/<name>.<ext>` for statically re-runnable actions "
    "(verification, fixture generators, probes). Add a one-line "
    "pointer in SKILL.md so future agents find them.\n"
    "  4. CREATE A NEW CLASS-LEVEL UMBRELLA at "
    "`.agents/skills/<name>/SKILL.md` (YAML frontmatter `name`/`description` "
    "then markdown body). Name at the class level — NOT a PR number, "
    "error string, codename, library-alone name, or 'fix-X / debug-Y' "
    "session artifact. If the name only fits today's task, fall back "
    "to (1), (2), or (3), or skip.\n\n"
    "After ANY write (`write` or `edit` under `.agents/skills/`), call "
    "`load_skill(action='reload')` so the parent's next turn picks up "
    "the change. "
    "If you skip the reload, the catalog stays stale until the next "
    "session boots.\n\n"
    "If you notice overlapping existing skills, mention it — the "
    "background curator handles consolidation.\n\n"
    "Do NOT capture as skills (these become persistent self-imposed "
    "constraints that bite you later when the environment changes):\n"
    "  • Environment-dependent failures: missing binaries, fresh-install "
    "errors, post-migration path mismatches, 'command not found', "
    "unconfigured credentials, uninstalled packages. The user can fix "
    "these — they are not durable rules.\n"
    "  • Negative claims about tools or features ('browser tools do not "
    "work', 'X tool is broken', 'cannot use Y from tool Z'). These "
    "harden into refusals the agent cites against itself for months "
    "after the actual problem was fixed.\n"
    "  • Session-specific transient errors that resolved before the "
    "conversation ended. If retrying worked, the lesson is the retry "
    "pattern, not the original failure.\n"
    "  • One-off task narratives. A user asking 'summarize today's "
    "market' or 'analyze this PR' is not a class of work that warrants "
    "a skill.\n"
    "  • A single, isolated user complaint with no pattern behind it. "
    "Wait for corroboration before encoding it as a rule.\n\n"
    "If a tool failed because of setup state, capture the FIX (install "
    "command, config step, env var to set) under an existing setup or "
    "troubleshooting skill — never 'this tool does not work' as a "
    "standalone constraint.\n\n"
    "Act on whichever dimension has real signal. If neither does, "
    "say 'Nothing to save.' and stop."
)
