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

"""Utilities to locate, read and apply AQuA's Terraform roots from a project.

Allows deployment wrappers and CLI tools to resolve infrastructure attributes
such as GCS bucket names directly from disk, and to run a root against state
kept outside the replaceable vendored tree, using standard library only.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

# Project directory for persistent AQuA files (Terraform state, deployment metadata).
# Kept outside `extensions/aqua/`, which is overwritten on extension updates.
STATE_DIR_NAME = ".aqua"

# Identifiers for AQuA's Terraform roots, used to name state files.
BOOTSTRAP_KEY = "bootstrap"
DEPLOYMENT_KEY = "single-project"

# The root holding one attached agent's triggers, relative to the repository
# root, and the prefix of its per-agent state key. One state per agent is what
# lets `detach --apply` destroy one agent's triggers and leave the others.
ATTACH_ROOT = Path("terraform") / "examples" / "attach"
ATTACH_KEY_PREFIX = "attach-"

# The output of `ATTACH_ROOT` naming the AQuA instance it was applied against.
# Destroying the root needs every variable it requires, and taking them from
# here wires the destroy to that instance and no other.
ATTACH_INSTANCE_OUTPUT = "instance"


def build_state_path(state_root: Path, state_key: str) -> Path:
    """Returns the path to the Terraform state file for a given root.

    Args:
        state_root: Directory containing state files (usually `.aqua`).
        state_key: Identifier of the Terraform root (e.g. `single-project`).

    Returns:
        Path to the corresponding `.tfstate` file.
    """
    return state_root / f"{state_key}.tfstate"


def read_state_outputs(state_root: Path, state_key: str) -> dict[str, Any]:
    """Reads root-level Terraform outputs directly from the state file.

    Parses state JSON without executing `terraform output`, avoiding subprocess
    overhead and dependency on the original configuration files. Returns an empty
    mapping if the state file is absent or invalid.

    Args:
        state_root: Directory containing state files.
        state_key: Identifier of the Terraform root.

    Returns:
        Mapping of output names to their unwrapped values.
    """
    path = build_state_path(state_root, state_key)
    if not path.is_file():
        return {}
    try:
        outputs = (
            json.loads(path.read_text(encoding="utf-8")).get("outputs") or {}
        )
    except (AttributeError, OSError, ValueError):
        # ValueError covers both UnicodeDecodeError and JSONDecodeError.
        return {}
    if not isinstance(outputs, Mapping):
        return {}
    return {
        name: entry.get("value")
        for name, entry in outputs.items()
        if isinstance(entry, Mapping)
    }


def build_attach_key(agent_name: str) -> str:
    """Builds the state key of the attach root applied for one agent.

    Args:
        agent_name: Attached agent, as ADK names it.

    Returns:
        The state key, as `attach-<agent_name>`.
    """
    return f"{ATTACH_KEY_PREFIX}{agent_name}"


def list_attached_agents(state_root: Path) -> list[str]:
    """Lists the agents whose triggers have attach state in ``state_root``.

    Args:
        state_root: Directory containing state files.

    Returns:
        Agent names, sorted.
    """
    suffix = build_state_path(Path(), "").name
    return sorted(
        path.name[len(ATTACH_KEY_PREFIX) : -len(suffix)]
        for path in state_root.glob(f"{ATTACH_KEY_PREFIX}*{suffix}")
        if path.is_file()
    )


def run_terraform(
    root: Path,
    *,
    state_key: str,
    state_root: Path,
    apply: bool,
    tf_vars: Mapping[str, str],
    destroy: bool = False,
) -> None:
    """Executes Terraform lifecycle steps using isolated state storage.

    Execution sequence:
    1. Verify `terraform` is installed on PATH.
    2. Initialize backend pointing to relocated state file in ``state_root``.
    3. Plan or apply/destroy configuration with provided variables.

    Args:
        root: Directory containing Terraform configuration.
        state_key: State identifier key (e.g. `bootstrap` or `single-project`).
        state_root: Directory storing relocated state files.
        apply: If True, applies or destroys; if False, plans.
        tf_vars: Variable key-value mapping passed via `-var`.
        destroy: If True, executes destroy action instead of apply.

    Raises:
        SystemExit: If Terraform is missing or a command exits non-zero.
    """
    if not shutil.which("terraform"):
        print(
            "AQuA: `terraform` is not on PATH, and AQuA is provisioned with it.",
            file=sys.stderr,
            flush=True,
        )
        raise SystemExit(1)

    data_dir = state_root / ".terraform" / state_key
    data_dir.mkdir(parents=True, exist_ok=True)
    state = build_state_path(state_root, state_key)
    # TF_IN_AUTOMATION drops the "run terraform apply next" advice, which names
    # a command the user is not the one running.
    env = {
        **os.environ,
        "TF_DATA_DIR": str(data_dir),
        "TF_IN_AUTOMATION": "1",
    }

    # -input=false everywhere: a Terraform prompt in a wrapped run is
    # indistinguishable from a hang, whereas this exits naming the variable.
    init = [
        "terraform",
        "init",
        "-input=false",
        f"-backend-config=path={state}",
    ]
    _run_or_exit(init, cwd=root, env=env)

    if destroy:
        action = (
            ["terraform", "destroy", "-auto-approve"]
            if apply
            else ["terraform", "plan", "-destroy"]
        )
    else:
        action = (
            ["terraform", "apply", "-auto-approve"]
            if apply
            else ["terraform", "plan"]
        )
    action.append("-input=false")
    for key, value in tf_vars.items():
        action.extend(["-var", f"{key}={value}"])
    _run_or_exit(action, cwd=root, env=env)


def _run_or_exit(argv: list[str], *, cwd: Path, env: Mapping[str, str]) -> None:
    """Runs a command with no shell, exiting with its code if it fails.

    Args:
        argv: Command-line arguments vector.
        cwd: Working directory for the process.
        env: Complete environment for the process.

    Raises:
        SystemExit: If the command exits non-zero.
    """
    result = subprocess.run(  # noqa: S603 - explicit argument list without shell execution
        argv, cwd=str(cwd), env=dict(env), check=False
    )
    if result.returncode != 0:
        raise SystemExit(result.returncode)
