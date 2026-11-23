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
  - Every tracked pyproject.toml sets [tool.uv] exclude-newer (>= 7 days) and
    has an up-to-date, git-tracked uv.lock (`uv lock --check --offline`).
  - Lockfiles resolve packages from public registries, not Artifact Registry.
  - ui/Dockerfile builds the front end with the Node major in ui/web/.nvmrc.
  - Dockerfile and ui/Dockerfile use a python base image that satisfies
    requires-python in pyproject.toml.
  - Every external Dockerfile FROM stage pins an explicit X.Y.Z tag and
    @sha256 digest.
"""

import argparse
import operator
import pathlib
import re
import subprocess
import sys
import tomllib
from collections.abc import Iterable, Sequence

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


def list_git_tracked_paths() -> list[pathlib.Path]:
    """List every git-tracked path recorded in the index.

    Returns:
        Repository-relative paths reported by `git ls-files`.
    """
    output = subprocess.check_output(
        ["git", "ls-files"]  # noqa: S607 - standard tool resolved from PATH
    ).decode("utf-8")
    return [pathlib.Path(line) for line in output.splitlines()]


def _dedupe_existing_paths(
    candidates: Iterable[pathlib.Path],
) -> list[pathlib.Path]:
    """Return existing file paths in first-seen order without duplicates.

    Args:
        candidates: Candidate file paths to filter and deduplicate.

    Returns:
        Deduplicated paths that exist as regular files on disk.
    """
    seen: set[pathlib.Path] = set()
    result: list[pathlib.Path] = []
    for path in candidates:
        if path.is_file() and path not in seen:
            seen.add(path)
            result.append(path)
    return result


_REPO_LOCKFILES = (
    pathlib.Path("uv.lock"),
    pathlib.Path("ui/uv.lock"),
    pathlib.Path("ui/web/package-lock.json"),
)


def list_repo_lockfiles() -> list[pathlib.Path]:
    """List the repo lockfiles (declared or git-tracked) on disk.

    Returns:
        Deduplicated paths of every lockfile named in _LOCKFILE_NAMES.
    """
    tracked = [
        path
        for path in list_git_tracked_paths()
        if path.name in _LOCKFILE_NAMES
    ]
    return _dedupe_existing_paths((*_REPO_LOCKFILES, *tracked))


# Newly published packages are the primary vector for yanked releases and
# supply-chain compromises before community advisories catch up. Every uv
# project enforces a minimum publish-age cooldown and a committed uv.lock that
# matches its pyproject.toml.
_MIN_EXCLUDE_NEWER_DAYS = 7
_EXCLUDE_NEWER_DAYS_RE = re.compile(r"^(\d+)\s+days?$|^P(\d+)D$", re.IGNORECASE)
PYTHON_PYPROJECTS = (PYPROJECT, pathlib.Path("ui/pyproject.toml"))


def list_repo_pyprojects() -> list[pathlib.Path]:
    """List every repo pyproject.toml (declared or git-tracked) on disk.

    Returns:
        Deduplicated paths of pyproject.toml files in stable order.
    """
    tracked = [
        path
        for path in list_git_tracked_paths()
        if path.name == "pyproject.toml"
    ]
    return _dedupe_existing_paths((*PYTHON_PYPROJECTS, *tracked))


def parse_exclude_newer_days(value: object) -> int | None:
    """Parse a relative [tool.uv] exclude-newer duration in whole days.

    Supports uv's human duration (`"7 days"`, `"1 day"`) and ISO-8601 day span
    (`"P7D"`). Fixed timestamps are rejected because they stop delaying new
    uploads once time advances past them.

    Args:
        value: Raw `tool.uv.exclude-newer` value from pyproject.toml.

    Returns:
        Number of days, or None if unset or not a relative day span.
    """
    if not isinstance(value, str):
        return None
    match = _EXCLUDE_NEWER_DAYS_RE.fullmatch(value.strip())
    if match is None:
        return None
    return int(match.group(1) or match.group(2))


def check_pyproject_exclude_newer(
    pyprojects: Sequence[pathlib.Path],
    min_days: int = _MIN_EXCLUDE_NEWER_DAYS,
) -> bool:
    """Check that every pyproject.toml sets a relative uv exclude-newer cooldown.

    Args:
        pyprojects: The pyproject.toml files to inspect.
        min_days: Minimum required cooldown in days.

    Returns:
        True if every file sets `tool.uv.exclude-newer` to at least `min_days`.
    """
    ok = True
    for pyproject in pyprojects:
        raw = (
            tomllib.loads(pyproject.read_text(encoding="utf-8"))
            .get("tool", {})
            .get("uv", {})
            .get("exclude-newer")
        )
        days = parse_exclude_newer_days(raw)
        if days is None or days < min_days:
            ok = False
            print(
                f"\n{pyproject} must set `[tool.uv] exclude-newer` to a"
                f' relative cooldown of at least "{min_days} days"'
                f" (found {raw!r}).",
                file=sys.stderr,
            )
        else:
            print(f'{pyproject} sets [tool.uv] exclude-newer = "{raw}".')
    return ok


def check_uv_lockfiles(
    pyprojects: Sequence[pathlib.Path],
    tracked_files: set[pathlib.Path] | None = None,
) -> bool:
    """Check that every pyproject.toml has a tracked, up-to-date uv.lock.

    Runs `uv lock --check --offline` so the verification needs no network
    access and never consults an ambient mirror configuration.

    Args:
        pyprojects: The pyproject.toml files whose sibling uv.lock to verify.
        tracked_files: Optional set of git-tracked paths. When provided, each
            uv.lock must also belong to this set so an untracked local lockfile
            cannot pass presubmit locally and fail in CI.

    Returns:
        True if every project has a valid, synchronized uv.lock.
    """
    ok = True
    for pyproject in pyprojects:
        lockfile = pyproject.with_name("uv.lock")
        if not lockfile.is_file():
            ok = False
            print(
                f"\nMissing lockfile {lockfile} for {pyproject}. Generate it"
                ' with: UV_INDEX_URL="https://pypi.org/simple"'
                f" uv lock --directory {pyproject.parent}",
                file=sys.stderr,
            )
            continue
        if (
            tracked_files is not None
            and pyproject in tracked_files
            and lockfile not in tracked_files
        ):
            ok = False
            print(
                f"\n{lockfile} exists on disk but is not tracked by git."
                f" Stage it with `git add {lockfile}`.",
                file=sys.stderr,
            )
            continue
        result = subprocess.run(  # noqa: S603 - explicit argument list without shell execution
            [  # noqa: S607 - standard tool resolved from PATH
                "uv",
                "lock",
                "--check",
                "--offline",
                "--directory",
                str(pyproject.parent),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            ok = False
            detail = (result.stderr or result.stdout).strip()
            print(
                f"\n{lockfile} is out of sync with {pyproject}:\n{detail}\n"
                'Relock with: UV_INDEX_URL="https://pypi.org/simple"'
                f" uv lock --directory {pyproject.parent}",
                file=sys.stderr,
            )
        else:
            print(f"{lockfile} is locked and matches {pyproject}.")
    return ok


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


# Outside requires-python, `uv sync` ignores the image's interpreter and
# downloads a managed CPython at build time, and `pip install` resolves
# dependencies against a version they may publish no wheels for.
PYTHON_DOCKERFILES = (pathlib.Path("Dockerfile"), UI_DOCKERFILE)
_PYTHON_FROM = re.compile(
    r"^FROM\s+(?:--\S+\s+)*(?:\S+/)?python:(\S+)", re.MULTILINE | re.IGNORECASE
)
_PYTHON_TAG_VERSION = re.compile(r"(\d+)\.(\d+)(?:\.\d+)?(?![\d.])")
_REQUIRES_PYTHON_CLAUSE = re.compile(r"(>=|<)\s*(\d+)\.(\d+)")
_VERSION_COMPARISONS = {">=": operator.ge, "<": operator.lt}


def extract_dockerfile_python_tags(text: str) -> list[str]:
    """Extract the tags of every python base image in a Dockerfile.

    Args:
        text: Contents of the Dockerfile.

    Returns:
        The tag of each `FROM python:<tag>` stage, in file order, e.g.
        ["3.13-slim"]. Empty if no stage uses a tagged python image.
    """
    return _PYTHON_FROM.findall(text)


def parse_python_tag_version(tag: str) -> tuple[int, int] | None:
    """Parse the Python minor version from a python image tag.

    Args:
        tag: The image tag, e.g. "3.13-slim".

    Returns:
        The (major, minor) version, or None if the tag does not start with an
        X.Y or X.Y.Z version (for example "slim" or "3-slim").
    """
    match = _PYTHON_TAG_VERSION.match(tag)
    return (int(match.group(1)), int(match.group(2))) if match else None


def parse_requires_python(
    spec: str,
) -> list[tuple[str, tuple[int, int]]] | None:
    """Parse a requires-python specifier into comparison clauses.

    Supports comma-separated `>=` and `<` clauses on an X.Y version, e.g.
    ">=3.11,<3.14". Only these two operators compare exactly at minor-version
    precision: a `python:3.13` image ships the latest 3.13.x patch, which PEP 440
    would reject for `<=3.13` or `==3.13`. Other forms are rejected rather than
    approximated, because `packaging` is only a transitive dependency here.

    Args:
        spec: The requires-python value from pyproject.toml.

    Returns:
        The (operator, (major, minor)) clauses, or None if any clause uses an
        unsupported form.
    """
    clauses = []
    for clause in spec.split(","):
        match = _REQUIRES_PYTHON_CLAUSE.fullmatch(clause.strip())
        if match is None:
            return None
        clauses.append(
            (match.group(1), (int(match.group(2)), int(match.group(3))))
        )
    return clauses


def matches_requires_python(
    version: tuple[int, int], clauses: list[tuple[str, tuple[int, int]]]
) -> bool:
    """Check whether a Python minor version satisfies every clause.

    Args:
        version: The (major, minor) version to test.
        clauses: Clauses from `parse_requires_python`.

    Returns:
        True if the version satisfies all clauses.
    """
    return all(
        _VERSION_COMPARISONS[op](version, bound) for op, bound in clauses
    )


def check_python_base_images(
    dockerfiles: tuple[pathlib.Path, ...] = PYTHON_DOCKERFILES,
    pyproject: pathlib.Path = PYPROJECT,
) -> bool:
    """Check that every python base image satisfies requires-python.

    Args:
        dockerfiles: The Dockerfiles whose python stages to check.
        pyproject: The pyproject.toml that declares requires-python.

    Returns:
        True if each Dockerfile has at least one python stage and every python
        stage uses a Python minor that requires-python allows.
    """
    spec = (
        tomllib.loads(pyproject.read_text(encoding="utf-8"))
        .get("project", {})
        .get("requires-python", "")
    )
    clauses = parse_requires_python(spec)
    if clauses is None:
        print(
            f'\nCould not parse requires-python "{spec}" in {pyproject}. The'
            " check supports comma-separated >= and < clauses on an X.Y"
            " version.",
            file=sys.stderr,
        )
        return False

    ok = True
    for dockerfile in dockerfiles:
        tags = extract_dockerfile_python_tags(
            dockerfile.read_text(encoding="utf-8")
        )
        if not tags:
            print(
                f"\nCould not read a Python version from {dockerfile}: it has"
                " no FROM python:<X.Y> stage.",
                file=sys.stderr,
            )
            ok = False
        for tag in tags:
            version = parse_python_tag_version(tag)
            if version is None:
                print(
                    f"\nCould not read a Python version from {dockerfile}:"
                    f" python:{tag} does not start with an X.Y or X.Y.Z"
                    " version.",
                    file=sys.stderr,
                )
                ok = False
            elif not matches_requires_python(version, clauses):
                print(
                    f"\n{dockerfile} builds on python:{tag}, but {pyproject}"
                    f' declares requires-python "{spec}". Use a python tag'
                    " inside that range, or raise requires-python once the"
                    " project supports the new version.",
                    file=sys.stderr,
                )
                ok = False
            else:
                print(
                    f"{dockerfile} builds on python:{tag}, which"
                    f' requires-python "{spec}" allows.'
                )
    return ok


# Floating image tags (`python:3.13-slim`, `node:26-slim`) resolve to different
# digests over time, pulling unreviewed OS packages or mutated tags into builds.
# Requiring `<image>:<X.Y.Z...>@sha256:<digest>` locks the exact multi-arch
# manifest while keeping the version readable to humans and version checks.
_DOCKERFILE_FROM_RE = re.compile(
    r"^\s*FROM\s+(?:--\S+\s+)*(?P<target>\S+)(?:\s+AS\s+(?P<alias>\S+))?\s*$",
    re.IGNORECASE,
)
_PINNED_BASE_IMAGE_RE = re.compile(
    r"^[^\s:@]+:v?\d+\.\d+\.\d+(?![\d.])[^\s@]*@sha256:[0-9a-f]{64}$"
)


def list_tracked_dockerfiles() -> list[pathlib.Path]:
    """List every git-tracked Dockerfile that exists in the working tree.

    Returns:
        Paths of tracked `Dockerfile` and `*.Dockerfile` files.
    """
    return [
        path
        for path in list_git_tracked_paths()
        if (path.name == "Dockerfile" or path.name.endswith(".Dockerfile"))
        and path.is_file()
    ]


def extract_dockerfile_from_targets(
    text: str,
) -> list[tuple[int, str, str | None]]:
    """Extract every FROM instruction's image target and optional stage alias.

    Args:
        text: Contents of the Dockerfile.

    Returns:
        `(1-based line number, target, alias)` tuples in file order.
    """
    stages: list[tuple[int, str, str | None]] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        match = _DOCKERFILE_FROM_RE.match(stripped)
        if match is not None:
            stages.append((lineno, match.group("target"), match.group("alias")))
    return stages


def is_pinned_base_image(target: str) -> bool:
    """Check whether a FROM image reference pins an X.Y.Z tag and sha256 digest.

    Args:
        target: Image reference from a Dockerfile `FROM` line.

    Returns:
        True if `target` has the form `<image>:<X.Y.Z...>@sha256:<64 hex>`.
    """
    return _PINNED_BASE_IMAGE_RE.fullmatch(target) is not None


def check_dockerfile_image_pins(dockerfiles: Sequence[pathlib.Path]) -> bool:
    """Check that every external FROM image pins a patch version and sha256 digest.

    Stages that build `FROM <earlier-stage>` within the same Dockerfile are
    allowed without a tag or digest because they reference a stage already
    pinned earlier in the file.

    Args:
        dockerfiles: Dockerfiles to validate.

    Returns:
        True if every Dockerfile has at least one FROM stage and every external
        base image is pinned by both X.Y.Z version and `@sha256:` digest.
    """
    ok = True
    for dockerfile in dockerfiles:
        stages = extract_dockerfile_from_targets(
            dockerfile.read_text(encoding="utf-8")
        )
        if not stages:
            ok = False
            print(
                f"\n{dockerfile} contains no FROM instructions.",
                file=sys.stderr,
            )
            continue
        known_aliases: set[str] = set()
        for lineno, target, alias in stages:
            if target.lower() not in known_aliases and not is_pinned_base_image(
                target
            ):
                ok = False
                print(
                    f"\n{dockerfile}:{lineno}: `FROM {target}` is not locked."
                    " Pin both an explicit X.Y.Z patch tag and an immutable"
                    " `@sha256:<64-hex>` manifest digest.",
                    file=sys.stderr,
                )
            if alias is not None:
                known_aliases.add(alias.lower())
    if ok:
        print(
            "All Dockerfile base images pin patch versions and sha256 digests."
        )
    return ok


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
    files_to_check = [
        str(path) for path in list_git_tracked_paths() if path.is_file()
    ]

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

    tracked_paths = set(list_git_tracked_paths())
    pyprojects = list_repo_pyprojects()

    # All run before the verdict so one invocation reports every violation
    # rather than making the caller re-run to discover the next one.
    checks = [
        check_license_headers(args.fix),
        check_requirements_txt(args.fix),
        check_pyproject_exclude_newer(pyprojects),
        check_uv_lockfiles(pyprojects, tracked_files=tracked_paths),
        check_lockfile_registries(list_repo_lockfiles()),
        check_ui_node_version(),
        check_python_base_images(),
        check_dockerfile_image_pins(list_tracked_dockerfiles()),
    ]

    if not all(checks):
        print("\nConvention checks failed.", file=sys.stderr)
        sys.exit(1)
    print("\nAll convention checks passed successfully.")


if __name__ == "__main__":
    main()
