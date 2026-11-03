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

"""Every code and configuration file starts with the Apache 2.0 license
header (JSON, docs, prompts, test golden files and data cannot or must not
carry one)."""

import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CODE = {
    *(
        ".py",
        ".ts",
        ".tsx",
        ".js",
        ".mjs",
        ".cjs",
        ".jsx",
        ".css",
        ".html",
        ".sh",
    ),
    *(".tf", ".toml", ".yaml", ".yml", ".example"),
}
NAMES = {"Makefile", "Dockerfile"}
SKIP_DIRS = {
    "node_modules",
    "dist",
    "build",
    ".venv",
    "__pycache__",
    ".git",
    ".adk",
}
HEADER = [
    "Copyright 2026 Google LLC",
    "",
    'Licensed under the Apache License, Version 2.0 (the "License");',
    "you may not use this file except in compliance with the License.",
    "You may obtain a copy of the License at",
    "",
    "https://www.apache.org/licenses/LICENSE-2.0",
    "",
    "Unless required by applicable law or agreed to in writing, software",
    'distributed under the License is distributed on an "AS IS" BASIS,',
    "WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.",
    "See the License for the specific language governing permissions and",
    "limitations under the License.",
]
#: Comment markers a header line may start or end with (longest first).
_LEADING = ("<!--", "/**", "/*", "//", "*/", "#", "*")
_TRAILING = ("*/", "-->")


def _code_files() -> list[Path]:
    """The tracked code files (git), or every code file outside the build
    and tool folders when the folder is not a git checkout."""
    try:
        listed = subprocess.run(
            ["git", "-C", str(ROOT), "ls-files"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.split()
        files = [ROOT / name for name in listed]
    except (OSError, subprocess.CalledProcessError):
        files = [
            p
            for p in ROOT.rglob("*")
            if not SKIP_DIRS & set(p.relative_to(ROOT).parts)
        ]
    return sorted(
        p
        for p in files
        if (p.suffix in CODE or p.name in NAMES) and p.is_file()
    )


def _header(path: Path) -> list[str]:
    """The comment lines from ``Copyright`` to the end of the license, without
    comment markers or indentation."""
    lines = path.read_text(encoding="utf-8").splitlines()[:40]
    start = next(
        (i for i, line in enumerate(lines) if "Copyright" in line), None
    )
    if start is None:
        return []
    found = []
    for line in lines[start:]:
        found.append(_text(line))
        if "limitations under the License." in line:
            break
    return found


def _text(line: str) -> str:
    """``line`` without its comment markers and surrounding spaces."""
    text = line.strip()
    for marker in _LEADING:
        if text.startswith(marker):
            text = text[len(marker) :]
            break
    for marker in _TRAILING:
        if text.endswith(marker):
            text = text[: -len(marker)]
            break
    return text.strip()


def test_every_code_and_config_file_has_the_license_header():
    files = _code_files()
    assert files
    missing = [str(p.relative_to(ROOT)) for p in files if _header(p) != HEADER]
    assert not missing, f"missing or different license header: {missing}"
