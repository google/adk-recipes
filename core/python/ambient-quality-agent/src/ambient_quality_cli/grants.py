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

"""Runs the grant and revoke commands `attach` and `detach` print.

The commands come from `ambient_quality_shared.grant_commands`. `attach`
prints those for the reads the deployment is denied and, once confirmed, runs
exactly the commands it printed, with the user's credentials, so a grant is
never made that the user was not shown. `detach` runs the revocations of the
grants recorded on the attachment.
"""

from __future__ import annotations

import shutil
import subprocess
from collections.abc import Callable, Sequence

from ambient_quality_shared.grant_commands import Grant, Revocation


class GrantError(Exception):
    """A grant or revoke command could not be run, or failed."""

    def __init__(
        self, message: str, completed: Sequence[Grant | Revocation] = ()
    ) -> None:
        """Initializes the error.

        Args:
            message: What went wrong.
            completed: The commands that succeeded.
        """
        super().__init__(message)
        self.completed = tuple(completed)


def render_grant_lines(
    grants: Sequence[Grant], ungranted: Sequence[str], *, account: str
) -> list[str]:
    """Renders the grant commands as lines of the attach plan.

    Args:
        grants: Commands to show.
        ungranted: Denied targets that got no command.
        account: AQuA's service account, as the deployment reported it.

    Returns:
        Lines to print, in order; empty when nothing was denied.
    """
    lines = []
    if grants:
        lines.append(
            f"\nTo let {account} make those reads, run with your own "
            "credentials:"
        )
        lines.extend(f"  {grant.render()}" for grant in grants)
    if ungranted:
        whom = account or "AQuA's service account"
        lines.append(
            f"\nNo grant command for {', '.join(ungranted)}: grant {whom} "
            "read access to it by hand."
        )
    return lines


Runner = Callable[[list[str]], int]
"""Runs one command and returns its exit status."""


def run_grants(
    grants: Sequence[Grant],
    *,
    runner: Runner | None = None,
    which: Callable[[str], str | None] | None = None,
    echo: Callable[[str], None] = print,
) -> None:
    """Runs the grant commands in order, stopping at the first that fails.

    Args:
        grants: Commands to run, as printed.
        runner: Runs one command; `subprocess.run`, inheriting the terminal,
            when None.
        which: Resolves a program on PATH; `shutil.which` when None.
        echo: Prints one line.

    Raises:
        GrantError: If a program is missing or a command exits non-zero; its
            `completed` holds the grants made before the failure.
    """
    _run_commands(
        grants,
        runner=runner,
        which=which,
        echo=echo,
        keep_going=False,
        failure_note=(
            "Grants before it were made; every command is safe to run again."
        ),
    )


def run_revocations(
    revocations: Sequence[Revocation],
    *,
    runner: Runner | None = None,
    which: Callable[[str], str | None] | None = None,
    echo: Callable[[str], None] = print,
) -> None:
    """Runs every revoke command, carrying on past one that fails.

    A revocation does not depend on another, and one that fails because its
    binding is already gone must not keep the rest in place.

    Args:
        revocations: Commands to run, as printed.
        runner: Runs one command; `subprocess.run`, inheriting the terminal,
            when None.
        which: Resolves a program on PATH; `shutil.which` when None.
        echo: Prints one line.

    Raises:
        GrantError: If a program is missing or any command exits non-zero;
            its `completed` holds the revocations that succeeded.
    """
    _run_commands(
        revocations,
        runner=runner,
        which=which,
        echo=echo,
        keep_going=True,
        failure_note="The other revocations were made.",
    )


def _run_commands(
    commands: Sequence[Grant | Revocation],
    *,
    runner: Runner | None,
    which: Callable[[str], str | None] | None,
    echo: Callable[[str], None],
    keep_going: bool,
    failure_note: str,
) -> None:
    """Runs commands in order.

    Steps:
    1. Check that every program the commands start is on PATH, so that a
       missing one fails before any command runs.
    2. Run each command with no shell, echoing it first, and stop at the first
       that fails unless `keep_going`.
    3. Report every command that failed.

    Args:
        commands: Commands to run, as printed.
        runner: Runs one command; `_run_command` when None.
        which: Resolves a program on PATH; `shutil.which` when None.
        echo: Prints one line.
        keep_going: Whether to run the rest after a command fails.
        failure_note: What the commands that succeeded leave behind, appended
            to the message when at least one did.

    Raises:
        GrantError: If a program is missing or a command exits non-zero.
    """
    find = which or shutil.which
    missing = sorted({c.argv[0] for c in commands if find(c.argv[0]) is None})
    if missing:
        raise GrantError(
            f"Not on PATH: {', '.join(missing)}. Install the Google Cloud CLI, "
            "or run the commands above wherever it is installed."
        )
    run = runner or _run_command
    completed: list[Grant | Revocation] = []
    failed: list[str] = []
    for command in commands:
        echo(f"$ {command.render()}")
        status = run(list(command.argv))
        if status == 0:
            completed.append(command)
            continue
        failed.append(f"Exit status {status} from: {command.render()}")
        if not keep_going:
            break
    if failed:
        # The note says some commands were made, which is false when none was.
        lines = [*failed, failure_note] if completed else failed
        raise GrantError("\n".join(lines), completed=completed)


def _run_command(argv: list[str]) -> int:
    """Runs one command with no shell, its output going to the terminal.

    Args:
        argv: Command-line arguments vector.

    Returns:
        The command's exit status.
    """
    return subprocess.run(argv, check=False).returncode  # noqa: S603 - explicit argument list without shell execution
