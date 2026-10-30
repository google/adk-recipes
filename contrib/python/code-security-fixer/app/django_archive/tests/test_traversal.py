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
"""Exploit tests for the two published archive.extract() traversals.

They reproduce the archives from Django's own regression tests:

- CVE-2021-3281 / GHSA-fvgf-6h6h-3322 (fixed in 05413afa): members with dot
  segments (Django's traversal.tar) or an absolute path
  (traversal_absolute.tar) are written outside the target directory.
- CVE-2025-59682 / GHSA-q95w-c7qg-hrff (fixed in 924a0c09): the 2021 fix
  checked `filename.startswith(target_path)`, so a sibling directory sharing
  the prefix (`<target>abc/evil.txt`) still escaped.

Every payload is aimed inside a private temporary directory. The security
property checked is that nothing is written outside the target directory;
raising an error (Django raises SuspiciousOperation) is the expected way.
"""

import io
import os
import tarfile
import tempfile
import unittest
import zipfile

from django.utils import archive


def _tar_with(path: str, names: list[str]) -> None:
    with tarfile.open(path, "w") as tar:
        for name in names:
            info = tarfile.TarInfo(name)
            if name.endswith("/"):
                info.type = tarfile.DIRTYPE
                info.mode = 0o775
                tar.addfile(info)
            else:
                info.size = 4
                tar.addfile(info, io.BytesIO(b"evil"))


class TraversalTest(unittest.TestCase):
    def setUp(self) -> None:
        self._root = tempfile.TemporaryDirectory()
        self.root = os.path.realpath(self._root.name)
        self.target = os.path.join(self.root, "target")
        os.makedirs(self.target)

    def tearDown(self) -> None:
        self._root.cleanup()

    def _extract(self, archive_path: str) -> None:
        try:
            archive.extract(archive_path, self.target)
        except Exception:
            pass

    def test_cve_2021_3281_dot_segments(self) -> None:
        payload = os.path.join(self.root, "traversal.tar")
        _tar_with(payload, ["foo/", "foo/../", "foo/../evil.py"])
        self._extract(payload)
        self.assertFalse(os.path.exists(os.path.join(self.root, "evil.py")))

    def test_cve_2021_3281_absolute_path(self) -> None:
        payload = os.path.join(self.root, "traversal_absolute.tar")
        evil = os.path.join(self.root, "outside", "evil.py")
        _tar_with(payload, ["foo/", evil])
        self._extract(payload)
        self.assertFalse(os.path.exists(evil))

    def test_cve_2025_59682_sibling_prefix(self) -> None:
        payload = os.path.join(self.root, "traversal_startswith.zip")
        evil = os.path.join(self.target + "abc", "evil.txt")
        with zipfile.ZipFile(payload, "w") as zf:
            zf.writestr(evil, "evil\n")
            zf.writestr("test.txt", "data\n")
        self._extract(payload)
        self.assertFalse(os.path.exists(evil))


if __name__ == "__main__":
    unittest.main()
