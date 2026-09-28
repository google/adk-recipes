#!/usr/bin/env python3
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

"""Live probe: can a sandbox obtain and use a GCP token under an AGENT_IDENTITY engine?

Creates a throwaway reasoning engine with ``identity_type=AGENT_IDENTITY``,
provisions a sandbox under it, and from inside the sandbox tries to (1) read the
ambient identity email and (2) mint a metadata access token and call a real Google
API with it. Everything is torn down in a finally block.

Costs real money and uses a Preview feature — ``agent_engines.create`` with
AGENT_IDENTITY may be denied, which is itself a result. curl-only inside the
sandbox (gcloud is not prebaked). Gated by RUN_SANDBOX_PROBE.

Env (source .env first): GOOGLE_CLOUD_PROJECT, LHA_RUNTIME_IMAGE,
LHA_SANDBOX_CALLER_SA. ADC needs tokenCreator on the caller SA.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time

import vertexai
from vertexai._genai.types import AgentEngineConfig, IdentityType

from app.environment.sandbox import SandboxEnvironment
from app.sandbox.lifecycle import (
    delete_sandbox,
    ensure_template,
    mint_sandbox_token,
    provision_sandbox,
)

SANDBOX_LOCATION = "us-central1"
PROBE_TTL = "1h"

# curl-only: read the ambient identity, then try a real GCP call with a
# metadata-minted token. cloudresourcemanager projects.list is a harmless read.
_IN_SANDBOX = r"""
EMAIL=$(curl -s -H 'Metadata-Flavor: Google' \
  http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/email)
echo "IDENTITY_EMAIL=${EMAIL:-<none>}"
TOKEN=$(curl -s -H 'Metadata-Flavor: Google' \
  http://metadata.google.internal/computeMetadata/v1/instance/service-accounts/default/token \
  | sed -n 's/.*"access_token":"\([^"]*\)".*/\1/p')
if [ -n "$TOKEN" ]; then
  CODE=$(curl -s -o /dev/null -w '%{http_code}' -H "Authorization: Bearer $TOKEN" \
    'https://cloudresourcemanager.googleapis.com/v1/projects?pageSize=1')
  echo "GCP_HTTP=$CODE"
else
  echo "GCP_HTTP=no-token"
fi
"""


def _log(msg: str) -> None:
    print(f"probe_agent_identity_live: {msg}", file=sys.stderr, flush=True)


def _require(var: str) -> str:
    val = os.environ.get(var, "").strip()
    if not val:
        _log(f"missing required env var {var}")
        sys.exit(2)
    return val


async def _run() -> dict[str, object]:
    project = _require("GOOGLE_CLOUD_PROJECT")
    image_uri = _require("LHA_RUNTIME_IMAGE")
    caller_sa = _require("LHA_SANDBOX_CALLER_SA")
    stamp = int(time.time())

    client = vertexai.Client(
        project=project,
        location=SANDBOX_LOCATION,
        http_options={"api_version": "v1beta1"},
    )

    result: dict[str, object] = {
        "engine_created": False,
        "engine_name": None,
        "sandbox_ready": False,
        "identity_email": None,
        "gcp_http": None,
        "raw_output": None,
        "verdict": None,
        "error": None,
    }

    engine_name = None
    provisioned = None
    env = None
    try:
        _log("creating throwaway engine with AGENT_IDENTITY (Preview) …")
        engine = client.agent_engines.create(
            config=AgentEngineConfig(
                display_name=f"lha-aiprobe-{stamp}",
                identity_type=IdentityType.AGENT_IDENTITY,
            ),
        )
        engine_name = engine.api_resource.name
        result["engine_created"] = True
        result["engine_name"] = engine_name
        _log(f"engine: {engine_name}")

        _log("provisioning sandbox under it …")
        template = ensure_template(
            client, agent_instance_name=engine_name, image_uri=image_uri
        )
        provisioned = provision_sandbox(
            client,
            agent_instance_name=engine_name,
            owner="aiprobe",
            template_name=template,
            display_name=f"lha-aiprobe-{stamp}",
            ttl=PROBE_TTL,
        )
        token = mint_sandbox_token(client, service_account=caller_sa)
        env = SandboxEnvironment(
            client=client,
            sandbox_name=provisioned.name,
            lb_host=provisioned.lb_host,
            routing_token=provisioned.routing_token,
            sandbox_token=token,
            owner="aiprobe",
        )
        await env.initialize()
        result["sandbox_ready"] = True
        _log(f"sandbox ready: {provisioned.name}")

        _log("probing identity from inside the sandbox …")
        exec_result = await env.execute(_IN_SANDBOX, timeout=90.0)
        out = ((exec_result.stdout or "") + (exec_result.stderr or "")).strip()
        result["raw_output"] = out
        for line in out.splitlines():
            if line.startswith("IDENTITY_EMAIL="):
                result["identity_email"] = line.split("=", 1)[1]
            elif line.startswith("GCP_HTTP="):
                result["gcp_http"] = line.split("=", 1)[1]

        email = result["identity_email"]
        http = result["gcp_http"]
        if http == "200":
            result["verdict"] = (
                f"USABLE: sandbox obtained a token ({email}) and called GCP (200) "
                "— ambient identity works from the sandbox"
            )
        elif http in {"401", "403"}:
            result["verdict"] = (
                f"REJECTED: token present ({email}) but GCP returned {http} "
                "— token not usable from the sandbox"
            )
        elif http == "no-token":
            result["verdict"] = (
                f"NO-TOKEN: no metadata token in the sandbox (email={email}) "
                "— AGENT_IDENTITY token is not delivered via the metadata path"
            )
        else:
            result["verdict"] = f"INCONCLUSIVE: email={email} gcp_http={http}"
    except Exception as exc:  # record the failure and fall through to cleanup
        result["error"] = f"{type(exc).__name__}: {exc}"
        _log(f"ERROR: {result['error']}")
    finally:
        _log("cleaning up …")
        if env is not None:
            try:
                await env.close()
            except Exception as exc:
                _log(f"env close failed: {exc}")
        if provisioned is not None:
            try:
                delete_sandbox(client, sandbox_name=provisioned.name)
                _log(f"deleted sandbox {provisioned.name}")
            except Exception as exc:
                _log(f"delete sandbox failed: {exc}")
        if engine_name is not None:
            try:
                client.agent_engines.delete(name=engine_name, force=True)
                _log(f"deleted engine {engine_name}")
            except Exception as exc:
                _log(f"delete engine failed (MANUAL CLEANUP NEEDED): {exc}")

    return result


def main() -> int:
    if not os.environ.get("RUN_SANDBOX_PROBE"):
        _log("set RUN_SANDBOX_PROBE=1 to run this live probe")
        return 0
    result = asyncio.run(_run())
    json.dump(result, sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
