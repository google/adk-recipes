#!/usr/bin/env python3
"""Resets ClauseIQ's local POC state to a blank slate.

Restarting `adk web`/`adk run` alone does NOT reset anything -- Memory
Bank, the audit log, and the Legal-review queue are all file-backed (see
`clause_agent/shared_libraries/config.py`) and persist across restarts by
design. This script clears that state (and, optionally, ADK's own
conversation/session history) so a demo can be replayed from scratch.

IMPORTANT: stop any running `adk web`/`adk run` process first -- deleting
files out from under a live server/session can cause errors or have the
files silently recreated mid-delete.

Usage:
    python scripts/reset_state.py                  # reset everything (asks to confirm)
    python scripts/reset_state.py --yes            # skip the confirmation prompt
    python scripts/reset_state.py --keep-sessions  # keep ADK chat history; only
                                                    # reset ClauseIQ's own memory/
                                                    # audit/legal-queue state
    python scripts/reset_state.py --dry-run        # show what would be deleted
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from clause_agent.shared_libraries import config

AGENT_DIR = Path(__file__).resolve().parent.parent / "clause_agent"


def _clauseiq_state_paths() -> list[Path]:
    """Files holding ClauseIQ's own state: Memory Bank, audit log, Legal queue."""
    return [
        config.get_memory_bank_path(),
        config.get_audit_log_path(),
        config.get_legal_queue_path(),
    ]


def _adk_session_dir() -> Path:
    """The `.adk/` folder `adk web`/`adk run` creates under the agent package
    (session.db + artifacts/) -- see
    `google.adk.cli.utils.local_storage`/`dot_adk_folder` in the installed
    `google-adk` package.
    """
    return AGENT_DIR / ".adk"


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--yes", "-y", action="store_true", help="Skip the confirmation prompt."
    )
    parser.add_argument(
        "--keep-sessions",
        action="store_true",
        help=(
            "Keep ADK chat/session history (clause_agent/.adk/); only reset"
            " ClauseIQ's own memory/audit/legal-queue state."
        ),
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be deleted, without deleting anything.",
    )
    args = parser.parse_args()

    targets = _clauseiq_state_paths()
    adk_dir = _adk_session_dir()
    if not args.keep_sessions:
        targets.append(adk_dir)

    existing = [p for p in targets if p.exists()]
    if not existing:
        print("Nothing to reset -- ClauseIQ is already at a blank slate.")
        return

    print("Will reset:")
    for p in existing:
        print(f"  - {p} ({'directory' if p.is_dir() else 'file'})")
    if args.keep_sessions and adk_dir.exists():
        print(f"\nKeeping ADK chat history: {adk_dir}")

    if args.dry_run:
        print("\n--dry-run: nothing deleted.")
        return

    if not args.yes:
        reply = input("\nProceed? [y/N] ").strip().lower()
        if reply != "y":
            print("Aborted.")
            return

    for p in existing:
        if p.is_dir():
            shutil.rmtree(p)
        else:
            p.unlink()

    print("\nDone -- ClauseIQ is back to a blank slate.")


if __name__ == "__main__":
    main()
