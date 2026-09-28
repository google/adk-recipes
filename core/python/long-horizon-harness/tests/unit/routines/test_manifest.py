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

from app.routines.manifest import (
    DEFAULT_DELIVERY,
    RoutineManifest,
    parse_manifest,
)


def test_parse_valid_full():
    m = parse_manifest(
        {
            "name": "Daily digest",
            "schedule": "0 8 * * *",
            "task": "summarize r/ml",
            "secrets": ["A", "B"],
            "delivery": "report_back",
        },
        routine_id="abc",
    )
    assert m == RoutineManifest(
        id="abc",
        name="Daily digest",
        schedule="0 8 * * *",
        task="summarize r/ml",
        secrets=("A", "B"),
        delivery="report_back",
    )


def test_defaults_for_secrets_and_delivery():
    m = parse_manifest(
        {"name": "n", "schedule": "s", "task": "t"}, routine_id="x"
    )
    assert m is not None
    assert m.secrets == ()
    assert m.delivery == DEFAULT_DELIVERY


def test_missing_required_returns_none():
    assert (
        parse_manifest({"name": "n", "schedule": "s"}, routine_id="x") is None
    )


def test_blank_id_returns_none():
    assert (
        parse_manifest(
            {"name": "n", "schedule": "s", "task": "t"}, routine_id=""
        )
        is None
    )


def test_non_dict_returns_none():
    assert parse_manifest(["nope"], routine_id="x") is None


def test_secrets_coercion_drops_non_strings_and_blanks():
    m = parse_manifest(
        {
            "name": "n",
            "schedule": "s",
            "task": "t",
            "secrets": ["A", 5, "  ", "B"],
        },
        routine_id="x",
    )
    assert m is not None
    assert m.secrets == ("A", "B")


def test_secrets_accepts_bare_string():
    m = parse_manifest(
        {"name": "n", "schedule": "s", "task": "t", "secrets": "STRIPE_KEY"},
        routine_id="x",
    )
    assert m is not None
    assert m.secrets == ("STRIPE_KEY",)
