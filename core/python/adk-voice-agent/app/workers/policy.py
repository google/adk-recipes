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

"""Permission policy and run-budget configuration (`config/policy.yaml`)."""

from __future__ import annotations

import base64
import shlex
from dataclasses import dataclass, field
from pathlib import Path

from app.workers.environment import Environment

_CONFIG_FILE = Path(__file__).resolve().parents[2] / "config" / "policy.yaml"
PERMISSION_DECISIONS = ("deny",)


@dataclass(frozen=True)
class Policy:
    permissions: dict[str, list[str]] = field(default_factory=dict)
    mcp_gates: list[str] = field(default_factory=list)
    stall_timeout_s: float = 5 * 60


def _read_yaml(path: Path) -> dict:
    import yaml

    if not path.exists():
        return {}
    data = yaml.safe_load(path.read_text()) or {}
    if not isinstance(data, dict):
        raise ValueError(f"{path} must contain a mapping")
    return data


def _union(*lists: object) -> list[str]:
    out: list[str] = []
    for items in lists:
        for item in items if isinstance(items, list) else []:
            if isinstance(item, str) and item.strip() and item not in out:
                out.append(item.strip())
    return out


def load_policy() -> Policy:
    from app.store.engine import get_default_state_dir

    base = _read_yaml(_CONFIG_FILE)
    user = _read_yaml(get_default_state_dir() / "policy.yaml")
    base_perms = base.get("permissions") or {}
    user_perms = user.get("permissions") or {}
    permissions = {
        d: _union(base_perms.get(d), user_perms.get(d))
        for d in PERMISSION_DECISIONS
    }
    mcp_gates = _union(
        (base.get("mcp_gates") or {}).get("ask"),
        (user.get("mcp_gates") or {}).get("ask"),
    )
    budget = {
        **(base.get("run_budget") or {}),
        **(user.get("run_budget") or {}),
    }
    return Policy(
        permissions=permissions,
        mcp_gates=mcp_gates,
        stall_timeout_s=float(budget.get("stall_timeout_min", 5)) * 60,
    )


async def read_clone_file(
    env: Environment,
    clone: str,
    relative: str,
    *,
    max_bytes: int,
    allow_hidden: bool = True,
) -> bytes:
    """Read a file inside `clone`, rejecting symlinks/paths outside it (and dot-dirs when `allow_hidden=False`)."""
    if (
        not relative
        or relative.startswith(("/", "~"))
        or ".." in Path(relative).parts
        or "\0" in relative
    ):
        raise ValueError(
            f"{relative!r} is not a path inside the thread's clone."
        )
    q = shlex.quote
    path = q(f"{clone}/{relative}")
    root = q(clone)
    hidden = (
        ""
        if allow_hidden
        else 'case "${real#"$base"/}" in .*|*/.*) exit 5;; esac; '
    )
    result = await env.exec(
        f"real=$(realpath {path} 2>/dev/null) && base=$(realpath {root}) && "
        f'case "$real" in "$base"/*) ;; *) exit 4;; esac && [ -f "$real" ] || exit 4; '
        f"{hidden}"
        f'[ "$(wc -c < "$real")" -le {max_bytes} ] || exit 3; base64 < "$real"',
        timeout_s=15.0,
    )
    if result.error:
        raise RuntimeError(result.error)
    if result.exit_code == 4:
        raise ValueError(f"{relative} is not a file in the thread's clone.")
    if result.exit_code == 5:
        raise ValueError(f"{relative} leads into a hidden directory.")
    if result.exit_code == 3:
        raise ValueError(f"{relative} is larger than {max_bytes} bytes.")
    if result.exit_code != 0:
        raise ValueError(
            f"Could not read {relative}: {result.describe_failure()}"
        )
    return base64.b64decode(result.stdout)
