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

"""Process environment + `.env` persistence for credentials and settings."""

from __future__ import annotations

import logging
import os
import re
from pathlib import Path

from dotenv import dotenv_values, load_dotenv

logger = logging.getLogger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DOTENV_PATH = PROJECT_ROOT / ".env"
DEFAULT_DOTENV_EXAMPLE_PATH = PROJECT_ROOT / ".env.example"
ENV_KEY_PATTERN = re.compile(r"^[A-Z_][A-Z0-9_]*$")
# Escapes python-dotenv decodes inside double quotes, so every value stays on one line.
_DOTENV_ESCAPES = str.maketrans(
    {"\\": "\\\\", '"': '\\"', "\n": "\\n", "\r": "\\r"}
)


def dotenv_path() -> Path:
    custom = os.getenv("VOICE_AGENT_DOTENV_PATH")
    return Path(custom).resolve() if custom else DEFAULT_DOTENV_PATH


def load_dotenv_defaults() -> None:
    if os.getenv("VOICE_AGENT_LOAD_DOTENV") == "0":
        os.environ.update(ADK_DISABLE_LOAD_DOTENV="1")
        return
    path = dotenv_path()
    if path.exists():
        load_dotenv(path, override=False)
    if not os.getenv("VOICE_AGENT_DOTENV_PATH"):
        load_example_defaults(DEFAULT_DOTENV_EXAMPLE_PATH)


def load_example_defaults(example: Path) -> None:
    """Fill variables still unset from `.env.example`, the single home of defaults.

    Runs under `.env`, so a `.env` written before a key was added still gets its
    default. Blank values and `<TODO: ...>` placeholders mean "unset" and are skipped.
    """
    if not example.exists():
        return
    for key, value in dotenv_values(example).items():
        if not value or (value.startswith("<TODO") and value.endswith(">")):
            continue
        if key not in os.environ:
            os.environ[key] = value


def read_dotenv() -> dict[str, str]:
    path = dotenv_path()
    if not path.exists():
        return {}
    return {k: v for k, v in dotenv_values(path).items() if v is not None}


def persist_env_vars(updates: dict[str, str | None]) -> bool:
    """Update `os.environ` and `.env` (`None` deletes); return False if `.env` is read-only."""
    if invalid := [
        key for key in updates if not ENV_KEY_PATTERN.fullmatch(key)
    ]:
        raise ValueError(
            f"Invalid environment variable name(s): {', '.join(invalid)}"
        )
    for key, value in updates.items():
        if value is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = value

    path = dotenv_path()
    try:
        lines = path.read_text().splitlines() if path.exists() else []
        _write_private(
            path, "\n".join(_merge_dotenv_lines(lines, updates)) + "\n"
        )
    except OSError as exc:
        logger.warning(
            "Could not write %s (%s); the change lasts until restart.",
            path,
            exc,
        )
        return False
    return True


def _write_private(path: Path, text: str) -> None:
    """Write `text` to `path` with mode 0600, falling back to in-place write on bind mounts."""
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(text)
    try:
        os.replace(tmp, path)
    except OSError:
        tmp.unlink(missing_ok=True)
        path.write_text(text)
        path.chmod(0o600)


def _dotenv_line(key: str, value: str) -> str:
    return f'{key}="{value.translate(_DOTENV_ESCAPES)}"'


def _merge_dotenv_lines(
    lines: list[str], updates: dict[str, str | None]
) -> list[str]:
    merged: list[str] = []
    seen: set[str] = set()
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            merged.append(line)
            continue
        key = stripped.removeprefix("export ").split("=", 1)[0].strip()
        if key not in updates:
            merged.append(line)
            continue
        seen.add(key)
        if (value := updates[key]) is not None:
            merged.append(_dotenv_line(key, value))
    merged.extend(
        _dotenv_line(key, value)
        for key, value in updates.items()
        if key not in seen and value is not None
    )
    return merged
