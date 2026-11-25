# SPDX-License-Identifier: Apache-2.0
#
# Copied from Harbor (https://github.com/harbor-framework/harbor), release
# v0.25.0, file src/harbor/models/trajectories/__init__.py. Copyright the Harbor
# authors, licensed under the Apache License, Version 2.0; see LICENSE in this
# directory. Modified for AQuA: imports point at `aqua_atif.models`, and each
# other change is marked with an "AQuA change" comment (see `__init__.py`).

"""Pydantic models for Agent Trajectory Interchange Format (ATIF).

This module provides Pydantic models for validating and constructing
trajectory data following the ATIF specification (RFC 0001).

AQuA changes to Harbor's v0.25.0 models, each marked "AQuA change N" in the
code:

1. The 12 model configs allow unknown fields instead of forbidding them.
2. `Trajectory.schema_version` is a string; `aqua_atif.validation` checks its
   range.
3. `aqua_atif.validation` walks the validated document and lists every unknown
   field with its path (no edit to these files).
4. A content part of an unknown type is kept: `ContentPart.type` and the media
   type fields take any string, the source model is chosen from the part's
   type, and the media part validator skips unknown types. A step `source`
   other than `system`, `user` or `agent` stays invalid.
5. A subagent reference with neither `trajectory_id` nor `trajectory_path` is
   kept, and `aqua_atif.markers.find_missing_markers` marks it.

Besides these, the copy differs from Harbor only in the file headers, the
imports, which point at `aqua_atif.models` and are sorted by ruff, ruff's line
wrapping, and one `noqa`. `tests/fixtures/atif/harbor_v0.25.0_models.diff`
holds the whole difference, and `tests/test_aqua_atif_models.py` checks that
reversing it gives Harbor's files byte for byte. To vendor a newer release:

1. Copy its `src/harbor/models/trajectories/*.py` here.
2. Apply the diff with `patch -p1 -d src/aqua_atif/models`, and resolve what
   no longer applies.
3. Run `ruff check --fix` and `ruff format` on the copy.
4. Regenerate the diff with `diff -u --label a/<file> --label b/<file>`, update
   the release and the upstream hashes in the test, and run it.
"""

from aqua_atif.models.agent import Agent
from aqua_atif.models.content import (
    AudioMediaType,
    AudioSource,
    ContentPart,
    ImageMediaType,
    ImageSource,
)
from aqua_atif.models.final_metrics import FinalMetrics
from aqua_atif.models.metrics import Metrics
from aqua_atif.models.observation import Observation
from aqua_atif.models.observation_result import ObservationResult
from aqua_atif.models.step import Step
from aqua_atif.models.subagent_trajectory_ref import SubagentTrajectoryRef
from aqua_atif.models.tool_call import ToolCall
from aqua_atif.models.trajectory import Trajectory

__all__ = [
    "Agent",
    "AudioMediaType",
    "AudioSource",
    "ContentPart",
    "FinalMetrics",
    "ImageMediaType",
    "ImageSource",
    "Metrics",
    "Observation",
    "ObservationResult",
    "Step",
    "SubagentTrajectoryRef",
    "ToolCall",
    "Trajectory",
]
