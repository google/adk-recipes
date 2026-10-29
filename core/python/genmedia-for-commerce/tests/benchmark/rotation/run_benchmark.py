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

"""Benchmark ``eval.motion.check_rotation`` (the V3f check of the generic spin).

Usage::

    uv run python tests/benchmark/rotation/run_benchmark.py FOLDER ... [--add-reversed]

Each FOLDER holds clips named
``<anything>__cw.mp4``, ``<anything>__ccw.mp4`` or ``<anything>__invalid.mp4``
(or ``.webp``, an animated WebP; the label is the ground truth: clockwise,
anticlockwise, or no single clean direction). The script prints one line per
clip, the confusion matrix and the accuracy. ``--add-reversed`` also scores
every cw/ccw clip played backwards with the opposite label (ground truth by
construction).

Collecting clips:

1. ``GENMEDIA_LIVE=1 uv run pytest tests/live/test_spin_live.py -q`` saves every
   generated variation under ``/tmp/genmedia_live/spin/``.
2. Watch each clip and copy it into a folder of your own with its label, e.g.
   ``cp /tmp/genmedia_live/spin/other_0.webp ~/rotation-clips/phone_0__cw.webp``.
   Clips that reverse, wobble through a sustained turn back, or do not spin
   are ``__invalid``.
3. ``uv run python tests/benchmark/rotation/run_benchmark.py
   ~/rotation-clips --add-reversed``.

On 20 omni garment spins it agreed with a human 15 times (75%, below the 90%
needed to gate), so the generic route gates on glitch detection + fidelity
and uses the rotation only to reverse anticlockwise clips.
"""

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from genmedia4commerce.core import video
from genmedia4commerce.eval import motion

CLASSES = ("clockwise", "anticlockwise", "invalid")
SUFFIXES = {"cw": "clockwise", "ccw": "anticlockwise", "invalid": "invalid"}
OPPOSITE = {"clockwise": "anticlockwise", "anticlockwise": "clockwise"}


def load(
    folders: list[Path], add_reversed: bool
) -> list[tuple[str, str, bytes]]:
    """Return ``(name, true label, clip bytes)`` for every labelled clip."""
    samples = []
    for folder in folders:
        for path in sorted([*folder.glob("*.mp4"), *folder.glob("*.webp")]):
            label = SUFFIXES.get(path.stem.rpartition("__")[2])
            if label is None or not path.exists():
                print(
                    f"skip {path}: no __cw/__ccw/__invalid suffix or missing file"
                )
                continue
            data = path.read_bytes()
            samples.append((path.name, label, data))
            if add_reversed and label in OPPOSITE:
                name = f"{path.name} (reversed)"
                samples.append((name, OPPOSITE[label], video.reverse(data)))
    return samples


def report(results: Sequence[tuple[str, str, str]]) -> float:
    """Print the confusion matrix (rows = truth) and return the accuracy."""
    matrix = {t: dict.fromkeys(CLASSES, 0) for t in CLASSES}
    for _, truth, predicted in results:
        matrix[truth][predicted] += 1
    header = "truth / predicted"
    print(f"\n{header:>18} " + " ".join(f"{c:>13}" for c in CLASSES))
    for truth in CLASSES:
        row = " ".join(f"{matrix[truth][p]:>13}" for p in CLASSES)
        print(f"{truth:>18} {row}")
    correct = sum(matrix[c][c] for c in CLASSES)
    accuracy = correct / len(results) if results else 0.0
    print(f"\naccuracy: {correct}/{len(results)} = {accuracy:.1%}")
    return accuracy


def main(argv: list[str] | None = None) -> int:
    """Run ``check_rotation`` on every clip and print the scores."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("folders", nargs="+", type=Path)
    parser.add_argument("--add-reversed", action="store_true")
    args = parser.parse_args(argv)
    samples = load(args.folders, args.add_reversed)
    if not samples:
        print("no labelled clips found")
        return 1
    results = []
    for name, truth, data in samples:
        predicted = motion.check_rotation(data)
        flag = "" if predicted == truth else "   <-- wrong"
        print(f"{name:48} truth={truth:13} predicted={predicted}{flag}")
        results.append((name, truth, predicted))
    report(results)
    return 0


if __name__ == "__main__":
    sys.exit(main())
