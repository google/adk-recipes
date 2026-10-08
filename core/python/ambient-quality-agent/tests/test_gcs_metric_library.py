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

"""Tests for reading a code-metric library out of Cloud Storage."""

from __future__ import annotations

from unittest import mock

from ambient_quality_agent.tools.metrics.gcs_library import (
    LIBRARY_PREFIX,
    load_library_from_gcs,
)
from ambient_quality_agent.tools.metrics.library import MetricLibrary

_CONFIG = b"metrics_to_run: [ok]\ncustom_metrics:\n  - name: ok\n    custom_function_file: ok.py\n"
_METRIC = (
    b"EXPECTED = 'x'\n\n\ndef evaluate(instance):\n    return {'score': 1.0}\n"
)


def _load(blobs: dict[str, bytes]) -> MetricLibrary:
    def make(name: str, data: bytes) -> mock.MagicMock:
        blob = mock.MagicMock()
        blob.name = name
        blob.download_as_bytes.return_value = data
        return blob

    client = mock.MagicMock()
    client.list_blobs.return_value = [make(n, d) for n, d in blobs.items()]
    return load_library_from_gcs("b", client_factory=lambda: client)


def test_reads_the_prefix_and_loads_it() -> None:
    library = _load(
        {
            f"{LIBRARY_PREFIX}eval_config.yaml": _CONFIG,
            f"{LIBRARY_PREFIX}ok.py": _METRIC,
        }
    )

    assert set(library) == {"ok"}
    assert library["ok"].evaluate({}) == {"score": 1.0}


def test_an_empty_prefix_yields_an_empty_library() -> None:
    assert _load({}) == {}


def test_directory_placeholders_are_skipped() -> None:
    blobs = {
        LIBRARY_PREFIX: b"",
        f"{LIBRARY_PREFIX}eval_config.yaml": _CONFIG,
        f"{LIBRARY_PREFIX}ok.py": _METRIC,
    }

    assert set(_load(blobs)) == {"ok"}


def test_a_nested_object_keeps_its_relative_name() -> None:
    """`custom_function_file` is resolved against the library root, so a metric
    in a subdirectory has to key on the path the config names."""
    config = (
        b"metrics_to_run: [ok]\ncustom_metrics:\n  - name: ok\n"
        b"    custom_function_file: helpers/ok.py\n"
    )
    blobs = {
        f"{LIBRARY_PREFIX}eval_config.yaml": config,
        f"{LIBRARY_PREFIX}helpers/ok.py": _METRIC,
    }

    assert set(_load(blobs)) == {"ok"}


def test_a_broken_metric_is_isolated_from_the_healthy_ones() -> None:
    """Syntax errors in one metric are isolated so healthy metrics still load,
    preventing entire run failures."""
    config = (
        b"metrics_to_run: [ok, broken]\ncustom_metrics:\n"
        b"  - {name: ok, custom_function_file: ok.py}\n"
        b"  - {name: broken, custom_function_file: broken.py}\n"
    )
    blobs = {
        f"{LIBRARY_PREFIX}eval_config.yaml": config,
        f"{LIBRARY_PREFIX}ok.py": b"def evaluate(instance):\n    return 1.0\n",
        f"{LIBRARY_PREFIX}broken.py": b"def evaluate(instance:\n",
    }

    library = _load(blobs)

    assert sorted(library) == ["ok"], "the healthy metric still loaded"
    assert [name for name, _ in library.failures] == ["broken"]
    assert "SyntaxError" in library.failures[0][1]
