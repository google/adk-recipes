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

"""The live probe must judge actual content independently of plugin logs."""

import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from google.cloud import modelarmor_v1

from safety_plugins.plugins.model_armor.constants import ModelArmorMethod
from scripts.live_model_armor import EMAIL, VerdictLogHandler, run_case
from scripts.live_model_armor import main as run_probe


@pytest.mark.asyncio
async def test_probe_uses_resolved_gcloud_and_literal_account(monkeypatch):
    account = "test@example.com; echo ignored"
    command = MagicMock(
        return_value=SimpleNamespace(stdout="synthetic-token\n")
    )
    run = AsyncMock(return_value=True)
    monkeypatch.setattr("shutil.which", lambda _: "/opt/google/bin/gcloud")
    monkeypatch.setattr("scripts.live_model_armor.subprocess.run", command)
    monkeypatch.setattr("scripts.live_model_armor.run_case", run)
    monkeypatch.setattr(
        "scripts.live_model_armor.logging.getLogger", MagicMock()
    )
    args = SimpleNamespace(
        account=account, project="test-project", case="benign"
    )

    assert await run_probe(args) == 0

    command.assert_called_once_with(
        [
            "/opt/google/bin/gcloud",
            "auth",
            "print-access-token",
            f"--account={account}",
        ],
        check=True,
        capture_output=True,
        text=True,
        timeout=30,
        shell=False,
    )
    credentials = run.await_args.args[1]
    assert credentials.token == "synthetic-token"
    assert credentials.quota_project_id == "test-project"


@pytest.mark.asyncio
async def test_probe_missing_gcloud_fails_before_authentication(monkeypatch):
    command = MagicMock()
    monkeypatch.setattr("shutil.which", lambda _: None)
    monkeypatch.setattr("scripts.live_model_armor.subprocess.run", command)
    args = SimpleNamespace(account="test@example.com", project="test-project")

    with pytest.raises(FileNotFoundError, match="gcloud"):
        await run_probe(args)

    command.assert_not_called()


@pytest.mark.parametrize(
    "replacement, expected",
    [("[REDACTED]", True), ("WRONG", False), (EMAIL, False)],
)
@pytest.mark.parametrize("collect_logs", [False, True])
@pytest.mark.asyncio
async def test_live_probe_requires_exact_redaction_without_logs(
    monkeypatch, caplog, replacement, expected, collect_logs
):
    client = MagicMock()
    client.close = AsyncMock()
    client.sanitize = AsyncMock(
        side_effect=[
            modelarmor_v1.SanitizeUserPromptResponse(
                sanitization_result={
                    "invocation_result": "SUCCESS",
                    "filter_match_state": "MATCH_FOUND",
                    "filter_results": {
                        "sdp": {
                            "sdp_filter_result": {
                                "deidentify_result": {
                                    "execution_state": "EXECUTION_SUCCESS",
                                    "match_state": "MATCH_FOUND",
                                    "data": {"text": f"Contact {replacement}."},
                                }
                            }
                        }
                    },
                }
            ),
            modelarmor_v1.SanitizeModelResponseResponse(
                sanitization_result={
                    "invocation_result": "SUCCESS",
                    "filter_match_state": "NO_MATCH_FOUND",
                }
            ),
        ]
    )
    monkeypatch.setattr(
        "scripts.live_model_armor.ModelArmorClient", lambda **_: client
    )
    # Exercise both absent logs and genuine "redact" logs attached to an
    # incorrect transformation. Neither may determine the probe's verdict.
    logs = VerdictLogHandler()
    args = SimpleNamespace(
        project="test-project",
        location="europe-west1",
        template="test-template",
        email_replacement="[REDACTED]",
    )

    logger = logging.getLogger("safety_plugins.plugins.model_armor")
    if collect_logs:
        logger.addHandler(logs)
    try:
        with caplog.at_level(logging.INFO, logger=logger.name):
            passed = await run_case(
                args, None, logs, "user_sdp", f"Contact {EMAIL}."
            )
    finally:
        if collect_logs:
            logger.removeHandler(logs)

    assert passed is expected
    assert bool(logs.verdicts) is collect_logs


@pytest.mark.parametrize("name", ["mixed_tool", "mixed_thought"])
@pytest.mark.asyncio
async def test_runner_mixed_content_evidence(monkeypatch, name):
    async def sanitize(method, text, **kwargs):
        result = {
            "invocation_result": "SUCCESS",
            "filter_match_state": "NO_MATCH_FOUND",
        }
        if EMAIL in text:
            result.update(
                filter_match_state="MATCH_FOUND",
                filter_results={
                    "sdp": {
                        "sdp_filter_result": {
                            "deidentify_result": {
                                "execution_state": "EXECUTION_SUCCESS",
                                "match_state": "MATCH_FOUND",
                                "data": {
                                    "text": text.replace(EMAIL, "[REDACTED]")
                                },
                            }
                        }
                    }
                },
            )
        if method == ModelArmorMethod.SANITIZE_USER_PROMPT:
            return modelarmor_v1.SanitizeUserPromptResponse(
                sanitization_result=result
            )
        return modelarmor_v1.SanitizeModelResponseResponse(
            sanitization_result=result
        )

    client = MagicMock()
    client.close = AsyncMock()
    client.sanitize = AsyncMock(side_effect=sanitize)
    monkeypatch.setattr(
        "scripts.live_model_armor.ModelArmorClient", lambda **_: client
    )
    args = SimpleNamespace(
        project="test-project",
        location="europe-west1",
        template="test-template",
        email_replacement="[REDACTED]",
    )
    assert await run_case(
        args,
        None,
        VerdictLogHandler(),
        name,
        "Return the test response.",
        "Completed." if name == "mixed_tool" else None,
    )
