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

"""Scripted stand-in for `claude` and `agy` CLIs (`fake_agent_cli.py <dialect> <args...>`).

Turn schema in `$FAKE_AGENT_STATE/turns.json` or `scenarios.json`:
  steps: list of {"say": str} | {"run": shell} | {"write": {path: text}} | {"sleep": s}
         | {"hang": true} | {"ask": {"action_class": str, "command": str}}
         | {"finding": str} | {"question": str} | {"save_to_drive": {"path": str, "title"?: str}}
  keeps_working, result, is_error, interrupted, exit_code, resume_fails, no_result, when.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
import time
import urllib.request
import uuid
from typing import Any

INTERRUPTED = "The stream was interrupted. Please continue the task."
HANG_S = 3600


class FakeCli:
    def __init__(self, dialect: str, args: list[str], state: str) -> None:
        self.dialect, self.args, self.state = dialect, args, state
        self.step_index = 0
        self.prompt = ""

    def opt(self, name: str) -> str | None:
        return (
            self.args[self.args.index(name) + 1] if name in self.args else None
        )

    def load(self, name: str, default: Any) -> Any:
        path = os.path.join(self.state, name)
        if not os.path.exists(path):
            return default
        with open(path) as f:
            return json.load(f)

    def append(self, name: str, record: dict[str, Any]) -> None:
        with open(os.path.join(self.state, name), "a") as f:
            f.write(json.dumps(record) + "\n")

    def next_turn(self) -> dict[str, Any]:
        # `code/<repo>` uses that repo's scenario; any other folder uses "workspace".
        repo = re.search(r"/code/([^/]+)", os.getcwd())
        key = repo.group(1) if repo else "workspace"
        scenario = self.load("scenarios.json", {}).get(key) if key else None
        if scenario is None:
            turns, counter = self.load("turns.json", []), "counter"
        else:
            clone = os.environ.get("VOICE_AGENT_THREAD_CLONE") or os.getcwd()
            turns = scenario
            counter = "counter-" + hashlib.sha1(clone.encode()).hexdigest()[:12]
        path = os.path.join(self.state, counter)
        n = 0
        if os.path.exists(path):
            with open(path) as f:
                n = int(f.read())
        with open(path, "w") as f:
            f.write(str(n + 1))
        return turns[n] if n < len(turns) else {}

    def emit(self, obj: dict[str, Any]) -> None:
        print(json.dumps(obj), flush=True)

    def agy_step(self, state: str, step_type: str, **fields: Any) -> None:
        self.emit(
            {
                "event": "step_update",
                "step_update": {
                    "step_index": self.step_index,
                    "state": state,
                    "step_type": step_type,
                    **fields,
                },
            }
        )

    def say(self, text: str) -> None:
        if self.dialect == "claude":
            content = [{"type": "text", "text": text}]
            self.emit({"type": "assistant", "message": {"content": content}})
            return
        # agy streams text in fragments and closes the step with an empty DONE update.
        self.step_index += 1
        half = len(text) // 2
        self.agy_step("ACTIVE", "agent_response", text_delta=text[:half])
        self.agy_step("ACTIVE", "agent_response", text_delta=text[half:])
        self.agy_step("DONE", "agent_response")

    def tool(self, kind: str, target: str) -> None:
        if self.dialect == "claude":
            name, args = {
                "write": ("Write", {"file_path": target}),
                "run": ("Bash", {"command": target}),
            }[kind]
            content = [{"type": "tool_use", "name": name, "input": args}]
            self.emit({"type": "assistant", "message": {"content": content}})
            return
        self.step_index += 1
        name, params = {
            "write": ("write_to_file", {"TargetFile": target}),
            "run": ("run_command", {"CommandLine": target}),
        }[kind]
        for state in ("ACTIVE", "DONE"):
            info = {"name": name, "parameters": params}
            self.agy_step(state, "tool", tool_name=name, tool_info=info)

    def run(self) -> int:
        turn = self.next_turn()
        prompt = self.opt("-p") or ""
        for needle, override in turn.get("when", {}).items():
            if needle.lower() in prompt.lower():
                turn = {**turn, **override}
        self.prompt = prompt
        resume = self.opt("--resume") or self.opt("--conversation")
        session = (
            resume or self.opt("--session-id") or f"conv-{uuid.uuid4().hex[:8]}"
        )
        self.append(
            "calls.jsonl",
            {
                "dialect": self.dialect,
                "prompt": prompt,
                "resume": resume,
                "session": session,
                "cwd": os.getcwd(),
                "clone": os.environ.get("VOICE_AGENT_THREAD_CLONE"),
            },
        )
        if resume and turn.get("resume_fails"):
            sys.stderr.write(
                f"No conversation found with session ID: {resume}\n"
            )
            return 1

        if self.dialect == "claude":
            self.emit(
                {"type": "system", "subtype": "init", "session_id": session}
            )
        else:
            self.emit({"event": "init", "conversation_id": session})
        for step in turn.get("steps", []):
            self.step(step)
            if "question" in step and not turn.get("keeps_working"):
                turn = {
                    **turn,
                    "result": turn.get("result") or step["question"],
                }
                break
        if not turn.get("no_result"):
            self.result(turn, session)
        return int(turn.get("exit_code", 0))

    def step(self, step: dict[str, Any]) -> None:
        if "say" in step:
            self.say(step["say"])
        elif "sleep" in step:
            time.sleep(step["sleep"])
        elif "hang" in step:
            time.sleep(HANG_S)
        elif "finding" in step:
            self.call_tool("update_user", {"text": step["finding"]})
        elif "question" in step:
            self.say(step["question"])
        elif "save_to_drive" in step:
            self.call_tool("save_to_drive", step["save_to_drive"])
        elif "ask" in step:
            ask = step["ask"]
            if f"Approved: `{ask['command']}`" not in self.prompt:
                self.request_permission(
                    {
                        "action_class": ask["action_class"],
                        "command": ask["command"],
                        "key": hashlib.sha256(
                            ask["command"].encode("utf-8")
                        ).hexdigest(),
                        "kind": "tool",
                    }
                )
        elif "write" in step:
            for rel, text in step["write"].items():
                os.makedirs(
                    os.path.dirname(os.path.abspath(rel)), exist_ok=True
                )
                with open(rel, "w") as f:
                    f.write(text)
                self.tool("write", os.path.abspath(rel))
        elif "run" in step:
            raw_cmd = step["run"]
            self.tool("run", raw_cmd)
            gated = _classify_gated_command(raw_cmd)
            if gated is not None:
                action_class, argv = gated
                cmd_display = shlex.join(argv)
                if f"Approved: `{cmd_display}`" not in self.prompt:
                    key = hashlib.sha256(
                        json.dumps(argv).encode("utf-8")
                    ).hexdigest()
                    self.append(
                        "commands.jsonl",
                        {
                            "command": raw_cmd,
                            "exit_code": 126,
                            "stderr": "Needs the user's approval",
                        },
                    )
                    self.request_permission(
                        {
                            "action_class": action_class,
                            "command": cmd_display,
                            "key": key,
                        }
                    )
                    return
            proc = subprocess.run(  # noqa: S602 - scripted fake CLI runs the test scenario's shell step
                raw_cmd,
                shell=True,
                capture_output=True,
                text=True,
                check=False,
            )
            self.append(
                "commands.jsonl",
                {
                    "command": raw_cmd,
                    "exit_code": proc.returncode,
                    "stderr": proc.stderr,
                },
            )

    def call_tool(self, name: str, arguments: dict[str, Any]) -> None:
        body = json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": name, "arguments": arguments},
            }
        ).encode("utf-8")
        request = urllib.request.Request(
            os.environ["VOICE_AGENT_MCP_URL"],
            data=body,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {os.environ['VOICE_AGENT_MCP_TOKEN']}",
            },
        )
        with urllib.request.urlopen(request, timeout=30) as response:
            reply = json.load(response)
        self.append(
            "mcp_calls.jsonl",
            {"tool": name, "arguments": arguments, "reply": reply},
        )

    def request_permission(self, perm: dict[str, Any]) -> None:
        if self.dialect == "claude":
            self.emit({"type": "permission_request", "permission": perm})
        else:
            self.emit({"event": "permission_request", "permission": perm})
        self.say("That needs your approval. Waiting.")
        time.sleep(HANG_S)

    def result(self, turn: dict[str, Any], session: str) -> None:
        text = turn.get("result") or "Done. Made the change and ran the tests."
        is_error = bool(turn.get("is_error"))
        if self.dialect == "claude":
            self.emit(
                {
                    "type": "result",
                    "subtype": "error_during_execution"
                    if is_error
                    else "success",
                    "is_error": is_error,
                    "result": text,
                    "session_id": session,
                }
            )
            return
        self.say(text)
        error = (
            INTERRUPTED
            if turn.get("interrupted")
            else (text if is_error else None)
        )
        self.emit(
            {
                "event": "result",
                "result": {
                    "status": "ERROR" if error else "SUCCESS",
                    "response": text,
                    "error": error,
                    "conversation_id": session,
                },
            }
        )


def _classify_gated_command(raw_cmd: str) -> tuple[str, list[str]] | None:
    cleaned = raw_cmd.split("||", 1)[0].strip()
    if "&&" in cleaned or ";" in cleaned:
        return None
    try:
        argv = shlex.split(cleaned)
    except ValueError:
        return None
    if len(argv) < 2 or argv[0] != "git":
        return None
    if argv[1] == "push":
        if "--force" in argv or "-f" in argv or "--force-with-lease" in argv:
            return "force_push", argv
        if any(arg in ("main", "master") for arg in argv[2:]):
            return "merge", argv
    if argv[1] == "reset" and "--hard" in argv:
        return "destructive", argv
    return None


if __name__ == "__main__":
    cli = FakeCli(sys.argv[1], sys.argv[2:], os.environ["FAKE_AGENT_STATE"])
    sys.exit(cli.run())
