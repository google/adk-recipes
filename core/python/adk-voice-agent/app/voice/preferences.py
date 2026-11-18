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

"""User preferences (timezone, voice persona, voice mode, saving to Drive) and the
current-time tool.

Each user's choices are per-user settings (`app/user_settings.py`); `USER_TIMEZONE`,
`LIVE_VOICE_NAME`, and `VOICE_MODE` are the deployment defaults for users who haven't
chosen.
"""

from __future__ import annotations

import os
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from app.user_settings import (
    SAVE_TO_DRIVE,
    TIMEZONE,
    VOICE_MODE,
    VOICE_NAME,
    save_user_settings,
    user_settings,
)

DEFAULT_TIMEZONE = "America/Chicago"

AVAILABLE_VOICES = ["Aoede", "Puck", "Kore", "Charon", "Fenrir", "Zephyr"]

#: One native-audio Live model, or speech-to-text, a text model, and text-to-speech
#: (`app/voice/voice_mode.py`).
NATIVE = "native"
CASCADED = "cascaded"
VOICE_MODES = [NATIVE, CASCADED]

COMMON_TIMEZONES = [
    "America/Los_Angeles",
    "America/Denver",
    "America/Chicago",
    "America/New_York",
    "America/Toronto",
    "America/Sao_Paulo",
    "UTC",
    "Europe/London",
    "Europe/Paris",
    "Europe/Berlin",
    "Asia/Dubai",
    "Asia/Kolkata",
    "Asia/Singapore",
    "Asia/Tokyo",
    "Australia/Sydney",
]


def user_timezone() -> tuple[str, ZoneInfo]:
    """The acting user's IANA timezone, else `USER_TIMEZONE`, falling back to the default."""
    return resolve_timezone(user_settings().get(TIMEZONE))


def resolve_timezone(*preferred: object) -> tuple[str, ZoneInfo]:
    """The first non-blank of `preferred`, else `USER_TIMEZONE`, else the default.

    An unknown zone name falls back to the default rather than failing.
    """
    candidates = (*preferred, os.getenv("USER_TIMEZONE"))
    name = next(
        (str(c).strip() for c in candidates if c and str(c).strip()),
        DEFAULT_TIMEZONE,
    )
    try:
        return name, ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        return DEFAULT_TIMEZONE, ZoneInfo(DEFAULT_TIMEZONE)


def _utc_offset(now: datetime) -> str:
    raw = now.strftime("%z") or "+0000"
    return f"UTC{raw[:3]}:{raw[3:]}" if len(raw) == 5 else f"UTC{raw}"


def resolve_voice_mode() -> str:
    """The acting user's voice mode, else `VOICE_MODE`, else native.

    An unknown value falls through to the next rather than failing.
    """
    candidates = (user_settings().get(VOICE_MODE), os.getenv("VOICE_MODE"))
    return next(
        (
            mode
            for c in candidates
            if (mode := str(c or "").strip().lower()) in VOICE_MODES
        ),
        NATIVE,
    )


def get_runtime_preferences() -> dict[str, Any]:
    """Return the acting user's timezone, formatted local time, and voice configuration."""
    chosen = user_settings()
    tz_name, tz = user_timezone()
    now = datetime.now(tz)
    tz_abbrev = now.strftime("%Z") or "Local"
    return {
        "timezone": tz_name,
        "tz_abbrev": tz_abbrev,
        "tz_offset": _utc_offset(now),
        "local_time_formatted": now.strftime(
            f"%A, %B %-d, %Y at %-I:%M %p {tz_abbrev}"
        ),
        "local_clock_short": now.strftime(f"%-I:%M %p {tz_abbrev}"),
        "voice_name": (
            str(chosen.get(VOICE_NAME) or "").strip()
            or (os.getenv("LIVE_VOICE_NAME") or "").strip()
            or "Aoede"
        ),
        # native | cascaded; a change takes effect from the next conversation.
        "voice_mode": resolve_voice_mode(),
        # Whether the computer saves deliverables to the user's Drive: on or off.
        "save_to_drive": _save_to_drive(chosen),
        "available_voices": AVAILABLE_VOICES,
        "available_voice_modes": VOICE_MODES,
        "common_timezones": COMMON_TIMEZONES,
    }


async def save_preferences(
    *,
    timezone: str | None = None,
    voice_name: str | None = None,
    voice_mode: str | None = None,
    save_to_drive: str | None = None,
) -> dict[str, Any]:
    """Save the acting user's non-blank choices; return their preferences after.

    Raises `ValueError` for a `save_to_drive` other than on or off, or a `voice_mode`
    other than native or cascaded.
    """
    from app.threads.documents import SAVE_CHOICES

    if (
        save_to_drive is not None
        and save_to_drive.strip().lower() not in SAVE_CHOICES
    ):
        raise ValueError(
            f"save_to_drive must be one of {', '.join(SAVE_CHOICES)}."
        )
    if voice_mode is not None and voice_mode.strip().lower() not in VOICE_MODES:
        raise ValueError(f"voice_mode must be one of {', '.join(VOICE_MODES)}.")
    updates = {
        key: value.strip()
        for key, value in (
            (TIMEZONE, timezone),
            (VOICE_NAME, voice_name),
            (VOICE_MODE, voice_mode and voice_mode.lower()),
            (SAVE_TO_DRIVE, save_to_drive and save_to_drive.lower()),
        )
        if value is not None and value.strip()
    }
    if updates:
        await save_user_settings(updates)
    return get_runtime_preferences()


def _save_to_drive(chosen: dict[str, Any]) -> str:
    from app.threads.documents import save_to_drive_preference

    return save_to_drive_preference(chosen)


async def get_current_time_and_timezone() -> str:
    """Return the user's current local date, time, and IANA timezone."""
    prefs = get_runtime_preferences()
    return (
        f"Current local time is {prefs['local_time_formatted']} "
        f"(timezone {prefs['timezone']}, {prefs['tz_offset']})."
    )
