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
  - Lockfiles resolve packages from public registries, not Artifact Registry.
  - ui/Dockerfile builds the front end with the Node major in ui/web/.nvmrc.
"""

import argparse
import pathlib
import re
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


# Corp workstations route pip, uv and npm through an Airlock mirror on Artifact
# Registry (*.pkg.dev). A lockfile regenerated there records the mirror's URLs,
# and every build without corp credentials (Cloud Build, GitHub runners, users
# outside Google) then fails the install with 401.
_LOCKFILE_NAMES = frozenset({"uv.lock", "package-lock.json"})
_MIRROR_URL = re.compile(r"https?://[^\s\"']*\.pkg\.dev/[^\s\"']*")
_MAX_REPORTED_MIRROR_URLS = 5


def list_mirror_urls(text: str) -> list[tuple[int, str]]:
    """List the Artifact Registry URLs in a lockfile.

    Args:
        text: Contents of the lockfile.

    Returns:
        (1-based line number, URL) for every *.pkg.dev URL, in file order.
    """
    return [
        (lineno, match.group(0))
        for lineno, line in enumerate(text.splitlines(), start=1)
        for match in _MIRROR_URL.finditer(line)
    ]


def check_lockfile_registries(lockfiles: list[pathlib.Path]) -> bool:
    """Check that no lockfile resolves packages from an Artifact Registry mirror.

    There is no fix mode: rewriting the host is only safe when the mirror path
    maps onto the public one, which differs between npm and PyPI. Regenerating
    the lockfile against the public index is the fix.

    Args:
        lockfiles: Lockfiles to scan.

    Returns:
        True if no lockfile contains a *.pkg.dev URL.
    """
    ok = True
    for lockfile in lockfiles:
        urls = list_mirror_urls(lockfile.read_text(encoding="utf-8"))
        if not urls:
            continue
        ok = False
        print(
            f"\n{lockfile} resolves {len(urls)} package(s) from an Artifact"
            " Registry mirror, which builds without corp credentials cannot"
            " reach:",
            file=sys.stderr,
        )
        for lineno, url in urls[:_MAX_REPORTED_MIRROR_URLS]:
            print(f"  {lockfile}:{lineno}: {url}", file=sys.stderr)
        if len(urls) > _MAX_REPORTED_MIRROR_URLS:
            print(
                f"  ... and {len(urls) - _MAX_REPORTED_MIRROR_URLS} more",
                file=sys.stderr,
            )
        print(
            "For uv.lock, regenerate it with"
            ' UV_INDEX_URL="https://pypi.org/simple" uv lock. For'
            " package-lock.json, replace the mirror prefix up to the package"
            " name with https://registry.npmjs.org/ and check that npm ci"
            " still passes the recorded integrity hashes.",
            file=sys.stderr,
        )
    if ok:
        print("Lockfiles resolve packages from public registries.")
    return ok


def list_tracked_lockfiles() -> list[pathlib.Path]:
    """List the git-tracked lockfiles that exist in the working tree.

    Returns:
        Paths of every tracked file named in _LOCKFILE_NAMES.
    """
    output = subprocess.check_output(
        ["git", "ls-files"]  # noqa: S607 - standard tool resolved from PATH
    ).decode("utf-8")
    return [
        path
        for path in map(pathlib.Path, output.splitlines())
        if path.name in _LOCKFILE_NAMES and path.is_file()
    ]


# The front end is type-checked and tested by frontend-tests.yml on the Node
# version in .nvmrc, and built into the dashboard image by ui/Dockerfile. The
# version that builds the bundle has to be the one it was checked against.
UI_DOCKERFILE = pathlib.Path("ui/Dockerfile")
NVMRC = pathlib.Path("ui/web/.nvmrc")
_NODE_FROM = re.compile(r"^FROM\s+(?:\S+/)?node:v?(\d+)\b", re.MULTILINE)


def extract_dockerfile_node_major(text: str) -> str | None:
    """Extract the Node major version from a Dockerfile's node base image.

    Args:
        text: Contents of the Dockerfile.

    Returns:
        The major version of the first `FROM node:<version>` stage, or None
        if no stage uses a versioned node image.
    """
    match = _NODE_FROM.search(text)
    return match.group(1) if match else None


def extract_nvmrc_node_major(text: str) -> str | None:
    """Extract the Node major version from an .nvmrc file.

    Args:
        text: Contents of the .nvmrc file, e.g. "22", "v22.11.0".

    Returns:
        The major version, or None if the file does not name a numeric version
        (for example an alias such as "lts/*").
    """
    match = re.fullmatch(r"v?(\d+)(?:\.\d+){0,2}", text.strip())
    return match.group(1) if match else None


def check_ui_node_version(
    dockerfile: pathlib.Path = UI_DOCKERFILE, nvmrc: pathlib.Path = NVMRC
) -> bool:
    """Check that ui/Dockerfile builds the front end on the .nvmrc Node major.

    Args:
        dockerfile: The dashboard Dockerfile.
        nvmrc: The front end's .nvmrc.

    Returns:
        True if both name the same Node major version.
    """
    image_major = extract_dockerfile_node_major(
        dockerfile.read_text(encoding="utf-8")
    )
    nvmrc_major = extract_nvmrc_node_major(nvmrc.read_text(encoding="utf-8"))
    if image_major is None or nvmrc_major is None:
        print(
            f"\nCould not read a Node major version from"
            f" {dockerfile if image_major is None else nvmrc}.",
            file=sys.stderr,
        )
        return False
    if image_major != nvmrc_major:
        print(
            f"\n{dockerfile} builds the front end on node:{image_major}, but"
            f" {nvmrc} (which frontend-tests.yml runs on) says {nvmrc_major}."
            " Change both together.",
            file=sys.stderr,
        )
        return False
    print(f"{dockerfile} and {nvmrc} both use Node {image_major}.")
    return True


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

    # All run before the verdict so one invocation reports every violation
    # rather than making the caller re-run to discover the next one.
    checks = [
        check_license_headers(args.fix),
        check_requirements_txt(args.fix),
        check_lockfile_registries(list_tracked_lockfiles()),
        check_ui_node_version(),
    ]

    if not all(checks):
        print("\nConvention checks failed.", file=sys.stderr)
        sys.exit(1)
    print("\nAll convention checks passed successfully.")


if __name__ == "__main__":
    main()
