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

"""Choosing where storage lives, once, at start-up.

Two properties, and they are what the rest of the design rests on:

- A provider is either bound deliberately or it fails.
- A backend is bound whole or not at all.

Together they rule out a process that writes to two places, and an entry point
that gets a backend by forgetting to ask for one.
"""

from __future__ import annotations

from typing import Any

import pytest
from ambient_quality_agent import backends, providers


def test_an_unbound_provider_refuses_rather_than_guessing() -> None:
    """An entry point that installs nothing fails, and says what to call.

    A provider that defaulted to BigQuery would work in a deployment and be wrong
    in a standalone run, with nothing to say so.
    """
    refuse = providers.build_unbound_provider("insight_store_factory")

    with pytest.raises(providers.UnboundProviderError) as raised:
        refuse({"observed_agent_name": "root_agent"})

    assert "insight_store_factory" in str(raised.value)
    assert "backends.configure_providers()" in str(raised.value)


@pytest.mark.parametrize("name", sorted(backends.PROVIDERS))
def test_configuring_binds_every_provider(name: str) -> None:
    """Every provider in `PROVIDERS` is bound.

    Parametrized over the list `bind_providers` walks, so a provider added to the tree and
    forgotten in the list is not also forgotten here.
    """
    target = backends.PROVIDERS[name]
    unbound = providers.build_unbound_provider(name)
    original = getattr(target.module, target.attribute)
    setattr(target.module, target.attribute, unbound)
    try:
        with backends.override_providers():
            assert getattr(target.module, target.attribute) is not unbound
    finally:
        setattr(target.module, target.attribute, original)


def test_a_backend_missing_a_provider_is_refused_whole() -> None:
    """A missing key is refused, rather than leaving that provider unbound.

    An unbound provider raises somewhere far from the cause.
    """
    partial = dict(backends.build_deployed_bindings())
    partial.pop("insight_reader")

    with pytest.raises(ValueError, match="insight_reader"):
        backends.bind_providers(partial)


def test_a_backend_with_an_unknown_provider_is_refused_too() -> None:
    """An unknown key is refused too.

    A binding nothing reads is a rename that half landed. Installing it would
    leave the real provider unbound and look like it had worked.
    """
    extra = dict(backends.build_deployed_bindings()) | {
        "insight_readr": lambda state: None
    }

    with pytest.raises(ValueError, match="insight_readr"):
        backends.bind_providers(extra)


def test_overriding_restores_what_was_there_rather_than_the_default() -> None:
    """Each provider gets back what it held on the way in, not the shipped default.

    Nested inside another swap -- `patched_factories`, the quality harness --
    this has to give that one back.
    """
    target = backends.PROVIDERS["insight_writer"]
    sentinel: Any = object()
    original = getattr(target.module, target.attribute)
    setattr(target.module, target.attribute, sentinel)
    try:
        with backends.override_providers():
            pass
        assert getattr(target.module, target.attribute) is sentinel
    finally:
        setattr(target.module, target.attribute, original)


@pytest.mark.parametrize("name", sorted(backends.PROVIDERS))
def test_every_provider_declares_what_it_returns(name: str) -> None:
    """Each provider is annotated with the contract it returns.

    Unannotated, a type checker infers the unbound placeholder's
    `(...) -> NoReturn`, and every call on what the provider returns goes
    unchecked -- a typo in a method name passes `ty`.
    """
    target = backends.PROVIDERS[name]

    assert target.attribute in getattr(target.module, "__annotations__", {})


def test_a_deployment_keeps_its_documents_in_the_jobs_bucket(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Read from the environment when the backend is built, as a deployment's
    bucket does not change per request."""
    from ambient_quality_agent.tools.objects import store as objects
    from ambient_quality_agent.tools.objects.gcs_store import GcsObjectStore

    monkeypatch.setenv("AQA_JOBS_GCS_BUCKET", "the-jobs-bucket")

    with backends.override_providers():
        store = objects.jobs_store_factory()

    assert isinstance(store, GcsObjectStore)
    assert store.uri("goal.md") == "gs://the-jobs-bucket/goal.md"


def test_a_deployment_keeps_source_snapshots_in_the_source_bucket(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from ambient_quality_agent.tools.objects import store as objects

    monkeypatch.setenv("AQA_SOURCE_GCS_BUCKET", "the-source-bucket")

    with backends.override_providers():
        store = objects.source_store_factory()

    assert store is not None
    assert store.uri("a/1/") == "gs://the-source-bucket/a/1/"


def test_a_deployment_without_a_source_bucket_has_no_source_store(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """None rather than a store that fails, so the reader can say why."""
    from ambient_quality_agent.tools.objects import store as objects

    monkeypatch.delenv("AQA_SOURCE_GCS_BUCKET", raising=False)

    with backends.override_providers():
        assert objects.source_store_factory() is None


def test_a_standalone_run_keeps_source_snapshots_beside_its_jobs(
    tmp_path: Any,
) -> None:
    from ambient_quality_agent.tools.objects import store as objects

    with backends.override_providers(
        standalone=True, jobs_dir=tmp_path / "job"
    ):
        store = objects.source_store_factory()

    assert store is not None
    assert store.uri("a/") == f"{tmp_path / 'source'}/a/"
