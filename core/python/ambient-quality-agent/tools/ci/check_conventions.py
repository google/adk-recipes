#!/usr/bin/env python3
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

"""Check project conventions across the codebase.

Currently enforces:
  - Apache 2.0 license headers on git-tracked source files (via addlicense).
  - src/ambient_quality_agent/requirements.txt matches pyproject.toml.
"""

import argparse
import pathlib
import subprocess
import sys
import tomllib

# The agent package's requirements.txt mirrors the runtime dependencies declared
# in pyproject.toml for consumers that cannot read pyproject.toml directly, such
# as `adk deploy agent_engine`, whose Dockerfile runs
# `pip install -r requirements.txt`.
#
# Checked against pyproject.toml rather than uv.lock because requirements.txt
# defines version ranges rather than locked pins.
PYPROJECT = pathlib.Path("pyproject.toml")
REQUIREMENTS_TXT = pathlib.Path("src/ambient_quality_agent/requirements.txt")
_GENERATED_HEADER = (
    "# Generated from pyproject.toml [project.dependencies]. Do not edit by hand;\n"
    "# edit pyproject.toml and re-run `make lint`.\n"
)


def _build_expected_requirements() -> str:
    deps = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))["project"][
        "dependencies"
    ]
    return _GENERATED_HEADER + "".join(f"{d}\n" for d in sorted(deps))


def check_requirements_txt(fix: bool) -> bool:
    """Check that requirements.txt mirrors pyproject.toml dependencies.

    Args:
        fix: If True, regenerates requirements.txt from pyproject.toml.

    Returns:
        True if requirements.txt is synchronized or fixed successfully.
    """
    expected = _build_expected_requirements()
    actual = None
    if REQUIREMENTS_TXT.exists():
        actual = REQUIREMENTS_TXT.read_text(encoding="utf-8")

    if actual == expected:
        print(f"{REQUIREMENTS_TXT} matches pyproject.toml.")
        return True

    if fix:
        REQUIREMENTS_TXT.write_text(expected, encoding="utf-8")
        print(f"Regenerated {REQUIREMENTS_TXT} from pyproject.toml.")
        return True

    verb = "is missing" if actual is None else "has drifted from pyproject.toml"
    print(
        f"\n{REQUIREMENTS_TXT} {verb}. It is generated -- run `make lint` to"
        " regenerate it, and put any intended dependency change in"
        " pyproject.toml.",
        file=sys.stderr,
    )
    return False


def check_license_headers(fix: bool) -> bool:
    """Check and optionally fix missing license headers using addlicense.

    Args:
        fix: If True, adds missing license headers to tracked files.

    Returns:
        True if all checked files have license headers or were fixed.
    """
    ignore_patterns = [
        # File types that do not require a license header. YAML and generated
        # source files still require one (ui/web/src/routeTree.gen.ts gets its
        # header from ui/web/vite.config.ts).
        "**/*.md",
        "**/*.txt",
        "**/*.toml",
        "**/*.lock",
        "**/*.json",
        "**/*.cfg",
        "**/*.ini",
        ".gitignore",
        "Makefile",
    ]

    # Filter git-tracked files to avoid touching venvs, build directories, or scratch files.
    git_files_output = subprocess.check_output(
        ["git", "ls-files"]  # noqa: S607 - standard tool resolved from PATH
    ).decode("utf-8")
    files = git_files_output.splitlines()

    files_to_check = []
    for f in files:
        path = pathlib.Path(f)
        # Skip files recorded in git index that are deleted in the working directory.
        if path.is_file():
            files_to_check.append(str(path))

    if not files_to_check:
        print("No files to check for license headers.")
        return True

    cmd = [
        "go",
        "run",
        "github.com/google/addlicense@latest",
        "-c",
        "Google LLC",
        "-l",
        "apache",
        "-y",
        "2026",
    ]
    for pattern in ignore_patterns:
        cmd.extend(["-ignore", pattern])

    if not fix:
        cmd.append("-check")

    cmd.extend(files_to_check)

    try:
        if fix:
            print("Applying license headers using addlicense...")
        else:
            print("Checking license headers using addlicense...")

        subprocess.run(cmd, check=True)  # noqa: S603 - explicit argument list without shell execution

        if fix:
            print("License headers applied successfully.")
        else:
            print("All files have license headers.")
        return True
    except subprocess.CalledProcessError as e:
        if e.returncode == 1 and not fix:
            print(
                "\nSome files are missing license headers. "
                "Run with --fix to automatically add them."
            )
        else:
            print(
                f"\nCommand failed with exit code: {e.returncode}",
                file=sys.stderr,
            )
        return False
    except FileNotFoundError:
        print(
            "Error: 'go' command is required to run addlicense.",
            file=sys.stderr,
        )
        return False


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--fix",
        action="store_true",
        help="Fix convention violations where possible.",
    )
    args = parser.parse_args()

    # Both run before the verdict so one invocation reports every violation
    # rather than making the caller re-run to discover the next one.
    checks = [
        check_license_headers(args.fix),
        check_requirements_txt(args.fix),
    ]

    if not all(checks):
        print("\nConvention checks failed.", file=sys.stderr)
        sys.exit(1)
    print("\nAll convention checks passed successfully.")


if __name__ == "__main__":
    main()
