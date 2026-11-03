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

"""Console output and interactive prompts for provisioning scripts."""

from __future__ import annotations

import getpass
import sys

from app.app_utils.env_flags import env_flag

INDENT = "   "


def non_interactive(*, no_input: bool = False) -> bool:
    return no_input or env_flag("NONINTERACTIVE") or env_flag("CI")


def is_interactive(*, no_input: bool = False) -> bool:
    return sys.stdin.isatty() and not non_interactive(no_input=no_input)


def section(icon: str, title: str, note: str = "") -> None:
    print(f"\n{icon} {title}", flush=True)
    if note:
        print(f"{INDENT}{note}", flush=True)


def ok(message: str) -> None:
    print(f"{INDENT}✅ {message}", flush=True)


def skipped(message: str) -> None:
    print(f"{INDENT}⏭️  {message}", flush=True)


def warn(message: str) -> None:
    print(f"{INDENT}⚠️  {message}", flush=True)


def error(message: str) -> None:
    print(f"{INDENT}❌ {message}", flush=True)


def info(message: str) -> None:
    print(f"{INDENT}{message}", flush=True)


def action(number: int, message: str, *details: str) -> None:
    print(f"{INDENT}👉 {number}. {message}", flush=True)
    for detail in details:
        print(f"{INDENT}      {detail}", flush=True)


def ask(question: str, default: str = "", *, no_input: bool = False) -> str:
    if non_interactive(no_input=no_input):
        if default:
            return default
        raise SystemExit(
            f"❌ Cannot prompt for '{question}' with NONINTERACTIVE=1 or --no-input "
            "(pass the CLI flag or set the environment variable)."
        )
    suggestion = f" [{default}]" if default else ""
    try:
        return input(f"{INDENT}❓ {question}{suggestion}: ").strip() or default
    except EOFError:
        return default


def ask_secret(question: str, *, no_input: bool = False) -> str:
    if non_interactive(no_input=no_input):
        raise SystemExit(
            f"❌ Cannot prompt for '{question}' with NONINTERACTIVE=1 or --no-input "
            "(set the environment variable or pass --skip-google)."
        )
    try:
        return getpass.getpass(f"{INDENT}🔑 {question} (hidden): ").strip()
    except EOFError:
        return ""


def confirm(
    question: str, *, default: bool = True, no_input: bool = False
) -> bool:
    if non_interactive(no_input=no_input):
        return False
    hint = "Y/n" if default else "y/N"
    try:
        answer = input(f"{INDENT}❓ {question} [{hint}]: ").strip().lower()
    except EOFError:
        return False
    return default if not answer else answer in {"y", "yes"}
