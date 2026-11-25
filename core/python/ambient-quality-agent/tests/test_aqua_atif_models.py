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

"""Tests that the vendored Harbor models keep AQuA's five changes.

Vendoring a newer Harbor release is a copy, the changes applied again, and this
test. It also checks that `aqua_atif` imports nothing from AQuA, so converter
plugins can depend on it.
"""

from __future__ import annotations

import ast
import hashlib
import pathlib
import re
import typing

import aqua_atif
import pytest
from aqua_atif import models
from pydantic import ValidationError

_PACKAGE_DIR = pathlib.Path(aqua_atif.__file__).parent
_MODEL_CLASSES = (
    models.Agent,
    models.AudioSource,
    models.ContentPart,
    models.FinalMetrics,
    models.ImageSource,
    models.Metrics,
    models.Observation,
    models.ObservationResult,
    models.Step,
    models.SubagentTrajectoryRef,
    models.ToolCall,
    models.Trajectory,
)


def _image_part(media_type: str = "image/png") -> dict:
    """Builds an image content part.

    Args:
        media_type: The image's media type.

    Returns:
        The part.
    """
    return {
        "type": "image",
        "source": {"media_type": media_type, "path": "a.png"},
    }


def test_change_1_every_model_allows_unknown_fields():
    assert len(_MODEL_CLASSES) == 12
    for model in _MODEL_CLASSES:
        assert model.model_config.get("extra") == "allow", model.__name__


def test_change_2_schema_version_is_any_string():
    annotation = models.Trajectory.model_fields["schema_version"].annotation
    assert annotation is str
    assert (
        models.Trajectory.model_fields["schema_version"].default == "ATIF-v1.8"
    )


def test_change_4_unknown_part_type_is_kept_with_its_source():
    part = models.ContentPart.model_validate(
        {"type": "video", "source": {"uri": "gs://b/v.mp4"}}
    )
    assert part.type == "video"
    assert part.source == {"uri": "gs://b/v.mp4"}


def test_change_4_media_type_lists_are_open_and_the_source_follows_the_type():
    image = models.ContentPart.model_validate(_image_part("image/svg+xml"))
    assert isinstance(image.source, models.ImageSource)
    audio = models.ContentPart.model_validate(
        {"type": "audio", "source": {"media_type": "audio/mp3", "path": "a"}}
    )
    assert isinstance(audio.source, models.AudioSource)
    assert audio.source.media_type == "audio/mpeg"
    assert typing.get_args(models.ImageMediaType) == (
        "image/jpeg",
        "image/png",
        "image/gif",
        "image/webp",
    )


def test_change_4_known_media_parts_still_need_a_source():
    with pytest.raises(ValidationError, match="'source' field is required"):
        models.ContentPart.model_validate({"type": "image"})
    with pytest.raises(ValidationError, match="'text' field is required"):
        models.ContentPart.model_validate({"type": "text"})


def test_change_4_step_source_stays_closed():
    models.Step.model_validate(
        {"step_id": 1, "source": "agent", "message": "x"}
    )
    with pytest.raises(ValidationError) as raised:
        models.Step.model_validate(
            {"step_id": 1, "source": "tool", "message": "x"}
        )
    assert [error["loc"] for error in raised.value.errors()] == [("source",)]


def test_change_5_session_id_only_reference_is_kept():
    ref = models.SubagentTrajectoryRef.model_validate({"session_id": "s-1"})
    assert ref.trajectory_id is None
    assert ref.trajectory_path is None


def test_every_change_is_marked_in_the_vendored_files():
    text = "".join(
        path.read_text(encoding="utf-8")
        for path in sorted((_PACKAGE_DIR / "models").glob("*.py"))
    )
    for number in (1, 2, 4, 5):
        assert f"AQuA change {number}" in text
    assert text.count("AQuA change 1:") == 12
    assert "harbor.models" not in text
    assert (_PACKAGE_DIR / "models" / "LICENSE").is_file()


def test_package_imports_nothing_from_aqua():
    for path in _PACKAGE_DIR.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            else:
                continue
            for name in names:
                assert not name.startswith("ambient_quality"), (path, name)


# sha256 of Harbor v0.25.0's src/harbor/models/trajectories/*.py.
_HARBOR_SHA256 = {
    "__init__.py": "8eac32584166ff8f3a30d42027bc53793c4804fed7b07dcc4a94795557e11846",
    "agent.py": "ae26f91b1aa9cba2f9f308b3b1b6a8e92c0851bb191e4bd8df3042b49a0425c6",
    "content.py": "568f8ac708141b63b5facb363274b93d8bb76ae36250bcee656781f189ad3426",
    "final_metrics.py": "c4897c3b7f6d6be0d891d5fc9a72d13407d01c897c099e3ca195c50424734caf",
    "metrics.py": "c6d724d80d250dd27fefc4462a3f70aff2333fe42775382cb8073b889becf165",
    "observation.py": "2bb40e781053428e172af45110b0119bda4143594d592e414e9af0a60f057d28",
    "observation_result.py": "edbe26ca85aa8ef5f13a08a62d4d777d20832a7a5b67dcfa9162d6dbdaaf4c0a",
    "step.py": "6d8cc176449e0d5be46b40fdc9802bcb417f6b11d295c111e670055552abaa4a",
    "subagent_trajectory_ref.py": "207388b30eec20aaba3d21effb66f984caecfa0cb23dbaaa8fc055abd6347aea",
    "tool_call.py": "b28fc0526c45a5a253edaaaa305a8f53ac436e5fd44918fe4dadbe573214030b",
    "trajectory.py": "74c3aa74f3de38cdcdf3727b771c7ad75d66c2f5a6b43ec7982ed5b1e56b6fbf",
}
_DIFF = (
    pathlib.Path(__file__).parent
    / "fixtures"
    / "atif"
    / "harbor_v0.25.0_models.diff"
)
_HUNK_RE = re.compile(r"@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")


def _parse_diff(text: str) -> dict[str, list[tuple[int, int, list[str]]]]:
    """Parses a unified diff into hunks per file.

    Args:
        text: The diff.

    Returns:
        For each file name, its hunks as `(new_start, new_count, lines)`.
    """
    files: dict[str, list[tuple[int, int, list[str]]]] = {}
    hunks: list[tuple[int, int, list[str]]] = []
    for line in text.splitlines(keepends=True):
        if line.startswith("--- "):
            continue
        if line.startswith("+++ b/"):
            hunks = files.setdefault(line[len("+++ b/") :].strip(), [])
        elif match := _HUNK_RE.match(line):
            count = int(match.group(2) or 1)
            hunks.append((int(match.group(1)), count, []))
        else:
            hunks[-1][2].append(line)
    return files


def _reverse_hunks(
    new: list[str], hunks: list[tuple[int, int, list[str]]]
) -> str:
    """Rebuilds the original file from the changed one and the diff's hunks.

    Args:
        new: Lines of the changed file.
        hunks: The file's hunks.

    Returns:
        The original file's text.
    """
    old: list[str] = []
    position = 0
    for new_start, new_count, lines in hunks:
        start = new_start - 1 if new_count else new_start
        old.extend(new[position:start])
        expected = [line[1:] for line in lines if line[0] in " +"]
        assert new[start : start + new_count] == expected
        old.extend(line[1:] for line in lines if line[0] in " -")
        position = start + new_count
    old.extend(new[position:])
    return "".join(old)


def test_vendored_files_are_harbor_v0_25_0_plus_the_checked_in_diff():
    hunks_by_file = _parse_diff(_DIFF.read_text(encoding="utf-8"))
    vendored = sorted(
        path.name for path in (_PACKAGE_DIR / "models").glob("*.py")
    )
    assert vendored == sorted(_HARBOR_SHA256)
    for name, digest in _HARBOR_SHA256.items():
        new = (_PACKAGE_DIR / "models" / name).read_text(encoding="utf-8")
        original = _reverse_hunks(
            new.splitlines(keepends=True), hunks_by_file.get(name, [])
        )
        assert hashlib.sha256(original.encode()).hexdigest() == digest, name


def test_vendored_files_credit_harbor_not_google():
    for path in (_PACKAGE_DIR / "models").glob("*.py"):
        header = path.read_text(encoding="utf-8").split('"""', 1)[0]
        assert header.startswith("# SPDX-License-Identifier: Apache-2.0\n"), (
            path
        )
        assert "Copyright the Harbor" in header, path
        assert "Google" not in header, path
