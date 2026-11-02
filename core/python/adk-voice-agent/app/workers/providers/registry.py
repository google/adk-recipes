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

"""Computer providers by name, selected with `COMPUTER_PROVIDER` (default `host`)."""

from __future__ import annotations

import os
from collections.abc import Callable

from app.workers.providers.base import (
    ComputerProvider,
    ComputerUnavailableError,
)

DEFAULT_PROVIDER = "host"


def _host() -> ComputerProvider:
    from app.workers.providers.host import HostProvider

    return HostProvider()


def _docker() -> ComputerProvider:
    from app.workers.providers.docker import DockerProvider

    return DockerProvider()


def _static() -> ComputerProvider:
    from app.workers.providers.static import StaticProvider

    return StaticProvider()


def _gce() -> ComputerProvider:
    from app.workers.providers.gce import GceProvider

    return GceProvider()


_FACTORIES: dict[str, Callable[[], ComputerProvider]] = {
    "host": _host,
    "docker": _docker,
    "static": _static,
    "gce": _gce,
}
_instances: dict[str, ComputerProvider] = {}


def provider_names() -> list[str]:
    return sorted(_FACTORIES)


def selected_provider_name() -> str:
    return (os.getenv("COMPUTER_PROVIDER") or DEFAULT_PROVIDER).strip().lower()


def get_computer_provider() -> ComputerProvider:
    name = selected_provider_name()
    if name not in _instances:
        factory = _FACTORIES.get(name)
        if factory is None:
            raise ComputerUnavailableError(
                f"Unknown COMPUTER_PROVIDER {name!r}; "
                f"choose one of {', '.join(provider_names())}."
            )
        _instances[name] = factory()
    return _instances[name]


def reset_computer_providers() -> None:
    _instances.clear()
