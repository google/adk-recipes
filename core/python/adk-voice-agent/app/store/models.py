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

"""SQLAlchemy models for the durable thread ledger and user settings."""

from __future__ import annotations

import datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy.types import JSON

JSON_TYPE = JSON().with_variant(JSONB, "postgresql")


class Base(DeclarativeBase):
    pass


class ThreadRecord(Base):
    """Thread row holding `ThreadHandle.to_dict()` in `data` and indexed query columns."""

    __tablename__ = "threads"
    __table_args__ = (
        Index("ix_threads_owner_updated", "principal_id", "updated_at"),
    )

    thread_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    principal_id: Mapped[str] = mapped_column(
        String(128), index=True, nullable=False
    )
    state: Mapped[str] = mapped_column(String(32), index=True, nullable=False)
    dismissed: Mapped[bool] = mapped_column(default=False, nullable=False)
    data: Mapped[dict[str, Any]] = mapped_column(JSON_TYPE, nullable=False)
    title: Mapped[str] = mapped_column(
        String(200), nullable=False, default="", server_default=""
    )
    goal: Mapped[str] = mapped_column(
        Text, nullable=False, default="", server_default=""
    )
    ended_at: Mapped[float | None] = mapped_column(Float, nullable=True)
    activity_at: Mapped[float | None] = mapped_column(
        Float, nullable=True, index=True
    )
    origin_session_id: Mapped[str | None] = mapped_column(
        String(128), nullable=True, index=True
    )
    created_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
        index=True,
    )
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


def thread_columns(snapshot: dict[str, Any]) -> dict[str, Any]:
    from app.threads.titles import thread_title

    goal = str(snapshot.get("goal") or "")
    ended_at = snapshot.get("ended_at")
    activity_at = (
        ended_at or snapshot.get("updated_at") or snapshot.get("created_at")
    )
    return {
        "state": snapshot["state"],
        "dismissed": bool(snapshot.get("dismissed")),
        "title": thread_title(snapshot.get("title"), goal)[:200],
        "goal": goal,
        "ended_at": float(ended_at) if ended_at is not None else None,
        "activity_at": float(activity_at or 0.0),
        "origin_session_id": snapshot.get("origin_session_id") or None,
    }


class ThreadEventRecord(Base):
    """Append-only thread event log; `id` is the global monotonic briefing cursor."""

    __tablename__ = "thread_events"
    __table_args__ = (Index("ix_thread_events_thread_seq", "thread_id", "seq"),)

    id: Mapped[int] = mapped_column(
        BigInteger().with_variant(Integer, "sqlite"),
        primary_key=True,
        autoincrement=True,
    )
    thread_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("threads.thread_id", ondelete="CASCADE"),
        index=True,
        nullable=False,
    )
    seq: Mapped[int] = mapped_column(Integer, nullable=False)
    iteration: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    message: Mapped[str] = mapped_column(Text, nullable=False)
    data: Mapped[dict[str, Any] | None] = mapped_column(
        JSON_TYPE, nullable=True
    )
    ts: Mapped[float] = mapped_column(nullable=False)


class CursorRecord(Base):
    __tablename__ = "consumer_cursors"

    principal_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    last_seen_event_id: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0
    )
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class UserSettingRecord(Base):
    __tablename__ = "user_settings"

    principal_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    value_json: Mapped[Any] = mapped_column(JSON_TYPE, nullable=True)
    updated_at: Mapped[datetime.datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
