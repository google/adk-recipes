"""
Checks that every wheel and sdist distribution in a uv.lock file has a hash
in a recognised format.

Usage: python check_lockfile_hashes.py <path-to-uv.lock>

Exit codes:
  0  all distributions have valid hashes
  1  contributor-fixable problems found; every one has been reported with
     a fix, both as a human block and as a ::error annotation
  2  CI fault — the checker crashed or was invoked wrongly. Never blamed
     on the contributor's files.
"""

import re
import sys
import tomllib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))
from ci_message import (
    Diagnostic,
    Doc,
    guard,
    infra_fault,
    report,
    report_infra_fault,
)

CHECKER = "check_lockfile_hashes.py"
CHECK = "lockfile-hashes"

# Hashes produced by uv are always sha256; sha384 and sha512 are accepted
# defensively in case a future uv release adds stronger algorithms.
_HASH_RE = re.compile(r"^(sha256|sha384|sha512):[0-9a-f]+$")

# Source kinds that carry no downloadable artifact hash. Virtual workspace
# roots have no artifact at all. Path, editable, directory, and git deps are
# rejected by upstream pipeline checks (Check 3 / Check 2) before this script
# runs, so skipping them here is primarily defence-in-depth: the script stays
# correct even if the pipeline order ever changes.
_NO_HASH_SOURCE_KEYS = frozenset(
    {"virtual", "path", "editable", "directory", "git"}
)

_REGENERATE = (
    "Regenerate the lockfile from the recipe directory rather than editing "
    "it by hand:\n"
    "  rm uv.lock\n"
    "  uv lock\n"
    "Commit the regenerated uv.lock."
)


def _shape_error(lockfile: str, what: str) -> Diagnostic:
    """A uv.lock whose structure is not one uv would ever have written.

    Reported to the contributor rather than raised, because every way of
    getting here — a truncated file, a merge conflict resolved by hand, an
    entry edited in an editor — is something they did to the file and can
    undo by regenerating it.
    """
    return Diagnostic(
        check=CHECK,
        what=what,
        why=(
            "uv.lock is a generated file. uv always writes `[[package]]` as "
            "an array of tables whose `wheels` / `sdist` entries are "
            "themselves tables. A different shape means this lockfile was "
            "hand-edited or written by another tool, so its hashes cannot "
            "be verified."
        ),
        how=_REGENERATE,
        doc=Doc.LOCK_HASH,
        file=lockfile,
    )


def _missing_hash(lockfile: str, label: str, where: str) -> Diagnostic:
    return Diagnostic(
        check=CHECK,
        what=f"{label} has a distribution with no `hash` field: {where}",
        why=(
            "Every registry distribution in uv.lock must carry a hash so "
            "the artifact CI downloads can be proven identical to the one "
            "that was locked. uv writes sha256 hashes by default, so a "
            "missing hash means the lockfile was hand-edited or generated "
            "with a tool that omits them."
        ),
        how=_REGENERATE,
        doc=Doc.LOCK_HASH,
        file=lockfile,
    )


def _bad_hash(lockfile: str, label: str, hash_val: str) -> Diagnostic:
    return Diagnostic(
        check=CHECK,
        what=f"{label} has an unrecognised hash: {hash_val!r}",
        why=(
            "A hash must be `<algorithm>:<hex digest>` where the algorithm "
            "is sha256 (sha384 and sha512 are also accepted). Anything else "
            "cannot be verified at install time, so the entry provides no "
            "supply-chain guarantee even though a hash is present."
        ),
        how=_REGENERATE,
        doc=Doc.LOCK_HASH,
        file=lockfile,
    )


def _describe(value: object) -> str:
    """`list ['a', 'b']` — the type first, because the type is the bug."""
    text = repr(value)
    if len(text) > 60:
        text = text[:57] + "..."
    return f"{type(value).__name__} {text}"


def _check_package(
    pkg: object, position: int, lockfile: str
) -> list[Diagnostic]:
    """Validate hashes for all distributions in one lockfile package entry."""
    if not isinstance(pkg, dict):
        return [
            _shape_error(
                lockfile,
                f"`[[package]]` entry #{position} is a {_describe(pkg)}, "
                f"not a table.",
            )
        ]

    source = pkg.get("source", {})
    if not isinstance(source, dict):
        return [
            _shape_error(
                lockfile,
                f"`[[package]]` entry #{position} has a `source` that is a "
                f"{_describe(source)}, not a table.",
            )
        ]
    if any(key in source for key in _NO_HASH_SOURCE_KEYS):
        return []

    name = pkg.get("name", "<unknown>")
    version = pkg.get("version", "")
    label = f"{name}=={version}" if version else str(name)

    wheels = pkg.get("wheels", [])
    if not isinstance(wheels, list):
        return [
            _shape_error(
                lockfile,
                f"{label} has a `wheels` value that is a "
                f"{_describe(wheels)}, not an array of tables.",
            )
        ]
    dists = list(wheels) + ([pkg["sdist"]] if "sdist" in pkg else [])

    diagnostics: list[Diagnostic] = []
    for dist in dists:
        if not isinstance(dist, dict):
            diagnostics.append(
                _shape_error(
                    lockfile,
                    f"{label} has a distribution entry that is a "
                    f"{_describe(dist)}, not a table.",
                )
            )
            continue
        url_or_path = dist.get("url", dist.get("path", "?"))
        hash_val = dist.get("hash", "")
        if not hash_val:
            diagnostics.append(_missing_hash(lockfile, label, url_or_path))
        elif not isinstance(hash_val, str) or not _HASH_RE.match(hash_val):
            diagnostics.append(_bad_hash(lockfile, label, hash_val))
    return diagnostics


def _run(lockfile: str) -> int:
    try:
        with open(lockfile, "rb") as f:
            data = tomllib.load(f)
    except FileNotFoundError:
        return report_infra_fault(
            infra_fault(
                CHECKER,
                f"{lockfile} does not exist. The path came from the "
                f"workflow's lockfile discovery step, not from the "
                f"contributor.",
            )
        )
    except tomllib.TOMLDecodeError as exc:
        return _report(
            [_shape_error(lockfile, f"{lockfile} is not valid TOML: {exc}")],
            lockfile,
        )

    packages = data.get("package", [])
    if not isinstance(packages, list):
        return _report(
            [
                _shape_error(
                    lockfile,
                    f"`package` is a {_describe(packages)}, not an array of "
                    f"tables.",
                )
            ],
            lockfile,
        )

    diagnostics: list[Diagnostic] = []
    for position, pkg in enumerate(packages, start=1):
        diagnostics.extend(_check_package(pkg, position, lockfile))
    return _report(diagnostics, lockfile)


def _report(diagnostics: list[Diagnostic], lockfile: str) -> int:
    return report(
        diagnostics,
        header=f"{lockfile} — hash problems",
        passed_message=(
            f"{lockfile}: every distribution carries a valid hash."
        ),
        next_step=(
            "Regenerating the lockfile fixes all of the above at once:\n"
            "  cd <recipe-dir> && rm uv.lock && uv lock"
        ),
    )


def main() -> int:
    if len(sys.argv) != 2:
        return report_infra_fault(
            infra_fault(
                CHECKER,
                f"invoked with {len(sys.argv) - 1} argument(s); expected "
                f"exactly one path to a uv.lock.",
            )
        )
    try:
        return _run(sys.argv[1])
    except Exception as exc:
        return report_infra_fault(
            infra_fault(CHECKER, f"{type(exc).__name__}: {exc}")
        )


if __name__ == "__main__":
    # guard(): a traceback escaping to the runner is reported by the
    # workflow as the contributor's failure, which is exactly the confusion
    # infra_fault exists to prevent.
    sys.exit(guard(CHECKER, main))
