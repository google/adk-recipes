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

"""infra/terraform.sh uses a terraform on PATH only when it really runs."""

import os
import subprocess
from pathlib import Path

SCRIPT = Path(__file__).parents[2] / "infra" / "terraform.sh"


def _fake(path: Path, version_line: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        f'#!/bin/bash\nif [ "$1" = version ]; then echo "{version_line}"; '
        f'else echo "ran $(basename $(dirname $0)) $*"; fi\n'
    )
    path.chmod(0o755)


def _run(tmp_path: Path, on_path: str) -> str:
    stub = tmp_path / "stubbin" / "terraform"
    _fake(stub, on_path)
    _fake(
        tmp_path / "home" / ".local" / "bin" / "terraform", "Terraform v1.16.6"
    )
    env = {
        **os.environ,
        "HOME": str(tmp_path / "home"),
        "PATH": f"{stub.parent}:/usr/bin:/bin",
    }
    out = subprocess.run(
        ["bash", str(SCRIPT), "plan"],
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    return out.stdout.strip()


def test_a_stub_on_path_is_skipped_for_the_pinned_binary(tmp_path: Path):
    """Cloud Shell's stub prints install instructions and exits 0."""
    stub_text = "Follow the instructions to install terraform"
    assert _run(tmp_path, stub_text) == "ran bin plan"


def test_a_working_terraform_on_path_is_used(tmp_path: Path):
    assert _run(tmp_path, "Terraform v1.9.0") == "ran stubbin plan"
