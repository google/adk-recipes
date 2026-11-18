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
"""Django's historical patches for the bundled target, used by the tests.

Each patch is applied hunk by hunk to the vendored, vulnerable
`django/utils/archive.py`, with the exact text of the upstream commits:

- `FIX_2021`: 05413afa8c18cdb978fcdf470e09f7a12b234a23, "Fixed CVE-2021-3281
  -- Fixed potential directory-traversal via archive.extract()."
- `FIX_2025`: 2021 plus 924a0c092e65fa2d0953fd1855d2dc8786d94de2, "Fixed
  CVE-2025-59682 -- Fixed potential partial directory-traversal via
  archive.extract()."

Applying a hunk fails loudly if the context does not match, so these tests
prove the vendored file is the real pre-fix code.
"""

from pathlib import Path

from app import workspace

ARCHIVE = "django/utils/archive.py"
VULNERABLE = (workspace.BUNDLED_TARGET / ARCHIVE).read_text(encoding="utf-8")


def _apply(text: str, hunks: list[tuple[str, str]]) -> str:
    for before, after in hunks:
        if text.count(before) != 1:
            raise ValueError(f"hunk context not found exactly once: {before!r}")
        text = text.replace(before, after)
    return text


_HUNKS_2021 = [
    (
        "import tarfile\nimport zipfile\n\n",
        "import tarfile\nimport zipfile\n\n"
        "from django.core.exceptions import SuspiciousOperation\n\n",
    ),
    (
        "        return True\n\n    def extract(self):\n",
        "        return True\n\n"
        "    def target_filename(self, to_path, name):\n"
        "        target_path = os.path.abspath(to_path)\n"
        "        filename = os.path.abspath(os.path.join(target_path, name))\n"
        "        if not filename.startswith(target_path):\n"
        '            raise SuspiciousOperation("Archive contains invalid path:'
        " '%s'\" % name)\n"
        "        return filename\n\n"
        "    def extract(self):\n",
    ),
    (
        "                name = self.split_leading_dir(name)[1]\n"
        "            filename = os.path.join(to_path, name)\n"
        "            if member.isdir():\n",
        "                name = self.split_leading_dir(name)[1]\n"
        "            filename = self.target_filename(to_path, name)\n"
        "            if member.isdir():\n",
    ),
    (
        "            filename = os.path.join(to_path, name)\n"
        "            if filename.endswith(('/', '\\\\')):\n",
        "            if not name:\n"
        "                continue\n"
        "            filename = self.target_filename(to_path, name)\n"
        "            if name.endswith(('/', '\\\\')):\n",
    ),
]

_HUNKS_2025 = [
    (
        "        if not filename.startswith(target_path):\n"
        '            raise SuspiciousOperation("Archive contains invalid path:'
        " '%s'\" % name)\n",
        "        try:\n"
        "            if os.path.commonpath([target_path, filename]) !="
        " target_path:\n"
        '                raise SuspiciousOperation("Archive contains invalid'
        " path: '%s'\" % name)\n"
        "        except ValueError:\n"
        "            # Different drives on Windows raises ValueError.\n"
        '            raise SuspiciousOperation("Archive contains invalid path:'
        " '%s'\" % name)\n",
    ),
]

FIX_2021 = _apply(VULNERABLE, _HUNKS_2021)
FIX_2025 = _apply(FIX_2021, _HUNKS_2025)

# Looks like a fix (paths are normalized) but blocks nothing: normpath keeps
# absolute paths absolute and leaves leading ".." segments in place.
INCOMPLETE = VULNERABLE.replace(
    "            filename = os.path.join(to_path, name)\n"
    "            if member.isdir():\n",
    "            filename = os.path.normpath(os.path.join(to_path, name))\n"
    "            if member.isdir():\n",
)

# Blocks every traversal but breaks extraction of archives with a leading
# directory (functional regression).
BROKEN = FIX_2025.replace(
    "            if leading:\n                name = self.split_leading_dir(name)[1]\n",
    "",
)


def patched_target(tmp: Path, content: str) -> Path:
    """Copies the bundled target to `tmp` with `archive.py` replaced."""
    copy = workspace.copy_tree(workspace.BUNDLED_TARGET, str(tmp))
    (copy / ARCHIVE).write_text(content, encoding="utf-8")
    return copy
