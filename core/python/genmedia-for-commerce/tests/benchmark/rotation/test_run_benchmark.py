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

from genmedia4commerce.core import video
from genmedia4commerce.core.testing import mp4_stub
from genmedia4commerce.eval import motion
from tests.benchmark.rotation import run_benchmark


def test_benchmark_scores_labelled_clips(tmp_path, monkeypatch, capsys):
    forward = mp4_stub(frames=6)
    (tmp_path / "a__cw.mp4").write_bytes(forward)
    (tmp_path / "b__invalid.mp4").write_bytes(mp4_stub(frames=4))
    (tmp_path / "unlabelled.mp4").write_bytes(forward)
    monkeypatch.setattr(video, "reverse", lambda data: b"REVERSED")
    predictions = {forward: "clockwise", b"REVERSED": "clockwise"}
    monkeypatch.setattr(
        motion, "check_rotation", lambda data: predictions.get(data, "invalid")
    )
    assert run_benchmark.main([str(tmp_path), "--add-reversed"]) == 0
    out = capsys.readouterr().out
    assert "skip" in out and "unlabelled.mp4" in out
    assert "a__cw.mp4 (reversed)" in out and "truth=anticlockwise" in out
    assert "accuracy: 2/3 = 66.7%" in out


def test_benchmark_without_clips_fails(tmp_path):
    assert run_benchmark.main([str(tmp_path)]) == 1
