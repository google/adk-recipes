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

"""Keeps the Cloud SQL backend in the Terraform module opt-in.

A deployment that leaves `storage_backend` at `bigquery` must plan no new
resources and no change to its engine. Any change to the engine's environment
or to `terraform_data.engine_identity` replaces the engine, so these tests pin
what a BigQuery deployment sees.
"""

from __future__ import annotations

import pathlib
import re

import pytest

_MODULE_DIR = (
    pathlib.Path(__file__).resolve().parents[1]
    / "terraform"
    / "modules"
    / "aqa"
)

_RESOURCE_BLOCK = re.compile(
    r'^resource\s+"(?P<type>[\w-]+)"\s+"(?P<name>[\w-]+)"\s*\{(?P<body>.*?)^\}',
    re.MULTILINE | re.DOTALL,
)

_CLOUDSQL_FILES = ("network.tf", "database.tf")


def _list_cloudsql_resources() -> list[tuple[str, str, str]]:
    """Lists the resource blocks of the Cloud SQL files.

    Returns:
        (file name, resource address, body) for each resource block.
    """
    return [
        (name, f"{m.group('type')}.{m.group('name')}", m.group("body"))
        for name in _CLOUDSQL_FILES
        for m in _RESOURCE_BLOCK.finditer(
            (_MODULE_DIR / name).read_text(encoding="utf-8")
        )
    ]


_CLOUDSQL_RESOURCES = _list_cloudsql_resources()


def _read_service_tf() -> str:
    """Reads the engine's configuration.

    Returns:
        The content of `service.tf`.
    """
    return (_MODULE_DIR / "service.tf").read_text(encoding="utf-8")


def test_the_cloudsql_files_declare_resources() -> None:
    assert len(_CLOUDSQL_RESOURCES) >= 10


@pytest.mark.parametrize(
    ("file_name", "address", "body"),
    _CLOUDSQL_RESOURCES,
    ids=[address for _, address, _ in _CLOUDSQL_RESOURCES],
)
def test_every_cloudsql_resource_is_counted_on_the_backend(
    file_name: str, address: str, body: str
) -> None:
    assert re.search(
        r"^\s*count\s*=\s*local\.(cloudsql|create_network)\s*\?\s*1\s*:\s*0\s*$",
        body,
        re.MULTILINE,
    ), f"{file_name}: {address} would be created for a BigQuery deployment."


def test_a_bigquery_deployment_adds_nothing_to_the_engine_environment() -> None:
    storage_env = re.search(
        r"^\s*storage_env\s*=\s*local\.cloudsql\s*\?\s*\{.*?\}\s*:\s*\{\}\s*$",
        _read_service_tf(),
        re.MULTILINE | re.DOTALL,
    )
    assert storage_env is not None, (
        "storage_env must be {} unless the backend is cloudsql."
    )


def test_a_bigquery_deployment_keeps_its_engine_identity() -> None:
    assert re.search(
        r"^\s*triggers_replace\s*=\s*local\.cloudsql\s*\?\s*\S+\s*:\s*null\s*$",
        _read_service_tf(),
        re.MULTILINE,
    ), "triggers_replace must stay null unless the backend is cloudsql."


def test_the_module_requires_a_provider_that_removes_the_peering() -> None:
    """`deletion_policy = "REMOVE_PEERING"` requires google provider 8.1 or later."""
    versions = (_MODULE_DIR / "versions.tf").read_text(encoding="utf-8")
    floor = re.search(
        r'"hashicorp/google"\s*version\s*=\s*">=\s*(?P<major>\d+)\.(?P<minor>\d+)',
        versions,
    )
    assert floor is not None
    assert (int(floor.group("major")), int(floor.group("minor"))) >= (8, 1)


def test_destroy_removes_the_peering_that_blocks_the_network() -> None:
    connection = next(
        body
        for _, address, body in _CLOUDSQL_RESOURCES
        if address == "google_service_networking_connection.cloudsql"
    )
    assert re.search(
        r'^\s*deletion_policy\s*=\s*"REMOVE_PEERING"\s*$',
        connection,
        re.MULTILINE,
    )
