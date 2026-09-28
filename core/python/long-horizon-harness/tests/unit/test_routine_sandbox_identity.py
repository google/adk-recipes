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

from types import SimpleNamespace

from app.sandbox.lifecycle import (
    find_latest_user_sandbox,
    find_routine_sandbox,
    find_user_sandbox,
    routine_display_name,
    sandbox_display_name,
)

IMAGE = "us-central1-docker.pkg.dev/p/lha-sandbox/runtime:v0.11.0"
ENGINE = "projects/p/locations/l/reasoningEngines/r"


def _sandbox_stub(name, display_name, state="STATE_RUNNING", create_time="t1"):
    return SimpleNamespace(
        name=name,
        display_name=display_name,
        state=SimpleNamespace(name=state),
        create_time=create_time,
    )


def _client(sandboxes):
    client = SimpleNamespace()
    client.agent_engines = SimpleNamespace()
    client.agent_engines.sandboxes = SimpleNamespace(
        list=lambda *, name: iter(sandboxes)
    )
    return client


def test_routine_display_name_shape():
    assert routine_display_name("digest", IMAGE) == "lhart-digest-v0_11_0"


def test_routine_name_is_disjoint_from_user_prefix():
    # A routine name must never look like any user's sandbox prefix.
    rname = routine_display_name("digest", IMAGE)
    assert not rname.startswith("lha-")  # user prefix is `lha-<user>-`
    # ...and a user sandbox is never a routine name.
    assert not sandbox_display_name("alice", IMAGE).startswith("lhart-")


def test_find_routine_sandbox_matches_running_routine():
    sandboxes = [
        _sandbox_stub("sb-r", "lhart-digest-v0_11_0"),
        _sandbox_stub(
            "sb-stopped", "lhart-digest-v0_11_0", state="STATE_STOPPED"
        ),
        _sandbox_stub("sb-other", "lhart-weekly-v0_11_0"),
    ]
    got = find_routine_sandbox(
        _client(sandboxes),
        agent_instance_name=ENGINE,
        routine_id="digest",
        image_uri=IMAGE,
    )
    assert got == "sb-r"


def test_find_routine_sandbox_none_when_absent():
    got = find_routine_sandbox(
        _client([_sandbox_stub("sb-u", "lha-alice-v0_11_0")]),
        agent_instance_name=ENGINE,
        routine_id="digest",
        image_uri=IMAGE,
    )
    assert got is None


def test_user_and_routine_lookups_are_mutually_exclusive():
    # A pool with both a user sandbox and a routine sandbox.
    sandboxes = [
        _sandbox_stub("sb-user", "lha-alice-v0_11_0"),
        _sandbox_stub("sb-routine", "lhart-digest-v0_11_0"),
    ]
    client = _client(sandboxes)
    # routine lookup returns ONLY the routine sandbox
    assert (
        find_routine_sandbox(
            client,
            agent_instance_name=ENGINE,
            routine_id="digest",
            image_uri=IMAGE,
        )
        == "sb-routine"
    )
    # user lookups never return the routine sandbox
    assert (
        find_user_sandbox(
            client, agent_instance_name=ENGINE, user_id="alice", image_uri=IMAGE
        )
        == "sb-user"
    )
    latest = find_latest_user_sandbox(
        client, agent_instance_name=ENGINE, user_id="alice"
    )
    assert latest is not None and latest[0] == "sb-user"
    # a routine_id that happens to equal a username still can't reach the user box
    assert (
        find_routine_sandbox(
            client,
            agent_instance_name=ENGINE,
            routine_id="alice",
            image_uri=IMAGE,
        )
        is None
    )
