#!/usr/bin/env python3
"""Mock Legal Reviewer CLI: list and resolve pending Legal-review tasks.

This is the human-facing side of the Legal loop (PRD "How it works -- two
feedback loops"). It only reads/writes the local legal-queue file (see
`clause_agent/tools/legal_review.py`) -- it does NOT talk to a running
agent session directly. To actually resume a paused `adk web`/`adk run`
conversation after using this script, send the agent a message noting the
task was approved/edited/rejected (e.g. "LR-a1b2c was approved by
legal@example.com: <comment>") so it can look up the task and continue --
`adk web`'s chat UI does not currently expose raw function_response
injection.

Usage:
    python scripts/legal_review_cli.py list
    python scripts/legal_review_cli.py approve LR-a1b2c --approver legal@x.com \\
        --comment "Confirmed."
    python scripts/legal_review_cli.py edit LR-a1b2c --approver legal@x.com \\
        --final-answer "Actually Net 45, see amended schedule." \\
        --comment "Corrected the term."
    python scripts/legal_review_cli.py reject LR-a1b2c --approver legal@x.com \\
        --comment "Insufficient evidence of supersession."
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from clause_agent.tools.legal_review import (
    list_pending,
    resolve_legal_review,
)


def _cmd_list(_args: argparse.Namespace) -> None:
    pending = list_pending()
    if not pending:
        print("No pending Legal-review tasks.")
        return
    for task in pending:
        print(
            f"[{task.task_id}] customer={task.customer!r} confidence={task.confidence}"
        )
        print(f"  question: {task.question}")
        print(f"  proposed_answer: {task.proposed_answer}")
        print(f"  sources: {task.sources}")
        print()


def _cmd_resolve(decision: str, args: argparse.Namespace) -> None:
    resolved = resolve_legal_review(
        task_id=args.task_id,
        decision=decision,
        approver=args.approver,
        comment=args.comment,
        final_answer=args.final_answer,
    )
    print(f"Resolved {resolved.task_id} -> {resolved.status}")
    print(f"  final_answer: {resolved.final_answer}")
    print(
        "\nTo resume the conversation, tell the agent (in adk web/adk run):\n"
        f'  "{resolved.task_id} was {resolved.status} by {args.approver}'
        f'{": " + args.comment if args.comment else ""}"'
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("list", help="List pending Legal-review tasks.")

    decision_past = {
        "approve": "approved",
        "edit": "edited",
        "reject": "rejected",
    }
    for decision in ("approve", "edit", "reject"):
        sub = subparsers.add_parser(
            decision, help=f"Mark a task as {decision_past[decision]}."
        )
        sub.add_argument("task_id")
        sub.add_argument("--approver", required=True)
        sub.add_argument("--comment", default=None)
        sub.add_argument("--final-answer", dest="final_answer", default=None)

    args = parser.parse_args()

    if args.command == "list":
        _cmd_list(args)
    else:
        _cmd_resolve(
            {"approve": "approved", "edit": "edited", "reject": "rejected"}[
                args.command
            ],
            args,
        )


if __name__ == "__main__":
    main()
