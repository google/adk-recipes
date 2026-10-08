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

"""Structural protocols for ADK context and session state, and the HTTP context.

Its own module because both the config layer and the tools that read it need
the types, and anything richer to hang them off would import one of them.
"""

from __future__ import annotations

import dataclasses
from typing import Any, Protocol

from google.adk.sessions import Session


class ADKStateLike(Protocol):
    """Dict-like session state (ADK `State` or a plain `dict`).

    ADK's `State` is not a `dict`/`Mapping` (its MRO is just `object`), so no
    builtin type covers both it and the plain dicts used in tests; this
    Protocol captures exactly the operations the orchestrator relies on so
    both satisfy it. Methods use positional-only parameters to stay
    compatible with `dict`, `State`, and `MappingProxyType` (whose dunders
    are positional).
    """

    def get(self, key: str, default: Any = ..., /) -> Any: ...
    def __getitem__(self, key: str, /) -> Any: ...
    def setdefault(self, key: str, default: Any = ..., /) -> Any: ...
    def __setitem__(self, key: str, value: Any, /) -> None: ...


class LaunchContext(Protocol):
    """Protocol for the execution context a tool or an investigation runs under.

    ADK `Context` classes satisfy this protocol. Properties are read-only to
    match ADK property definitions. `session` is optional because HTTP triggers
    arrive outside agent sessions.

    A tool typed to this protocol must keep its parameter named `tool_context`.
    ADK matches a context parameter against its own `Context` class, which a
    protocol cannot satisfy, so it falls back to that name. Renaming it breaks
    the tool's declaration.
    """

    @property
    def state(self) -> ADKStateLike: ...
    @property
    def session(self) -> Session | None: ...


@dataclasses.dataclass(frozen=True)
class RouteContext:
    """The `LaunchContext` an HTTP route runs its work under.

    Overrides are per-session state written during a chat turn, and a request
    reaching a route belongs to no session, so the state starts empty and the
    work sees the deploy-time configuration.
    """

    state: ADKStateLike = dataclasses.field(default_factory=dict[str, Any])
    session: None = None


@dataclasses.dataclass(frozen=True)
class DetachedContext:
    """A `LaunchContext` over a copy of another context's state.

    For work handed to a thread. ADK's session-backed `State` is not
    thread-safe, and it records every write as a delta for the invocation that
    owns it to commit; a copy reads the same values and its writes reach
    nobody, which is what work running off the owning loop needs.

    `session` is `None` for the same reason: a `Session` belongs to the
    invocation that opened it.
    """

    state: ADKStateLike = dataclasses.field(default_factory=dict[str, Any])
    session: None = None

    @classmethod
    def from_context(cls, context: LaunchContext) -> DetachedContext:
        """Detaches `context` by copying its state into a dict and dropping its session.

        Args:
            context: Source context to snapshot.

        Returns:
            New DetachedContext instance with copied state.
        """
        state: Any = context.state
        # ADK's `State` is not a Mapping -- its MRO is just `object` -- so the
        # copy goes through the accessor it offers. A plain dict, which is what
        # a route and the tests pass, copies directly.
        values = state.to_dict() if hasattr(state, "to_dict") else state
        return cls(state=dict(values))
