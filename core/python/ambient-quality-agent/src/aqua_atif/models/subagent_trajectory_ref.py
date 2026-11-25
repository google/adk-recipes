# SPDX-License-Identifier: Apache-2.0
#
# Copied from Harbor (https://github.com/harbor-framework/harbor), release
# v0.25.0, file src/harbor/models/trajectories/subagent_trajectory_ref.py. Copyright the Harbor
# authors, licensed under the Apache License, Version 2.0; see LICENSE in this
# directory. Modified for AQuA: imports point at `aqua_atif.models`, and each
# other change is marked with an "AQuA change" comment (see `__init__.py`).

"""Subagent trajectory reference model for ATIF trajectories."""

from typing import Any

from pydantic import BaseModel, Field


class SubagentTrajectoryRef(BaseModel):
    """Reference to a delegated subagent trajectory.

    A subagent reference is resolved by one of two mechanisms:

    1. **Embedded form** — set `trajectory_id` to match the
       `Trajectory.trajectory_id` of an entry in the parent's
       `subagent_trajectories` array.
    2. **File-ref form** — set `trajectory_path` to the location
       (file path, S3 URL, etc.) of an external trajectory file.

    These two mechanisms are the only resolution keys. `session_id`, when
    present on the ref, is **informational only**: it records the run
    identity of the delegated subagent for debug / correlation / cross-
    trajectory search purposes, and MUST NOT be used as a matching key
    (it is run-scoped and MAY collide across siblings — see
    `Trajectory.session_id`). A ref therefore MUST set at least one of
    `trajectory_id` or `trajectory_path`; `session_id` alone is not a
    resolvable reference.
    """

    trajectory_id: str | None = Field(
        default=None,
        description=(
            "Canonical identifier of the delegated subagent trajectory. "
            "Matches `Trajectory.trajectory_id` of an entry in the parent's "
            "`subagent_trajectories` array and is the resolution key for "
            "embedded references. Added in ATIF-v1.7 to provide a document-"
            "unique matching key without overloading `session_id`."
        ),
    )
    session_id: str | None = Field(
        default=None,
        description=(
            "Run identity of the delegated subagent trajectory. Informational "
            "only: recorded so consumers can correlate this ref back to the "
            "subagent's run for debug / search / display purposes. Run-scoped "
            "(see `Trajectory.session_id`) and therefore NOT a valid "
            "resolution key — consumers MUST resolve via `trajectory_id` "
            "(embedded) or `trajectory_path` (external file)."
        ),
    )
    trajectory_path: str | None = Field(
        default=None,
        description=(
            "Location of the complete subagent trajectory as an external "
            "file (file path, S3 URL, database reference, etc.). Resolution "
            "key for file-ref references. When both `trajectory_id` and "
            "`trajectory_path` are set, consumers MAY choose either; "
            "typically `trajectory_id` is preferred when the embedded "
            "trajectory is available in-memory."
        ),
    )
    extra: dict[str, Any] | None = Field(
        default=None,
        description="Custom metadata about the subagent execution",
    )

    # AQuA change 1: unknown fields are kept; `aqua_atif.validation` lists them.
    model_config = {"extra": "allow"}

    # AQuA change 5: a reference with neither `trajectory_id` nor
    # `trajectory_path`, valid in ATIF-v1.6, is kept; it never resolves, so
    # `aqua_atif.markers.find_missing_markers` gives it a `trajectory` marker
    # instead of the document being rejected.
