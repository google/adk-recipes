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

"""The update trigger's gcloud commands, planned from what the APIs report."""

from __future__ import annotations

from typing import Any

import pytest
from ambient_quality_cli import update_trigger
from ambient_quality_cli.rest_client import ApiError

_TOPIC = "projects/aqua-project/topics/aqua-solo-aqua-ambient"
_KEY = "agent-project__it_agent"
_SINK = "aqua-solo-aqua-ambient-agent-project__it_agent-updates"
_SINK_URL = (
    f"https://logging.googleapis.com/v2/projects/agent-project/sinks/{_SINK}"
)
_SINKS_URL = "https://logging.googleapis.com/v2/projects/agent-project/sinks"
_POLICY_URL = f"https://pubsub.googleapis.com/v1/{_TOPIC}:getIamPolicy"
_WRITER = "serviceAccount:service-1@gcp-sa-logging.iam.gserviceaccount.com"
_DESTINATION = f"pubsub.googleapis.com/{_TOPIC}"


class _FakeApi:
    """Answers GETs by URL, in order when a URL has several replies."""

    def __init__(self, responses: dict[str, Any]):
        """Initializes the fake.

        Args:
            responses: Reply per URL: a dict, an `ApiError` to raise, or a
                list of those, answered in turn.
        """
        self.responses = responses
        self.calls: list[str] = []

    def get_json(self, url, params=None):
        """Answers a GET.

        Args:
            url: Request URL.
            params: Ignored query parameters.

        Returns:
            The scripted reply.

        Raises:
            ApiError: For a URL with no reply, or one scripted to fail.
        """
        self.calls.append(url)
        reply = self.responses.get(url, ApiError(404, "not found"))
        if isinstance(reply, list):
            reply = reply.pop(0) if len(reply) > 1 else reply[0]
        if isinstance(reply, ApiError):
            raise reply
        return reply

    def post_json(self, url, body):
        """Refuses a POST: planning only reads.

        Args:
            url: Request URL.
            body: Request body.

        Raises:
            AssertionError: Always.
        """
        raise AssertionError(f"unexpected POST {url}")


def _build_sink(engine_id: str = "42", **overrides: str) -> dict[str, str]:
    """Builds a `LogSink` as the Logging API returns the agent's sink.

    Args:
        engine_id: The engine its filter matches.
        **overrides: Fields to replace.

    Returns:
        The sink.
    """
    return {
        "name": _SINK,
        "destination": _DESTINATION,
        "filter": update_trigger.build_sink_filter(engine_id),
        "writerIdentity": _WRITER,
        **overrides,
    }


def _build_policy(*members: str) -> dict[str, Any]:
    """Builds a topic policy that lets `members` publish.

    Args:
        *members: Principals bound to the publisher role.

    Returns:
        The policy.
    """
    return {
        "bindings": [
            {"role": update_trigger.PUBLISHER_ROLE, "members": list(members)}
        ]
    }


def _plan(api: _FakeApi, engine_id: str = "42") -> update_trigger.TriggerPlan:
    """Plans the agent's trigger against the fake.

    Args:
        api: The fake API.
        engine_id: The engine to watch; empty to remove.

    Returns:
        The plan.
    """
    return update_trigger.plan_trigger(
        api,
        topic=_TOPIC,
        agent_key=_KEY,
        project="agent-project",
        engine_id=engine_id,
    )


def _build_runner(ran: list[list[str]], status: int):
    """Builds a command runner that records each command.

    Args:
        ran: Receives each command, in order.
        status: Exit status every command returns.

    Returns:
        The runner.
    """

    def run(argv: list[str]) -> int:
        ran.append(argv)
        return status

    return run


def test_the_sink_is_named_after_the_topic_and_the_key() -> None:
    assert update_trigger.build_sink_name(_TOPIC, _KEY) == _SINK


def test_a_long_sink_name_is_cut_and_hashed() -> None:
    """A log sink's name holds 100 characters; two cut names stay apart."""
    long_key = f"agent-project__{'a' * 90}"
    name = update_trigger.build_sink_name(_TOPIC, long_key)
    other = update_trigger.build_sink_name(_TOPIC, f"{long_key}b")

    assert len(name) == update_trigger.SINK_NAME_MAX_LENGTH
    assert name.endswith(update_trigger.SINK_SUFFIX)
    assert name != other


def test_a_key_that_is_not_a_key_names_no_sink() -> None:
    with pytest.raises(update_trigger.TriggerError):
        update_trigger.build_sink_name(_TOPIC, "it-agent; rm -rf")


def test_the_filter_matches_only_the_engines_completed_updates() -> None:
    log_filter = update_trigger.build_sink_filter("42")

    assert (
        'protoPayload.methodName:"ReasoningEngineService.UpdateReasoningEngine"'
        in log_filter
    )
    assert 'protoPayload.resourceName=~"/reasoningEngines/42$"' in log_filter
    assert 'operation.last="true"' in log_filter


def test_a_missing_sink_is_created_then_granted() -> None:
    """The writer identity exists only once the sink does."""
    plan = _plan(_FakeApi({}))

    create, grant = plan.commands
    assert create[:6] == (
        "gcloud",
        "logging",
        "sinks",
        "create",
        _SINK,
        _DESTINATION,
    )
    assert "--project=agent-project" in create
    assert grant == (
        "gcloud",
        "pubsub",
        "topics",
        "add-iam-policy-binding",
        _TOPIC,
        f"--member={update_trigger.WRITER_PLACEHOLDER}",
        f"--role={update_trigger.PUBLISHER_ROLE}",
    )


def test_a_sink_in_place_needs_nothing() -> None:
    plan = _plan(
        _FakeApi(
            {_SINK_URL: _build_sink(), _POLICY_URL: _build_policy(_WRITER)}
        )
    )

    assert plan.commands == ()
    assert "in place" in plan.render_lines()[0]


def test_a_sink_on_another_engine_is_updated_and_its_grant_kept() -> None:
    plan = _plan(
        _FakeApi(
            {_SINK_URL: _build_sink("7"), _POLICY_URL: _build_policy(_WRITER)}
        )
    )

    (update,) = plan.commands
    assert update[3] == "update"
    assert f"--log-filter={update_trigger.build_sink_filter('42')}" in update


def test_a_missing_grant_is_added_for_the_sinks_identity() -> None:
    plan = _plan(
        _FakeApi({_SINK_URL: _build_sink(), _POLICY_URL: _build_policy()})
    )

    (grant,) = plan.commands
    assert f"--member={_WRITER}" in grant


def test_a_sink_of_that_name_publishing_elsewhere_is_left_alone() -> None:
    """It belongs to another AQuA whose topic has the same name."""
    other = _build_sink(destination="pubsub.googleapis.com/projects/x/topics/t")

    with pytest.raises(update_trigger.TriggerError, match="leaving it alone"):
        _plan(_FakeApi({_SINK_URL: other}))


def test_a_removal_leaves_a_sink_of_that_name_publishing_elsewhere() -> None:
    """Another AQuA with the same topic name named it; detaching from this one
    must neither touch it nor fail."""
    other = _build_sink(destination="pubsub.googleapis.com/projects/x/topics/t")

    plan = _plan(_FakeApi({_SINK_URL: other}), engine_id="")

    assert plan.commands == ()


def test_a_conditional_or_unreadable_binding_is_granted_again() -> None:
    """A condition may not cover the sink's writes, and an unreadable policy
    says nothing; the grant is idempotent either way."""
    conditional = {
        "bindings": [
            {
                "role": update_trigger.PUBLISHER_ROLE,
                "members": [_WRITER],
                "condition": {"expression": "false"},
            }
        ]
    }
    for policy in (conditional, ApiError(403, "denied")):
        plan = _plan(_FakeApi({_SINK_URL: _build_sink(), _POLICY_URL: policy}))

        (grant,) = plan.commands
        assert grant[3] == "add-iam-policy-binding"


def test_a_sink_that_cannot_be_read_stops_the_plan() -> None:
    with pytest.raises(update_trigger.TriggerError, match="Could not read"):
        _plan(_FakeApi({_SINK_URL: ApiError(403, "denied")}))


def test_removing_deletes_the_sink_and_its_grant() -> None:
    plan = _plan(
        _FakeApi(
            {
                _SINK_URL: _build_sink(),
                _POLICY_URL: _build_policy(_WRITER),
                _SINKS_URL: {"sinks": [_build_sink()]},
            }
        ),
        engine_id="",
    )

    revoke, delete = plan.commands
    assert revoke[3] == "remove-iam-policy-binding"
    assert f"--member={_WRITER}" in revoke
    assert delete == (
        "gcloud",
        "logging",
        "sinks",
        "delete",
        _SINK,
        "--project=agent-project",
        "--quiet",
    )


def test_removing_keeps_a_grant_another_sink_publishes_with() -> None:
    """Cloud Logging can give every sink in a project one writer identity."""
    sibling = _build_sink(name="aqua-solo-aqua-ambient-other_agent-updates")
    plan = _plan(
        _FakeApi(
            {
                _SINK_URL: _build_sink(),
                _POLICY_URL: _build_policy(_WRITER),
                _SINKS_URL: {"sinks": [_build_sink(), sibling]},
            }
        ),
        engine_id="",
    )

    (delete,) = plan.commands
    assert delete[3] == "delete"


def test_removing_what_is_not_there_needs_nothing() -> None:
    plan = _plan(_FakeApi({}), engine_id="")

    assert plan.commands == ()
    assert plan.render_lines() == []


def test_applying_fills_in_the_identity_of_the_sink_it_created() -> None:
    api = _FakeApi({_SINK_URL: [ApiError(404, "not found"), _build_sink()]})
    plan = _plan(api)
    ran: list[list[str]] = []

    update_trigger.apply_trigger(
        plan,
        api=api,
        runner=_build_runner(ran, 0),
        echo=lambda line: None,
        which=lambda program: f"/bin/{program}",
    )

    assert ran[0][3] == "create"
    assert f"--member={_WRITER}" in ran[1]


def test_a_failed_command_stops_the_rest() -> None:
    plan = _plan(_FakeApi({}))
    ran: list[list[str]] = []

    with pytest.raises(update_trigger.TriggerError, match="Exit status 1"):
        update_trigger.apply_trigger(
            plan,
            api=_FakeApi({}),
            runner=_build_runner(ran, 1),
            echo=lambda line: None,
            which=lambda program: f"/bin/{program}",
        )

    assert len(ran) == 1


def test_a_created_sink_with_no_identity_is_not_granted() -> None:
    api = _FakeApi(
        {
            _SINK_URL: [
                ApiError(404, "not found"),
                _build_sink(writerIdentity=""),
            ]
        }
    )
    plan = _plan(api)
    ran: list[list[str]] = []

    with pytest.raises(update_trigger.TriggerError, match="no writer identity"):
        update_trigger.apply_trigger(
            plan,
            api=api,
            runner=_build_runner(ran, 0),
            echo=lambda line: None,
            which=lambda program: f"/bin/{program}",
        )

    assert len(ran) == 1


def test_a_failure_after_a_command_ran_says_a_rerun_picks_up_from_there() -> (
    None
):
    api = _FakeApi({_SINK_URL: [ApiError(404, "not found"), _build_sink()]})
    statuses = iter([0, 1])

    with pytest.raises(
        update_trigger.TriggerError, match="commands before it ran"
    ):
        update_trigger.apply_trigger(
            _plan(api),
            api=api,
            runner=lambda argv: next(statuses),
            echo=lambda line: None,
            which=lambda program: f"/bin/{program}",
        )


def test_applying_without_gcloud_runs_nothing() -> None:
    with pytest.raises(update_trigger.TriggerError, match="Not on PATH"):
        update_trigger.apply_trigger(
            _plan(_FakeApi({})),
            api=_FakeApi({}),
            runner=lambda argv: 0,
            echo=lambda line: None,
            which=lambda program: None,
        )
