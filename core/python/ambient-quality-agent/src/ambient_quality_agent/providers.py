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

"""What a storage provider holds until a backend is bound to it.

A provider is a module attribute holding the factory that builds a store, such
as `insight_tools.reader_factory`. This module imports nothing, so a module can
declare a provider without depending on the backend that fills it.

An unbound provider raises rather than falling back to a default. A provider
that fell back to BigQuery would let one process write to two places at once:

- the providers an entry point binds go where it meant them to go;
- the ones it misses go to the dataset.

Nothing fails, and half the rows land in the wrong place. Raising names the
provider and the call that binds it.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, NoReturn


class UnboundProviderError(RuntimeError):
    """A storage provider was used before any backend was bound to it."""


def build_unbound_provider(name: str) -> Callable[..., NoReturn]:
    """Creates a placeholder callable for an unbound storage provider.

    Args:
        name: The provider's identifier displayed if called before binding.

    Returns:
        Callable that raises UnboundProviderError when invoked.
    """

    def refuse(*args: Any, **kwargs: Any) -> NoReturn:
        del args, kwargs
        raise UnboundProviderError(
            f"{name} was used before a storage backend was bound; "
            "call ambient_quality_agent.backends.configure_providers() at start-up."
        )

    return refuse
