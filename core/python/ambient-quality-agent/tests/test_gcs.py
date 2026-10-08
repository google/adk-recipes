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

"""Tests for the GCS helpers: `claim_once` and the per-thread client factory.

The GCS client is faked (see `conftest.FakeGcsClient`) so the create-if-absent
precondition -- and its 412 duplicate path -- is exercised without network.
"""

from __future__ import annotations

from typing import Any

import pytest
from ambient_quality_agent.tools import gcs
from google.api_core import exceptions as api_exceptions

from .conftest import FakeGcsClient


def test_claim_once_creates_marker(monkeypatch: pytest.MonkeyPatch) -> None:
    # Uses the create-if-absent precondition (`if_generation_match=0`) so that
    # only the first claim for a given key writes the marker object.
    fake = FakeGcsClient()
    monkeypatch.setattr(gcs, "create_storage_client", lambda: fake)

    assert gcs.claim_once("bucket", "aqa-idempotency-keys/k.json") is True
    assert fake.uploads == [(("bucket", "aqa-idempotency-keys/k.json"), b"", 0)]


def test_claim_once_returns_false_when_marker_exists(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # An existing marker triggers a 412 Precondition Failed, rejecting duplicate claims.
    fake = FakeGcsClient()
    monkeypatch.setattr(gcs, "create_storage_client", lambda: fake)

    assert gcs.claim_once("bucket", "k") is True
    assert gcs.claim_once("bucket", "k") is False


def test_claim_once_propagates_unexpected_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Errors other than 412 (auth, network, quota) must propagate -- a failed
    # claim is neither "claimed" nor "already exists".
    class _BoomBlob:
        def upload_from_string(self, *_: Any, **__: Any) -> None:
            raise api_exceptions.ServiceUnavailable("503")

    class _BoomBucket:
        def blob(self, _name: str) -> _BoomBlob:
            return _BoomBlob()

    class _BoomClient:
        def bucket(self, _name: str) -> _BoomBucket:
            return _BoomBucket()

    monkeypatch.setattr(gcs, "create_storage_client", _BoomClient)
    with pytest.raises(api_exceptions.ServiceUnavailable):
        gcs.claim_once("bucket", "k")


def test_the_per_thread_factory_reuses_a_client_within_a_thread_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import threading

    monkeypatch.setattr(gcs, "create_storage_client", object)
    factory = gcs.build_per_thread_client_factory()
    other: list[object] = []

    thread = threading.Thread(target=lambda: other.append(factory()))
    thread.start()
    thread.join()

    assert factory() is factory()
    assert other[0] is not factory()
