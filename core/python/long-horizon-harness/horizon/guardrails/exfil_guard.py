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

"""``before_tool_callback`` that blocks sensitive-data exfiltration.

Posture (see ``exfil_config`` for the knobs):

* Any tool whose serialized args carry recognizable secret material
  (keys/tokens/private-key blocks) is hard-blocked — refusing to transmit
  a credential is never a false economy.
* For shell commands (any tool with a string ``command`` arg):
    - a network tool that also reads a credential path or a secret env var
      is hard-blocked (the ``cat .env | curl`` shape);
    - a curl/wget *upload* (``--data``/``-T``/…), or any use of a
      transfer/tunnel tool (scp/sftp/rsync/ssh/nc/…), to a host outside the
      allowlist surfaces a confirmation;
    - plain curl/wget downloads (GETs) are never gated.

A matching per-session grant (recorded by ``/grant`` → ``policy_grant``)
bypasses the guard. The durable fix for a recurring destination is the
``allow_hosts`` overlay; ``/grant`` is the one-time, exact-command escape.

This is a heuristic over common command shapes and is the exfiltration
boundary — not exhaustive (a raw socket in a script, an unlisted tool,
base64-wrapped data can evade it).
"""

from __future__ import annotations

import json
from typing import Any

from horizon.environment_context import active_environment
from horizon.guardrails.exfil_config import (
    DEVTCP_RE,
    EXFIL_OVERLAY_FILENAME,
    GIT_EGRESS_RE,
    HTTP_TOOLS_RE,
    TRANSFER_TOOLS_RE,
    UPLOAD_RE,
    ExfilConfig,
    extract_hosts,
    host_allowed,
    load_exfil_config_for_env,
    references_secret,
    scan_secrets,
    targets_metadata,
)
from horizon.guardrails.policy_grants import (
    find_matching_grant,
    find_matching_grant_loose,
)
from horizon.tools import names


def _serialize_args(args: Any) -> str:
    if isinstance(args, str):
        return args
    try:
        return json.dumps(args, ensure_ascii=False, default=str)
    except (TypeError, ValueError):
        return str(args)


def _inspect_command(
    command: str, config: ExfilConfig
) -> dict[str, Any] | None:
    http = HTTP_TOOLS_RE.search(command)
    transfer = TRANSFER_TOOLS_RE.search(command)
    devtcp = DEVTCP_RE.search(command)
    git = GIT_EGRESS_RE.search(command)
    if not (http or transfer or devtcp or git):
        return None
    if (http or transfer or devtcp) and targets_metadata(command):
        # tier "secret" → only an exact /grant clears it, so a re-wrapped retry
        # (cd … && curl …) can't slip the ambient token read past one approval.
        return {
            "reason": "a network command targets the GCP metadata server "
            "(ambient service-account token endpoint)",
            "hard": True,
            "tier": "secret",
        }
    if references_secret(command, config):
        return {
            "reason": "a network command reads a credential path or secret env var",
            "hard": True,
            "tier": "credential",
        }
    # curl/wget are gated only when uploading (GET/downloads pass); transfer
    # tools, raw /dev/tcp writes, and git push/remote are gated on any remote
    # host they name.
    egress = bool(
        (http and UPLOAD_RE.search(command)) or transfer or devtcp or git
    )
    if not egress:
        return None
    unknown = [h for h in extract_hosts(command) if not host_allowed(h, config)]
    if unknown:
        return {
            "reason": f"outbound transfer to non-allowlisted host(s) {unknown}",
            "hard": False,
            "tier": "host",
        }
    return None


def evaluate_exfil(
    tool_name: str, args: Any, config: ExfilConfig
) -> dict[str, Any] | None:
    """Pure verdict: ``{"reason", "hard", "tier"}`` to block, else ``None``.

    ``tier`` is "secret" (literal key/token in args), "credential" (reads a
    credential path/env), or "host" (upload to a non-allowlisted host). It
    governs how a ``/grant`` may clear the block (see ``exfil_guard``).
    """
    label = scan_secrets(_serialize_args(args), config)
    if label is not None:
        return {
            "reason": f"{tool_name or 'tool'} call carries {label}",
            "hard": True,
            "tier": "secret",
        }

    command = args.get("command") if isinstance(args, dict) else None
    if isinstance(command, str) and command:
        return _inspect_command(command, config)

    # process(action="write") drives a backgrounded shell via the `data` arg,
    # not `command` — inspect that payload for the same command-shape exfil.
    if (
        tool_name == names.PROCESS
        and isinstance(args, dict)
        and args.get("action") == "write"
    ):
        data = args.get("data")
        if isinstance(data, str) and data:
            return _inspect_command(data, config)
    return None


def _active_env() -> Any | None:
    try:
        return active_environment()
    except RuntimeError:
        return None


def _block(verdict: dict[str, Any]) -> dict[str, Any]:
    reason = verdict["reason"]
    if verdict["hard"]:
        guidance = (
            "This looks like sensitive-data exfiltration and is blocked. If "
            "this is a false positive the user must explicitly approve it — "
            "ask them to type `/grant <command>`, then retry."
        )
    else:
        guidance = (
            "Outbound destination is not on the allowlist. To allow it "
            f"durably, add the host to {EXFIL_OVERLAY_FILENAME} under "
            '"allow_hosts". For a one-off, if the user approves, have them '
            "type `/grant <command>`, then retry (the grant matches even if "
            "the command is re-wrapped)."
        )
    return {
        "error": f"exfiltration guard: {reason}. {guidance}",
        "confirmation_required": True,
        "exfil_blocked": True,
    }


async def exfil_guard(
    *, tool: Any, args: Any, tool_context: Any
) -> dict[str, Any] | None:
    """Block outbound calls that would leak secrets or hit unknown hosts."""
    tool_name = getattr(tool, "name", "") or ""
    if not tool_name:
        return None

    config = await load_exfil_config_for_env(_active_env())
    verdict = evaluate_exfil(tool_name, args, config)
    if verdict is None:
        return None

    state = getattr(tool_context, "state", None)
    if state is not None:
        if verdict["tier"] == "secret":
            # Literal secret material — only an exact grant clears it, so a
            # loosened wrapper can't smuggle a key past the block.
            if find_matching_grant(state, tool_name, args):
                return None
        # Credential-path / host blocks clear on a loosened grant, so a
        # re-wrapped retry (cd … && curl … && echo) of an approved command
        # still matches what the user granted.
        elif find_matching_grant_loose(state, tool_name, args):
            return None

    return _block(verdict)


__all__ = ["evaluate_exfil", "exfil_guard"]
