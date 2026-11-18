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
"""Functional tests for `django.utils.archive.extract()`.

Adapted from Django's tests/utils_tests/test_archive.py at commit 6822aa5c
(BSD-3-Clause). Django ships twelve binary fixture archives; here the same
layout is built in code so no binary files are vendored. Every security fix
must keep these passing.
"""

import io
import os
import stat
import sys
import tarfile
import tempfile
import unittest
import zipfile
from typing import Literal

from django.utils import archive

# Same content as Django's fixtures: two files at three levels, plus files
# with and without permission bits.
FILES = ["1", "2", "foo/1", "foo/2", "foo/bar/1", "foo/bar/2"]
EXECUTABLE = "executable"
NO_PERMISSIONS = "no_permissions"


def _tar(
    path: str, mode: Literal["w", "w:gz", "w:bz2", "w:xz"], prefix: str
) -> None:
    with tarfile.open(path, mode) as tar:
        for name in [*FILES, EXECUTABLE, NO_PERMISSIONS]:
            data = name.encode()
            info = tarfile.TarInfo(prefix + name)
            info.size = len(data)
            info.mode = {EXECUTABLE: 0o775, NO_PERMISSIONS: 0}.get(name, 0o664)
            tar.addfile(info, io.BytesIO(data))


def _zip(path: str, prefix: str) -> None:
    with zipfile.ZipFile(path, "w") as zf:
        for name in [*FILES, EXECUTABLE, NO_PERMISSIONS]:
            info = zipfile.ZipInfo(prefix + name)
            if name == EXECUTABLE:
                info.external_attr = (stat.S_IFREG | 0o775) << 16
            elif name != NO_PERMISSIONS:
                info.external_attr = (stat.S_IFREG | 0o664) << 16
            zf.writestr(info, name)


def _build_fixtures(directory: str) -> None:
    for lead, prefix in (("", ""), ("leadpath_", "leadpath/")):
        _tar(os.path.join(directory, f"{lead}foobar.tar"), "w", prefix)
        _tar(os.path.join(directory, f"{lead}foobar.tar.gz"), "w:gz", prefix)
        _tar(os.path.join(directory, f"{lead}foobar.tar.bz2"), "w:bz2", prefix)
        _tar(os.path.join(directory, f"{lead}foobar.tar.xz"), "w:xz", prefix)
        _zip(os.path.join(directory, f"{lead}foobar.zip"), prefix)


class TestArchive(unittest.TestCase):
    _fixtures: tempfile.TemporaryDirectory[str]
    testdir: str

    @classmethod
    def setUpClass(cls) -> None:
        cls._fixtures = tempfile.TemporaryDirectory()
        cls.testdir = cls._fixtures.name
        _build_fixtures(cls.testdir)

    @classmethod
    def tearDownClass(cls) -> None:
        cls._fixtures.cleanup()

    def test_extract_function(self) -> None:
        for entry in os.scandir(self.testdir):
            with (
                self.subTest(entry.name),
                tempfile.TemporaryDirectory() as tmpdir,
            ):
                archive.extract(entry.path, tmpdir)
                for name in FILES:
                    self.assertTrue(os.path.isfile(os.path.join(tmpdir, name)))

    @unittest.skipIf(
        sys.platform == "win32", "Python on Windows has a limited os.chmod()."
    )
    def test_extract_file_permissions(self) -> None:
        """archive.extract() preserves file permissions."""
        mask = stat.S_IRWXU | stat.S_IRWXG | stat.S_IRWXO
        umask = os.umask(0)
        os.umask(umask)  # Restore the original umask.
        for entry in os.scandir(self.testdir):
            if entry.name.startswith("leadpath_"):
                continue
            with (
                self.subTest(entry.name),
                tempfile.TemporaryDirectory() as tmpdir,
            ):
                archive.extract(entry.path, tmpdir)
                # An executable file in the archive has executable permissions.
                filepath = os.path.join(tmpdir, EXECUTABLE)
                self.assertEqual(os.stat(filepath).st_mode & mask, 0o775)
                # A file is readable even if permission data is missing.
                filepath = os.path.join(tmpdir, NO_PERMISSIONS)
                self.assertEqual(
                    os.stat(filepath).st_mode & mask, 0o666 & ~umask
                )

    def test_unrecognized_format(self) -> None:
        with self.assertRaises(archive.UnrecognizedArchiveFormat):
            archive.extract("templates.rar", tempfile.gettempdir())


if __name__ == "__main__":
    unittest.main()
