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

"""Whether a second sweep may start for an observed agent.

A sweep is sequential and writes the insight store as it goes, so two of them
running concurrently for one agent interleave their writes.

`InvestigationLock` guards execution: a launch attempts to claim the agent,
either receiving approval or identifying the run already holding it. A refused
launch returns the active run rather than starting a second sweep, so the interface
returns the holder's run ID instead of a boolean.

Lock scope depends on execution mode:
- `InProcessInvestigationLock`: holder table protected by `threading.Lock` for
  sweeps running inside this process (`sync_investigation` mode).
- `UnguardedInvestigationLock`: permits every claim for durable execution paths
  where sweeps execute in independent Agent Runtime runs.

Deployed durable launches run independently and rely on counting in-flight runs
in the investigations store (`job_scheduling.MAX_RUNS_IN_FLIGHT`) to bound
concurrent executions.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import Protocol

from ambient_quality_agent.config import Config


class InvestigationLock(Protocol):
    """A claim on the right to sweep one observed agent."""

    def acquire(self, agent_name: str, run_id: str) -> str | None:
        """Claim the right to sweep `agent_name` as `run_id`.

        Args:
            agent_name: Name of the observed agent.
            run_id: Unique identifier of the requesting run.

        Returns:
            `None` when the claim is granted, or the ID of the run already holding it.
        """

    def release(self, agent_name: str, run_id: str) -> None:
        """Release the claim granted by `acquire`.

        Args:
            agent_name: Name of the observed agent.
            run_id: Unique identifier of the holding run.
        """


class UnguardedInvestigationLock:
    """Grants every claim: the durable path's behaviour, written down.

    The sweep this would guard runs as a separate Agent Runtime execution, so a
    lock in this process cannot see it. Refusing here would only refuse the
    submits that happen to share a process, which is neither the guard that is
    wanted nor a subset of it.
    """

    def acquire(self, agent_name: str, run_id: str) -> str | None:
        return None

    def release(self, agent_name: str, run_id: str) -> None:
        return None


class InProcessInvestigationLock:
    """One sweep per observed agent, among the sweeps this process runs.

    The `threading.Lock` guards the holder table and nothing else: a claim is
    refused rather than waited for, so a caller answers with the run already
    going instead of queueing a second one behind it. Claims are keyed by agent
    because two agents share no insight store.
    """

    def __init__(self) -> None:
        self._guard = threading.Lock()
        self._holders: dict[str, str] = {}

    def acquire(self, agent_name: str, run_id: str) -> str | None:
        with self._guard:
            holder = self._holders.get(agent_name)
            if holder is not None:
                return holder
            self._holders[agent_name] = run_id
            return None

    def release(self, agent_name: str, run_id: str) -> None:
        with self._guard:
            if self._holders.get(agent_name) == run_id:
                del self._holders[agent_name]


_UNGUARDED = UnguardedInvestigationLock()

_IN_PROCESS = InProcessInvestigationLock()
"""One per process: two instances would each grant the same claim."""


def _create_default_lock(config: Config) -> InvestigationLock:
    """Return in-process lock when running synchronously; unguarded lock otherwise.

    Args:
        config: Agent configuration.

    Returns:
        The configured `InvestigationLock` instance.
    """
    return _IN_PROCESS if config.sync_investigation else _UNGUARDED


lock_factory: Callable[[Config], InvestigationLock] = _create_default_lock
"""Swappable seam; a distributed implementation would be installed here."""
