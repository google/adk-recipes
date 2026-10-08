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

"""Smoke tests to ensure the package can be imported.

Real tests should be added alongside features.
"""


def test_package_importable() -> None:
    """The top-level package should import cleanly."""
    import ambient_quality_agent  # noqa: F401


def test_version_available() -> None:
    """The package must expose a __version__ attribute."""
    import ambient_quality_agent

    assert isinstance(ambient_quality_agent.__version__, str)
    assert ambient_quality_agent.__version__


def test_a2a_server_routes_importable() -> None:
    """The A2A server surface must import, not just the bare a2a-sdk.

    `a2a-sdk` alone resolves and installs happily; it is the `http-server`
    extra that brings sse-starlette, without which this import raises
    ModuleNotFoundError only once something serves A2A.
    """
    import a2a.server.routes  # noqa: F401
