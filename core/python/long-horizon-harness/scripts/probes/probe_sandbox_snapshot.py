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

"""Empirically probe Vertex sandbox snapshot/restore semantics.

Gates Phase C (snapshot-based TTL survival). Answers, against the live API:
  (a) does a snapshot capture the full ``$HOME`` — including binaries in
      ``~/.local/bin`` with the executable bit, and symlinks — i.e. the things
      the zip ``/workspace`` migration drops?
  (b) snapshot + restore latency, and whether the restored sandbox is
      immediately usable (so a next-session restore is viable).

The "restore lands on the source image, not an arbitrary one" fact is already
settled structurally — ``CreateAgentEngineSandboxConfig`` takes EITHER
``sandbox_environment_template`` OR ``sandbox_environment_snapshot`` and a
snapshot carries no image field — so this probe does not test cross-image
restore.

Costs real money (provisions two sandboxes + a snapshot). Creates throwaway
resources with a short TTL and deletes all three in a finally block.

Env contract:
    GOOGLE_CLOUD_PROJECT       required
    AGENT_ENGINE_RESOURCE_NAME required (projects/.../reasoningEngines/...)
    LHA_RUNTIME_IMAGE          required
    LHA_SANDBOX_CALLER_SA      required (ADC needs tokenCreator on it)

Location is pinned to us-central1 (sandboxes are regional). All diagnostics go
to stderr; a JSON result object is written to stdout on completion.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import time

import vertexai

from app.environment.sandbox import SandboxEnvironment
from app.sandbox.lifecycle import (
    ProvisionedSandbox,
    _normalize_ttl,
    delete_sandbox,
    ensure_template,
    mint_sandbox_token,
    provision_sandbox,
    runtime_image_version,
)

SANDBOX_LOCATION = "us-central1"
PROBE_TTL = "1h"
SNAPSHOT_TTL = "1h"


def _log(msg: str) -> None:
    print(f"probe_sandbox_snapshot: {msg}", file=sys.stderr, flush=True)


def _require(var: str) -> str:
    val = os.environ.get(var, "").strip()
    if not val:
        _log(f"missing required env var {var}")
        sys.exit(2)
    return val


def _build_env(
    client: object, provisioned: object, caller_sa: str
) -> SandboxEnvironment:
    token = mint_sandbox_token(client, service_account=caller_sa)
    return SandboxEnvironment(
        client=client,
        sandbox_name=provisioned.name,
        lb_host=provisioned.lb_host,
        routing_token=provisioned.routing_token,
        sandbox_token=token,
        owner="probe",
    )


async def _exec(env: SandboxEnvironment, command: str) -> tuple[int, str]:
    result = await env.execute(command, timeout=60.0)
    out = (result.stdout or "") + (result.stderr or "")
    return result.exit_code, out.strip()


async def _run() -> dict[str, object]:
    project = _require("GOOGLE_CLOUD_PROJECT")
    agent_instance = _require("AGENT_ENGINE_RESOURCE_NAME")
    image_uri = _require("LHA_RUNTIME_IMAGE")
    caller_sa = _require("LHA_SANDBOX_CALLER_SA")
    stamp = int(time.time())

    client = vertexai.Client(
        project=project,
        location=SANDBOX_LOCATION,
        http_options={"api_version": "v1beta1"},
    )
    snapshots = client.agent_engines.sandboxes.snapshots

    result: dict[str, object] = {
        "source_image_version": runtime_image_version(image_uri),
        "home_captured": None,
        "exec_bit": None,
        "symlinks": None,
        "snapshot_latency_s": None,
        "restore_latency_s": None,
        "restore_usable": None,
    }

    src_provisioned = None
    restored_provisioned = None
    snapshot_name = None
    src_env = None
    restored_env = None
    try:
        _log("provisioning source sandbox …")
        template = ensure_template(
            client, agent_instance_name=agent_instance, image_uri=image_uri
        )
        src_provisioned = provision_sandbox(
            client,
            agent_instance_name=agent_instance,
            owner="probe",
            template_name=template,
            display_name=f"lha-probe-{stamp}",
            ttl=PROBE_TTL,
        )
        src_env = _build_env(client, src_provisioned, caller_sa)
        await src_env.initialize()
        _log(f"source ready: {src_provisioned.name}")

        _log("planting markers a zip migration would lose …")
        plant = (
            "mkdir -p ~/.local/bin && "
            "printf '#!/bin/sh\\necho probe-v1\\n' > ~/.local/bin/probe-cli && "
            "chmod +x ~/.local/bin/probe-cli && "
            "ln -sf probe-cli ~/.local/bin/probe-link && "
            "echo home-marker > ~/.probe-home-marker"
        )
        code, out = await _exec(src_env, plant)
        if code != 0:
            _log(f"WARNING: planting markers exited {code}: {out}")

        _log("creating snapshot …")
        t0 = time.time()
        snap_op = snapshots.create(
            source_sandbox_environment_name=src_provisioned.name,
            config={
                "display_name": f"lha-probe-snap-{stamp}",
                "ttl": _normalize_ttl(SNAPSHOT_TTL),
                "wait_for_completion": True,
            },
        )
        result["snapshot_latency_s"] = round(time.time() - t0, 1)
        snapshot_name = getattr(
            getattr(snap_op, "response", None), "name", None
        )
        if not snapshot_name:
            raise RuntimeError(f"snapshot create returned no name: {snap_op!r}")
        _log(f"snapshot: {snapshot_name} ({result['snapshot_latency_s']}s)")

        _log("restoring a new sandbox from the snapshot …")
        t0 = time.time()
        restore_op = client.agent_engines.sandboxes.create(
            name=agent_instance,
            config={
                "owner": "probe",
                "display_name": f"lha-probe-restore-{stamp}",
                "sandbox_environment_snapshot": snapshot_name,
                "ttl": _normalize_ttl(PROBE_TTL),
                "wait_for_completion": True,
            },
        )
        result["restore_latency_s"] = round(time.time() - t0, 1)
        response = getattr(restore_op, "response", None)
        conn = getattr(response, "connection_info", None)
        restored_provisioned = ProvisionedSandbox(
            name=response.name,
            lb_host=conn.load_balancer_hostname,
            routing_token=conn.routing_token,
        )
        restored_env = _build_env(client, restored_provisioned, caller_sa)
        await restored_env.initialize()
        result["restore_usable"] = True
        _log(
            f"restored: {restored_provisioned.name} ({result['restore_latency_s']}s)"
        )

        _log("verifying captured state on the restored sandbox …")
        code, out = await _exec(restored_env, "~/.local/bin/probe-cli")
        result["exec_bit"] = code == 0 and out == "probe-v1"
        code, out = await _exec(
            restored_env, "test -L ~/.local/bin/probe-link && echo symlink"
        )
        result["symlinks"] = out == "symlink"
        code, out = await _exec(restored_env, "cat ~/.probe-home-marker")
        result["home_captured"] = out == "home-marker"
    finally:
        _log("cleaning up …")
        if restored_env is not None:
            await _close(restored_env)
        if src_env is not None:
            await _close(src_env)
        if restored_provisioned is not None:
            _safe_delete(client, restored_provisioned.name)
        if src_provisioned is not None:
            _safe_delete(client, src_provisioned.name)
        if snapshot_name is not None:
            _safe_delete_snapshot(snapshots, snapshot_name)

    return result


async def _close(env: SandboxEnvironment) -> None:
    try:
        await env.close()
    except Exception as exc:  # cleanup is best-effort
        _log(f"close failed: {exc}")


def _safe_delete(client: object, name: str) -> None:
    try:
        delete_sandbox(client, sandbox_name=name)
        _log(f"deleted sandbox {name}")
    except Exception as exc:
        _log(f"delete sandbox {name} failed: {exc}")


def _safe_delete_snapshot(snapshots: object, name: str) -> None:
    try:
        snapshots.delete(name=name)
        _log(f"deleted snapshot {name}")
    except Exception as exc:
        _log(f"delete snapshot {name} failed: {exc}")


def main() -> int:
    result = asyncio.run(_run())
    json.dump(result, sys.stdout, indent=2)
    sys.stdout.write("\n")
    ok = bool(result.get("home_captured") and result.get("exec_bit"))
    _log("PASS" if ok else "FAIL — review the result above")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
