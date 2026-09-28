#!/usr/bin/env python3
"""Check a google/adk-samples recipe against the house rules in .github/review-rules.md.

    check_house_rules.py --repo-root /path/to/checkout --recipe contrib/python/foo
    check_house_rules.py --repo-root . --recipe contrib/python/foo --json

Emits findings in the same schema the analysis lanes use, with `rule`, `ci` and
`anchorable` pre-populated. Deterministic: no model, no judgement, no tokens.

Every reportable rule in `.github/review-rules.md` is either implemented here or
declared in MODEL_JUDGED below, and `tests/test_house_rules_drift.py` fails if a
rule is in neither. That test is why this list is short: seven reportable rules
(H39-H47) were in the doc, unimplemented, and NOT on any declared exception list,
so nothing was checking them and nothing said so.

Three rules here have traps that a naive grep gets wrong, and each is called out at
its implementation:

    H9   `source = { editable = "." }` is the recipe's OWN package, not a violation
    H15  needs package-root resolution among many __init__.py, and is a NEGATIVE
    H19  needs the real JSON schema; `license`/`tags` ARE permitted keys
"""

import argparse
import ast
import json
import os
import re
import subprocess
import sys

try:
    import tomllib
except ModuleNotFoundError:  # py<3.11
    import tomli as tomllib

CI_FAIL = "fail"
CI_ADV = "advisory"

# Reportable rules this script deliberately leaves to a model, and why. Anything
# in `.github/review-rules.md` under "Report these" that is neither implemented
# here nor listed here is an unchecked rule, and the drift test says so by name.
# A wrong "this fails CI" costs more than a missed nit, so the bar for adding a
# rule here rather than implementing it is "a regex gets it wrong in practice".
MODEL_JUDGED = {
    "H11": "hardcoded model literal — four AST exemptions (collection literal, "
    "subscript index, comparison operand, docstring)",
    "H16": "noqa E402 on a trailing import — needs 'after the first "
    "non-import statement', which the import graph alone does not give",
    "H25": "runnability assert placement — needs the `with patch(...)` block "
    "structure, not just the presence of an assert",
}

# Rules that could not be evaluated this run. Always reported -- a silent skip
# makes every "no violations" result a lie.
SKIPPED = []


def _scalar_value(raw):
    """The value part of a YAML scalar line, with any trailing comment removed.

    The manifest TEMPLATE ships every enum as `type: "standalone"  # Options:
    [standalone | module]`, so a fallback parser that keeps the whole remainder
    reads the value as `"standalone"  # Options: [standalone | module]` and H19
    then reports three confident CI-FAILs saying the enum is invalid. That is
    the most expensive comment this skill can produce, and it fired on
    contrib/python/clause-agent.
    """
    raw = raw.strip()
    q = re.match(r"""^(['"])(.*?)\1""", raw)
    if q:
        return q.group(2)
    # Unquoted: ` #` starts a comment, a bare `#` mid-token does not.
    return re.split(r"(?:^|\s)#", raw, maxsplit=1)[0].strip()


def _parse_manifest(text):
    """(data, degraded). Falls back to a top-level-key scan without pyyaml."""
    if not text:
        return {}, False
    try:
        import yaml

        return (yaml.safe_load(text) or {}), False
    except ModuleNotFoundError:
        pass
    except Exception:
        return None, False
    data = {}
    for line in text.split("\n"):
        if line.lstrip().startswith("#"):
            continue
        m = re.match(r"^([A-Za-z_][A-Za-z0-9_]*):\s*(.*)$", line)
        if not m:
            continue
        k, v = m.group(1), _scalar_value(m.group(2))
        data[k] = v if v else {}
    return data, True


# Rules that describe the recipe as a whole rather than a specific file. They are
# only the PR's responsibility when the PR is adding the recipe.
WHOLE_RECIPE_RULES = {"H21", "H22", "H23"}
# H24 is the exception: modifying ANY file under a frozen path is itself the
# violation, so a small edit to a legacy recipe does trigger it.
# H48 is the other: a junk ownership.team is the one thing nobody else catches
# (the schema only asks for a non-empty string), and letting it through once is
# what produced a fleet of recipes owned by "Google". It is always reported;
# when manifest.yaml is outside the diff the finding lands in the un-anchorable
# bucket rather than being dropped.
ALWAYS_RULES = {"H24", "H48"}

CHANGED = None  # set from --changed-files; None means "audit everything"
NEW_RECIPE = True  # set False when the PR only edits an existing recipe
FILTERED = []  # rules suppressed as pre-existing, for reporting


# Rules whose `path` names a DIRECTORY rather than a file: the recipe root, a
# pruned build directory, a misplaced vertical. `CHANGED` holds files, so a
# plain `path in CHANGED` is false for every one of them and the rule can
# never fire in CI. H41, H44 and H47 were implemented, tested, declared
# covered by the drift test -- and dead on arrival in their only production
# caller, which is a worse state than being unimplemented, because the drift
# test reports them as done.
DIRECTORY_RULES = {"H22", "H23", "H41", "H44", "H47"}


def _is_ours(rule, path):
    """Is this violation something the PR introduced, or pre-existing noise?"""
    if CHANGED is None:
        return True
    if rule in ALWAYS_RULES:
        return True
    # H22 and H23 sit in BOTH sets, and the two questions are independent:
    # "is this PR adding a recipe at all" and "is THIS the recipe it adds".
    # Returning on the first set made the second unaskable, so a PR that added
    # recipe A and touched one file in recipe B reported B's folder name and
    # skill depth as if the PR had authored them.
    if rule in WHOLE_RECIPE_RULES and not NEW_RECIPE:
        return False
    if rule in DIRECTORY_RULES:
        prefix = path.rstrip("/") + "/"
        return any(changed.startswith(prefix) for changed in CHANGED)
    if rule in WHOLE_RECIPE_RULES:
        return True
    return path in CHANGED


def fetch_changed_files(repo, pr):
    """Every changed path, paged.

    `gh pr view --json files` silently caps at 100. On PR #2302 (787 files) that
    truncation left pyproject.toml out of the list, so every finding in it was
    filtered away as "pre-existing" on a brand-new recipe.
    """
    out, page = set(), 1
    while True:
        proc = subprocess.run(
            [
                "gh",
                "api",
                f"/repos/{repo}/pulls/{pr}/files?per_page=100&page={page}",
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        if proc.returncode != 0:
            raise SystemExit(f"gh api failed: {proc.stderr.strip()[:200]}")
        batch = json.loads(proc.stdout or "[]")
        if not batch:
            break
        out.update(f["filename"] for f in batch)
        if len(batch) < 100:
            break
        page += 1
    return out


def find(out, rule, ci, path, line, what, evidence, verify):
    if not _is_ours(rule, path):
        FILTERED.append((rule, path))
        return
    out.append(
        {
            "rule": rule,
            "ci": ci,
            "path": path,
            "line": line,
            "severity": "no_critical",
            "confidence": "high",
            "cheap": "cheap",
            "what": what,
            "evidence": evidence,
            "verify_steps": verify,
            "window": "",
            "unchecked": None,
        }
    )


# GitHub accepts a 100 MB file, every reader here splits what it reads into
# lines (roughly doubling it), and the recipe subtree is walked six times. No
# rule needs more than this to decide anything.
MAX_READ_BYTES = 2_000_000


def read(p):
    """A file's text, or None if it cannot or should not be read.

    Guards OSError rather than just missing files: a dangling symlink, a
    permission error and a directory in place of a file all reach here, and
    any one of them escaping discards every finding for the whole recipe.
    """
    try:
        if os.path.getsize(p) > MAX_READ_BYTES:
            return None
        with open(p, encoding="utf-8", errors="replace") as handle:
            return handle.read()
    except OSError:
        return None


def lineno_of(text, pattern):
    for i, line in enumerate(text.split("\n"), 1):
        if re.search(pattern, line):
            return i
    return 1


def _table(data, key):
    """`data[key]` when it is a mapping, else an empty one."""
    value = data.get(key) if isinstance(data, dict) else None
    return value if isinstance(value, dict) else {}


def load_toml(p):
    try:
        with open(p, "rb") as fh:
            return tomllib.load(fh)
    except Exception:
        return None


# --------------------------------------------------------------------------- #

# --------------------------------------------------------------- H26 / H27

APACHE_HEADER = [
    "Copyright {year} Google LLC",
    "",
    'Licensed under the Apache License, Version 2.0 (the "License");',
    "you may not use this file except in compliance with the License.",
    "You may obtain a copy of the License at",
    "",
    "    https://www.apache.org/licenses/LICENSE-2.0",
    "",
    "Unless required by applicable law or agreed to in writing, software",
    'distributed under the License is distributed on an "AS IS" BASIS,',
    "WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.",
    "See the License for the specific language governing permissions and",
    "limitations under the License.",
]
HEADER_EXT = {
    ".py": "#",
    ".sh": "#",
    ".tf": "#",
    ".yaml": "#",
    ".yml": "#",
    ".ts": "//",
    ".tsx": "//",
    ".js": "//",
    ".jsx": "//",
}
# Source files are EXPECTED to carry the header, so a missing one is a violation
# however many others also lack it. Config formats only have to be self-consistent
# -- if no .tf in the recipe has a header, that is the convention there.
HEADER_REQUIRED = {".py", ".ts", ".tsx", ".js", ".jsx", ".sh"}
# Blank separator lines carry no text, so only the prose lines are matchable.
# Deriving this rather than hardcoding it: a literal 10 was unreachable against
# the 9 real lines, so every file scored "partial" and the check silently died.
_HEADER_LINES = [w for w in APACHE_HEADER[1:] if w]
_FULL_AT = len(_HEADER_LINES) - 1  # tolerate one reflowed line
_ENV_READERS = {"getenv", "environ"}


def _env_read_defaults(src, path):
    """[(line, call_text, why)] for env reads that carry a hardcoded default.

    AST, not regex: a regex flags the same call inside a docstring or a comment,
    and cannot tell `os.environ["X"]` (fine -- cannot carry a default) from
    `os.environ.get("X", "d")` (not fine).
    """
    try:
        tree = ast.parse(src)
    except (SyntaxError, ValueError, RecursionError, MemoryError):
        # RecursionError, not SyntaxError, is what a generated constant table
        # of 200k terms raises -- a plausible accident, not just an attack.
        # Anything escaping here discards the whole recipe's findings.
        return []

    def env_call(node):
        """('getenv'|'get'|'setdefault') if this Call reads the environment."""
        fn = node.func
        if not isinstance(fn, ast.Attribute):
            return None
        if (
            fn.attr == "getenv"
            and isinstance(fn.value, ast.Name)
            and fn.value.id == "os"
        ):
            return "getenv"
        if fn.attr in ("get", "setdefault"):
            v = fn.value
            if isinstance(v, ast.Attribute) and v.attr == "environ":
                return fn.attr
            if isinstance(v, ast.Name) and v.id == "environ":
                return fn.attr
        return None

    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            kind = env_call(node)
            if not kind:
                continue
            if len(node.args) >= 2 or any(
                k.arg == "default" for k in node.keywords
            ):
                out.append(
                    (
                        node.lineno,
                        kind,
                        "second argument is a hardcoded default",
                    )
                )
        # `os.getenv("X") or "fallback"` -- semantically a default.
        elif isinstance(node, ast.BoolOp) and isinstance(node.op, ast.Or):
            first = node.values[0]
            if isinstance(first, ast.Call) and env_call(first):
                rest = node.values[1:]
                if any(
                    isinstance(v, ast.Constant) and v.value not in (None, "")
                    for v in rest
                ):
                    out.append(
                        (node.lineno, "or", "`or` fallback after an env read")
                    )
    return out


def check_env_defaults(out, root, rel):
    """H26 -- an env read must not carry a hardcoded default."""
    recipe_abs = os.path.join(root, rel)
    hits = []
    for dirpath, dirnames, filenames in os.walk(recipe_abs):
        dirnames[:] = [
            d
            for d in dirnames
            if d not in ("__pycache__", ".venv", "node_modules", ".git")
        ]
        for fn in filenames:
            if not fn.endswith(".py"):
                continue
            fp = os.path.join(dirpath, fn)
            relp = os.path.relpath(fp, root)
            if CHANGED is not None and relp not in CHANGED:
                continue
            src = read(fp)
            if not src:
                continue
            for line, kind, why in _env_read_defaults(src, relp):
                hits.append((relp, line, kind, why))
    if not hits:
        return
    # One finding, not one per hit -- the grouping rule.
    relp, line, kind, why = hits[0]
    others = sorted({h[0] for h in hits})
    find(
        out,
        "H26",
        CI_ADV,
        relp,
        line,
        f"env read carries a hardcoded default ({why}); "
        f"{len(hits)} occurrence(s) across {len(others)} file(s). Defaults belong "
        "in .env.example, not in the code",
        "; ".join(f"{h[0]}:{h[1]}" for h in hits[:6]),
        f"read line {line}; the call has a second argument",
    )


def _header_state(text, marker):
    """('full'|'partial'|'none', matched_lines)."""
    lines = [ln.rstrip() for ln in text.split("\n")[:25]]
    body = []
    for ln in lines:
        s = ln.strip()
        if s.startswith(marker):
            body.append(s[len(marker) :].strip())
        elif not s and body:
            body.append("")
        elif body:
            break
    if not body:
        return "none", 0
    joined = " ".join(body)
    matched = sum(1 for want in _HEADER_LINES if want.strip()[:40] in joined)
    has_copyright = "Copyright" in joined and "Google LLC" in joined
    if matched >= _FULL_AT:
        return "full", matched
    if has_copyright or matched:
        return "partial", matched
    return "none", 0


def _anchor_in_diff(paths):
    """The first of these paths the PR actually changed, else the first.

    H27 counts the whole recipe -- it is a claim about the recipe's
    convention -- but `_is_ours` filters it on `path in CHANGED`. Anchoring
    on the first offender in WALK order therefore threw the finding away
    unless that exact file happened to be in the diff, which on a sixteen-file
    offender list is about a one-in-eight chance.
    """
    if CHANGED is None:
        return paths[0]
    return next((p for p in paths if p in CHANGED), paths[0])


def check_license_headers(out, root, rel):
    """H27 -- the licence header must be consistent WITHIN each file type.

    Compared per extension, not pooled. Every .tf file in this recipe lacks a
    header; that is the convention for terraform here, not 12 violations. Pooling
    them against 662 headed .py files produced exactly that false positive.
    """
    recipe_abs = os.path.join(root, rel)
    by_ext = {}
    for dirpath, dirnames, filenames in os.walk(recipe_abs):
        dirnames[:] = [
            d
            for d in dirnames
            if d
            not in (
                "__pycache__",
                ".venv",
                "node_modules",
                ".git",
                "dist",
                "build",
                ".next",
            )
        ]
        for fn in filenames:
            ext = os.path.splitext(fn)[1]
            marker = HEADER_EXT.get(ext)
            if not marker:
                continue
            fp = os.path.join(dirpath, fn)
            relp = os.path.relpath(fp, root)
            # NOT filtered by CHANGED here. H27 is a statement about the
            # recipe's convention -- "N differ from the M carrying the full
            # block" -- so the tally has to be over the whole recipe. Counting
            # only changed files made a PR that edits two of the sixteen
            # unheaded files in a 465-file recipe report "no .py file in this
            # recipe carries the standard Apache header", with 449 of them
            # carrying it. The filter chose the branch as well as the numbers.
            # _is_ours still decides whether the finding is reported.
            if fn.endswith((".gen.ts", ".gen.tsx")) or "generated" in relp:
                continue
            text = read(fp)
            if text is None or not text.strip():
                continue
            state, _ = _header_state(text, marker)
            by_ext.setdefault(ext, {"full": [], "partial": [], "none": []})
            by_ext[ext][state].append(relp)

    for ext, g in sorted(by_ext.items()):
        total = sum(len(v) for v in g.values())
        full = len(g["full"])
        if ext in HEADER_REQUIRED:
            if total < 2:
                continue  # a lone file establishes nothing
        # Config formats: consistency only. No convention, no finding.
        elif total < 5 or full == 0 or full / total < 0.6:
            continue
        dominant_full = full / total >= 0.6
        if g["partial"] or g["none"]:
            if dominant_full:
                # A convention exists and a minority breaks it.
                if g["partial"]:
                    find(
                        out,
                        "H27",
                        CI_ADV,
                        _anchor_in_diff(g["partial"]),
                        1,
                        f"licence header is truncated here; {len(g['partial'])} "
                        f"{ext} file(s) differ from the {full} carrying the full "
                        "Apache block",
                        "; ".join(g["partial"][:6]),
                        f"compare the first 15 lines of {g['partial'][0]} "
                        "with a sibling",
                    )
                if g["none"]:
                    find(
                        out,
                        "H27",
                        CI_ADV,
                        _anchor_in_diff(g["none"]),
                        1,
                        f"no licence header on this file; {len(g['none'])} {ext} "
                        f"file(s) have none while {full} carry the full block",
                        "; ".join(g["none"][:6]),
                        f"read the first 15 lines of {g['none'][0]}",
                    )
            elif full:
                # Two competing shapes. Say that, rather than implying the
                # majority is a broken version of the minority.
                find(
                    out,
                    "H27",
                    CI_ADV,
                    _anchor_in_diff(g["partial"] or g["none"]),
                    1,
                    f"the {ext} files carry two different headers: "
                    f"{len(g['partial'])} a shorter notice, {len(g['none'])} none, "
                    f"and {full} the full Apache block",
                    "; ".join((g["partial"] + g["none"])[:6]),
                    f"compare the first 15 lines of {(g['partial'] or g['none'])[0]} "
                    f"against {g['full'][0]}",
                )
            else:
                find(
                    out,
                    "H27",
                    CI_ADV,
                    _anchor_in_diff(g["partial"] or g["none"]),
                    1,
                    f"no {ext} file in this recipe carries the standard Apache "
                    f"header ({len(g['partial'])} have a shorter notice, "
                    f"{len(g['none'])} have none)",
                    "; ".join((g["partial"] + g["none"])[:6]),
                    f"read the first 15 lines of {(g['partial'] or g['none'])[0]}",
                )


# Roots whose recipe name is namespaced by the segment above it. Mirrors
# NAMESPACED_ROOTS in .github/scripts/check_recipe_pyproject.py, which is what
# actually fails the build.
_NAMESPACED_ROOTS = {"skills"}


def _expected_project_name(rel, recipe_name):
    """The [project].name CI will demand for this recipe."""
    parts = rel.strip("/").split("/")
    if len(parts) == 3 and parts[0] in _NAMESPACED_ROOTS:
        return f"{parts[1]}-{parts[2]}"
    return recipe_name


def check_pyproject(out, root, rel, recipe_name):
    p = os.path.join(root, rel, "pyproject.toml")
    text = read(p)
    if text is None:
        return
    r = os.path.join(rel, "pyproject.toml")
    data = load_toml(p) or {}
    # `.get(k, {})` is not enough: a key present with the WRONG type returns
    # that value, and every reader below then calls .get() on a str or a list.
    # A mistyped table is exactly what these rules exist to catch, so crashing
    # on one loses the recipe's whole review to the defect it was looking for.
    proj = _table(data, "project")

    # H1 -- ruff config belongs to the repo root only
    hits = [
        i
        for i, ln in enumerate(text.split("\n"), 1)
        if re.match(r"^\s*\[tool\.ruff(\.|\])", ln)
    ]
    if hits:
        find(
            out,
            "H1",
            CI_FAIL,
            r,
            hits[0],
            f"declares {len(hits)} [tool.ruff*] table(s); recipes must not "
            f"(lines {', '.join(map(str, hits))})",
            "AGENTS.md:50-52; python-validate-recipe.yml:261-266",
            "grep '^\\[tool\\.ruff' in this file",
        )

    # H3 -- [project].name. Under skills/ the expected value is
    # <vertical>-<solution>, not the bare basename: check_recipe_pyproject's
    # NAMESPACED_ROOTS namespaces that root. Comparing against the basename
    # fired on both shipped vertical skills and told each author to set the
    # one value CI would reject.
    name = proj.get("name")
    expected = _expected_project_name(rel, recipe_name)
    if name and isinstance(name, str) and name != expected:
        find(
            out,
            "H3",
            CI_FAIL,
            r,
            lineno_of(text, r"^\s*name\s*="),
            f'[project].name is "{name}"; this recipe\'s name is "{expected}"',
            "check_recipe_pyproject.py:182-215",
            f"compare name= against {expected}",
        )

    # H4 -- must accept 3.11 exactly. Specifier logic, not a string match.
    rp = proj.get("requires-python")
    ln = lineno_of(text, r"^\s*requires-python\s*=")
    if not rp:
        find(
            out,
            "H4",
            CI_FAIL,
            r,
            1,
            "no requires-python declared",
            "check_recipe_pyproject.py:117-197",
            "read [project]",
        )
    elif not isinstance(rp, str):
        find(
            out,
            "H4",
            CI_FAIL,
            r,
            ln,
            f"requires-python is {type(rp).__name__}, not a string; "
            "`requires-python = 3.11` without quotes is a float",
            "check_recipe_pyproject.py:117-197",
            "read the requires-python line",
        )
    else:
        bad = None
        for part in [s.strip() for s in rp.split(",")]:
            # Bounded: int() on a string of more than 4300 digits raises
            # ValueError, and a contributor could disable the deterministic
            # lane for their own recipe with one long line.
            m = re.match(r"(>=|>|~=|==)\s*3\.(\d{1,6})", part)
            if not m:
                continue
            op, minor = m.group(1), int(m.group(2))
            if op in (">=", "~=", "==") and minor < 11:
                bad = f"{rp} permits Python below 3.11"
            if op in (">=", "~=", "==") and minor > 11:
                bad = f"{rp} excludes Python 3.11 (CI pins 3.11 for uv lock --check)"
        if bad:
            find(
                out,
                "H4",
                CI_FAIL,
                r,
                ln,
                bad,
                "check_recipe_pyproject.py:117-197",
                "read the requires-python specifier",
            )

    # H5 -- [[tool.uv.index]] array-of-tables, default=true, public PyPI
    idx = _table(_table(data, "tool"), "uv").get("index")
    ok_urls = {"https://pypi.org/simple", "https://pypi.org/simple/"}
    if idx is None:
        find(
            out,
            "H5",
            CI_FAIL,
            r,
            1,
            "no [[tool.uv.index]] declared",
            "check_recipe_pyproject.py:262-340",
            "read [tool.uv]",
        )
    elif not isinstance(idx, list):
        # single-bracket [tool.uv.index] instead of array-of-tables
        find(
            out,
            "H5",
            CI_FAIL,
            r,
            lineno_of(text, r"tool\.uv\.index"),
            "[tool.uv.index] must be an array-of-tables [[tool.uv.index]]",
            "check_recipe_pyproject.py:298-308",
            "check the bracket count",
        )
    else:
        defaults = [
            e for e in idx if isinstance(e, dict) and e.get("default") is True
        ]
        if not defaults:
            find(
                out,
                "H5",
                CI_FAIL,
                r,
                lineno_of(text, r"tool\.uv\.index"),
                "no [[tool.uv.index]] entry has default = true",
                "check_recipe_pyproject.py:262-340",
                "read the index entries",
            )
        elif defaults[0].get("url") not in ok_urls:
            find(
                out,
                "H5",
                CI_FAIL,
                r,
                lineno_of(text, r"tool\.uv\.index"),
                f'default index is "{defaults[0].get("url")}", must be public PyPI',
                "check_recipe_pyproject.py:262-340",
                "read the default index url",
            )

    # H6 -- python-dotenv in [project].dependencies (dev group does NOT count)
    deps = proj.get("dependencies") or []
    if isinstance(deps, str) or not isinstance(deps, (list, tuple)):
        deps = []
    names = {
        re.match(r"^\s*([A-Za-z0-9_.\-]+)", d).group(1).lower()
        for d in deps
        # A dependency written as a table rather than a PEP 508 string is a
        # realistic mistake, and re.match on a dict raises TypeError.
        if isinstance(d, str) and re.match(r"^\s*([A-Za-z0-9_.\-]+)", d)
    }
    if "python-dotenv" not in names:
        find(
            out,
            "H6",
            CI_ADV,
            r,
            lineno_of(text, r"^\s*dependencies\s*="),
            "python-dotenv is not in [project].dependencies "
            "(a dev dependency-group does not count)",
            "extract_env_vars.py:1797-1839",
            "read [project].dependencies",
        )

    # H7 -- build-system with both keys
    bs = _table(data, "build-system")
    if not bs or not bs.get("requires") or not bs.get("build-backend"):
        find(
            out,
            "H7",
            CI_ADV,
            r,
            lineno_of(text, r"\[build-system\]"),
            "[build-system] missing or lacks requires / build-backend",
            "align_pyproject.py:667",
            "read [build-system]",
        )

    # H8 -- testpaths, if present, must collect the runnability test
    tp = _table(_table(_table(data, "tool"), "pytest"), "ini_options").get(
        "testpaths"
    )
    if tp:
        if isinstance(tp, str):
            entries = [tp]
        elif isinstance(tp, (list, tuple)):
            entries = [e for e in tp if isinstance(e, str)]
        else:
            entries = []
        ok = {"", ".", "tests", "tests/test_runnability.py"}
        if not any(e.strip().rstrip("/") in ok for e in entries):
            find(
                out,
                "H8",
                CI_ADV,
                r,
                lineno_of(text, r"testpaths"),
                f"testpaths {entries} never collects tests/test_runnability.py",
                "align_pyproject.py:1063-1121",
                "read testpaths",
            )


def check_uv_lock(out, root, rel, recipe_name, pyproject_name):
    """H9. TRAP: `source = { editable = "." }` is the recipe's OWN package."""
    p = os.path.join(root, rel, "uv.lock")
    text = read(p)
    if text is None:
        return
    r = os.path.join(rel, "uv.lock")
    own = {pyproject_name, recipe_name, recipe_name.replace("-", "_")}
    pkg = None
    for i, line in enumerate(text.split("\n"), 1):
        m = re.match(r'^\s*name\s*=\s*"([^"]+)"', line)
        if m:
            pkg = m.group(1)
            continue
        m = re.match(r"^\s*source\s*=\s*\{\s*(\w+)\s*=", line)
        if m and m.group(1) in ("git", "editable", "directory"):
            if m.group(1) in ("editable", "directory") and pkg in own:
                continue  # the recipe self-reference -- correct, not a defect
            find(
                out,
                "H9",
                CI_FAIL,
                r,
                i,
                f'package "{pkg}" uses a {m.group(1)} source; '
                "recipes must depend only on published PyPI releases",
                "python-dependency-policy.yml:224-303",
                f"read line {i}",
            )


def check_dotenv_bootstrap(out, root, rel):
    """H15. TRAP: needs package-root resolution, and asserts a NEGATIVE.

    The package root is the directory holding __init__.py that is a *direct child*
    of the recipe root -- not any of the nested ones, and not tests/ or eval/.
    """
    recipe_abs = os.path.join(root, rel)
    pkg_dirs = []
    try:
        for entry in sorted(os.listdir(recipe_abs)):
            d = os.path.join(recipe_abs, entry)
            if (
                os.path.isdir(d)
                and entry not in ("tests", "eval", "scripts", "docs")
                and not entry.startswith(".")
                and os.path.exists(os.path.join(d, "__init__.py"))
            ):
                pkg_dirs.append(entry)
    except OSError:
        return
    if not pkg_dirs:
        return  # no package -- H15 does not apply
    pkg = pkg_dirs[0]
    init_rel = os.path.join(rel, pkg, "__init__.py")
    init_text = read(os.path.join(root, init_rel)) or ""
    if "load_dotenv" in init_text:
        return  # compliant

    # Where IS it called? Reporting the absence alone is what got this wrong before.
    callers = []
    for dirpath, dirnames, filenames in os.walk(os.path.join(recipe_abs, pkg)):
        dirnames[:] = [d for d in dirnames if d not in ("__pycache__", ".venv")]
        for fn in filenames:
            if not fn.endswith(".py"):
                continue
            fp = os.path.join(dirpath, fn)
            t = read(fp) or ""
            if re.search(r"^\s*load_dotenv\s*\(", t, re.M):
                callers.append(os.path.relpath(fp, recipe_abs))
    if not callers:
        return  # reads no dotenv at all -- not this rule's business
    find(
        out,
        "H15",
        CI_ADV,
        init_rel,
        1,
        f"load_dotenv() is called from {len(callers)} module(s) "
        f"({', '.join(sorted(callers)[:4])}) but not from the package __init__.py",
        "docs/recipe-handbook/languages/python.md:107-110",
        f"grep load_dotenv in {pkg}/ and check {pkg}/__init__.py",
    )


# ------------------------------------------------------------------------ H48

# Values that name no owner. Normalised (lowercased, punctuation and a trailing
# "team"/"group" stripped) before lookup, so "Google, LLC" and "ADK Samples Team"
# both land here. Deliberately a closed list: an unfamiliar name like "OpenEAGO"
# or "attenu-io" is somebody's real org, and guessing "that looks like a handle"
# from its shape flags those far more often than it flags a cheat.
GENERIC_TEAMS = {
    "google",
    "google llc",
    "google inc",
    "google cloud",
    "google cloud platform",
    "googler",
    "googlers",
    "gcp",
    "alphabet",
    "adk",
    "adk samples",
    "adk-samples",
    "adk sample",
    "contrib",
    "open source",
    "opensource",
    "team",
    "the",
    "my",
    "our",
    "n/a",
    "na",
    "none",
    "null",
    "nil",
    "unknown",
    "tbd",
    "todo",
    "xxx",
    "placeholder",
    "test",
    "testing",
    "demo",
    "example",
    "self",
    "me",
    "myself",
    "personal",
    "individual",
    "independent",
    "solo",
    "misc",
    "other",
    "others",
}


def _norm_team(value):
    """Lowercase, de-punctuate, and drop a trailing team/group/org noun.

    The stripper is why the generic list must stay short: "Cloud Org" strips
    to "cloud", so putting a plausible team word on the list flags every real
    team whose name ends in it. Words removed for exactly that reason:
    DevRel, Community, Engineering, Cloud, Samples and their variants -- each
    of them names a real team somewhere, and one of them names a real team
    that owns five recipes in this repository.
    """
    v = re.sub(r"[^a-z0-9&/ -]", " ", str(value).lower())
    v = re.sub(r"\s+", " ", v).strip(" -&/")
    stripped = re.sub(
        r"\b(team|group|org|organisation|organization|llc|inc)\b\s*$", "", v
    ).strip(" -&/")
    # "Team" on its own strips to nothing; keep the original so it still matches.
    return re.sub(r"\s+", " ", stripped or v)


def _ownership(data, text):
    """(team, poc, [contributors]) with a regex fallback for degraded parses."""
    own = data.get("ownership")
    if isinstance(own, dict) and own:
        contrib = own.get("contributors")
        return (
            own.get("team"),
            own.get("poc"),
            contrib if isinstance(contrib, list) else [],
        )

    # Degraded parse (no pyyaml): the top-level scan yields `ownership: {}`, so
    # read the two scalars off the indented lines. A trailing `# comment` is part
    # of the line and not part of the value.
    def scalar(key):
        m = re.search(rf"^\s+{key}:[ \t]*(.+?)[ \t]*$", text, re.M)
        return _scalar_value(m.group(1)) or None if m else None

    return scalar("team"), scalar("poc"), []


def check_ownership_team(out, r, text, data):
    """H48 -- ownership.team must name a team, not an org or a person.

    The schema asks only for minLength 1, so "Google" and the author's own GitHub
    handle both sail through every deterministic check in the repo. Neither tells
    a future maintainer who to page.
    """
    team, poc, contributors = _ownership(data, text)
    if not team or not isinstance(team, str):
        return  # absent/typed wrong -- H19 and the schema
    raw = team.strip()
    if not raw:
        return
    # A live placeholder is H17's finding; two comments on one line is noise.
    if raw.upper().startswith("TODO") or "TODO:" in raw:
        return

    line = lineno_of(text, r"^\s+team:")
    norm = _norm_team(raw)
    handles = {
        str(h).strip().lstrip("@").lower() for h in [poc, *contributors] if h
    }
    verify = f"read the ownership.team value at line {line}"

    if norm in GENERIC_TEAMS:
        find(
            out,
            "H48",
            CI_ADV,
            r,
            line,
            f'ownership.team is "{raw}" -- that names an organisation, not a '
            "team. It must be the team that will maintain the recipe, specific "
            "enough that someone can find them",
            ".github/schemas/manifest-schema.json (ownership.team)",
            verify,
        )
        return

    if raw.strip().lstrip("@").lower() in handles:
        who = (
            "poc"
            if str(poc or "").strip().lstrip("@").lower()
            == raw.strip().lstrip("@").lower()
            else "a contributor"
        )
        find(
            out,
            "H48",
            CI_ADV,
            r,
            line,
            f'ownership.team and {who} are both "{raw}". One of the two is '
            "wrong: team is the owning team and poc is a person, so a single "
            "value cannot be both, and whichever one is the personal handle "
            "leaves the recipe unowned the moment that person moves",
            ".github/schemas/manifest-schema.json (ownership.team)",
            verify,
        )
        return

    # A group alias is not a person and does not leave when one does, which
    # is the exact failure this rule exists to prevent. Only a personal
    # address is a problem, and telling the two apart is not something to
    # guess at, so no email is reported.
    if re.search(r"https?://|github\.com/", raw):
        find(
            out,
            "H48",
            CI_ADV,
            r,
            line,
            f'ownership.team is "{raw}" -- a URL, not a team name',
            ".github/schemas/manifest-schema.json (ownership.team)",
            verify,
        )
        return

    # `isalnum()`, not an ASCII character class: [^A-Za-z0-9]+ matched any
    # name written wholly in a non-Latin script, so チーム, Команда and
    # 大数据平台组 were each reported as naming nobody.
    if len(raw) < 2 or not any(c.isalnum() for c in raw):
        find(
            out,
            "H48",
            CI_ADV,
            r,
            line,
            f'ownership.team is "{raw}", which names nobody',
            ".github/schemas/manifest-schema.json (ownership.team)",
            verify,
        )


def check_manifest(out, root, rel, schema_path):
    p = os.path.join(root, rel, "manifest.yaml")
    text = read(p)
    if text is None:
        return
    r = os.path.join(rel, "manifest.yaml")

    # H17 -- the two canonical placeholders. Never suggest a value for these.
    for ph in (
        "TODO: Replace with your team name",
        "TODO: Replace with your GitHub user ID",
    ):
        if ph in text:
            find(
                out,
                "H17",
                CI_FAIL,
                r,
                lineno_of(text, re.escape(ph)),
                f'ownership placeholder still present: "{ph}"',
                "validate_manifest.py:140-151",
                "grep the literal string",
            )

    # A manifest is flat enough that a missing pyyaml must not silently disable
    # H18/H19 -- a checker that quietly skips rules is worse than no checker.
    data, degraded = _parse_manifest(text)
    if data is not None and not isinstance(data, dict):
        # Anything that is not a mapping. Written as a blocklist of (list,
        # str, int, float, bool) it missed the rest of what yaml.safe_load
        # returns -- a bare date, a timestamp, !!set, !!binary -- and each of
        # those still reached .get() and discarded the recipe's whole review.
        # The question is "is it a mapping", so ask that.
        SKIPPED.append(
            (
                "H17/H18/H19/H48",
                f"manifest.yaml is a {type(data).__name__}, not a mapping",
            )
        )
        return
    if data is None:
        SKIPPED.append(("H18/H19", "could not parse manifest.yaml"))
        return
    if degraded:
        SKIPPED.append(
            ("H19-nested", "pyyaml missing; only top-level keys checked")
        )

    # H48 -- ownership.team. Runs before H18 so the ownership comment is first
    # in the manifest's findings.
    check_ownership_team(out, r, text, data)

    # H18 -- description
    raw_desc = data.get("description")
    # A YAML block scalar with nested keys parses to a dict, and an unquoted
    # year parses to an int. Both reached .strip().
    desc = raw_desc.strip() if isinstance(raw_desc, str) else ""
    if raw_desc is not None and not isinstance(raw_desc, str):
        find(
            out,
            "H18",
            CI_FAIL,
            r,
            lineno_of(text, r"^description:"),
            f"description is {type(raw_desc).__name__}, not a string",
            "validate_manifest.py:159-166",
            "read the description value",
        )
    if desc.upper().startswith("TODO") or len(desc) < 10:
        find(
            out,
            "H18",
            CI_FAIL,
            r,
            lineno_of(text, r"^description:"),
            "description is a TODO placeholder or shorter than 10 characters",
            "validate_manifest.py:159-166",
            "read the description value",
        )

    # H19 -- schema keys/enums. TRAP: license/tags/deployable/large ARE permitted.
    schema = None
    if schema_path and os.path.exists(schema_path):
        try:
            schema = json.load(open(schema_path))
        except Exception:
            schema = None
    if schema:
        allowed = set(schema.get("properties", {}))
        for raw_key in data:
            # str(): YAML turns `on:` into True, a bare year into an int and
            # `2026-01-01:` into a date, and re.escape on any of them raises
            # TypeError -- which the lane catches per recipe, discarding every
            # finding for it and reporting the PR clean. The key IS invalid
            # under additionalProperties: false, so the rule should say so
            # rather than die on it.
            k = str(raw_key)
            if k not in allowed:
                find(
                    out,
                    "H19",
                    CI_FAIL,
                    r,
                    lineno_of(text, rf"^{re.escape(k)}:"),
                    f'"{k}" is not a key in manifest-schema.json',
                    "manifest-schema.json (additionalProperties: false)",
                    "compare keys against the schema",
                )
        for k, spec in schema.get("properties", {}).items():
            if k in data and "enum" in spec and data[k] not in spec["enum"]:
                find(
                    out,
                    "H19",
                    CI_FAIL,
                    r,
                    lineno_of(text, rf"^{re.escape(k)}:"),
                    f'{k} = "{data[k]}" is not one of {spec["enum"]}',
                    "manifest-schema.json",
                    "compare against the enum",
                )


def check_readme(out, root, rel):
    p = os.path.join(root, rel, "README.md")
    text = read(p)
    if text is None:
        return
    r = os.path.join(rel, "README.md")
    words = len(text.split())
    if words < 100:
        find(
            out,
            "H20",
            CI_FAIL,
            r,
            1,
            f"README is {words} words, minimum is 100",
            "validate_readme.py:42",
            "word-count the file",
        )
    if "TODO:" in text:
        find(
            out,
            "H20",
            CI_FAIL,
            r,
            lineno_of(text, r"TODO:"),
            "README still contains TODO: text",
            "validate_readme.py:108",
            "grep TODO:",
        )
    if not re.search(r"^```", text, re.M):
        find(
            out,
            "H20",
            CI_FAIL,
            r,
            1,
            "README has no fenced code block",
            "validate_readme.py:132",
            "grep for a ``` fence",
        )
    heads = re.findall(r"^#+ .*$", text, re.M)
    setup = r"setup|prerequisit|installation|install|requirement|configuration|getting started|before you begin|environment"
    run = (
        r"\brun\b|running|usage|quickstart|quick start|\bstart\b|deploy|launch"
    )
    if not any(re.search(setup, h, re.I) for h in heads):
        find(
            out,
            "H20",
            CI_FAIL,
            r,
            1,
            "README has no setup/prerequisites heading",
            "validate_readme.py:116",
            "scan the headings",
        )
    if not any(re.search(run, h, re.I) for h in heads):
        find(
            out,
            "H20",
            CI_FAIL,
            r,
            1,
            "README has no run/usage heading",
            "validate_readme.py:124",
            "scan the headings",
        )


LANGUAGE_DIRS = {"python", "java", "go", "kotlin", "typescript"}

# Basenames validate_structure prunes. A recipe directory with one of these
# names is not "badly named" -- it is unvalidated.
PRUNED_DIR_NAMES = {
    "bin",
    "build",
    "dist",
    "vendor",
    "target",
    "out",
    "coverage",
    ".cache",
}

# Committed files nothing else reports, because the size checks exempt them.
# Deliberately narrow: `key.json` and `token.json` are common test fixtures, and
# a false "you committed a credential" is an alarming comment to get wrong.
_JUNK_EXACT = {
    ".env": "a committed .env; secrets belong in the environment, and "
    ".env.example is the file that ships",
    ".DS_Store": "macOS directory metadata, committed by accident",
    "credentials.json": "a committed credentials file",
    "client_secret.json": "a committed OAuth client secret",
}
# `.pem` alone is NOT here. A public CA bundle (certs/server-ca.pem is the
# documented Cloud SQL proxy fixture) and a test certificate are both ordinary
# committed files, and "you committed a private key" is the most alarming
# thing this checker can say. Only names that say PRIVATE KEY on their face.
_JUNK_SUFFIX = {
    "-key.pem": "a committed private key",
    "_key.pem": "a committed private key",
    "privkey.pem": "a committed private key",
    "private.pem": "a committed private key",
    ".pfx": "a committed key store",
    ".p12": "a committed key store",
    ".pyc": "a compiled artefact, not source",
}


def _junk_file(fn):
    """Why this basename should not be in the repo, or None."""
    if fn in _JUNK_EXACT:
        return _JUNK_EXACT[fn]
    # `.env.local.example` and `.env.test.sample` are examples too -- match the
    # suffix, not an allowlist of the three spellings someone thought of.
    if fn.startswith(".env.") and not re.search(
        r"\.(example|sample|template|dist)$", fn
    ):
        return f"{fn} is a real environment file; only .env.example ships"
    for suffix, why in _JUNK_SUFFIX.items():
        if fn.endswith(suffix):
            return why
    # A template or an example is the file a recipe SHOULD ship. The .env
    # branch above already knew that; this one did not, so
    # service-account-template.json was reported as a committed key.
    if re.fullmatch(r"service[-_]account.*\.json", fn) and not re.search(
        r"(example|sample|template|fake|dummy|test)", fn
    ):
        return "a committed service-account key"
    return None


# H43 is a claim about what is IN THE REPO, and the filesystem cannot answer
# that: a developer who ran the recipe has a .env and a .DS_Store sitting in
# the tree, both gitignored. On this repo, checking the filesystem produced 17
# "you committed a .env" findings and the true count of tracked ones is zero.
# There is no more alarming comment to get wrong.
#
# Keyed by (root, rel), not one bare set: the CI lane checks every recipe a PR
# touches in ONE process, and a single cached set would answer the second
# recipe with the first recipe's files.
_TRACKED = {}


def _tracked_files(root, rel):
    """The set of git-tracked paths under the recipe, or None if git cannot say."""
    key = (root, rel)
    if key not in _TRACKED:
        proc = subprocess.run(
            ["git", "-C", root, "ls-files", "-z", "--", rel],
            capture_output=True,
            text=True,
            check=False,
        )
        _TRACKED[key] = (
            None
            if proc.returncode != 0
            else {p for p in proc.stdout.split("\0") if p}
        )
    return _TRACKED[key]


def _manifest_language(root, rel):
    """manifest.language, or None. Works with or without pyyaml."""
    text = read(os.path.join(root, rel, "manifest.yaml"))
    if not text:
        return None
    data, _ = _parse_manifest(text)
    if isinstance(data, dict) and isinstance(data.get("language"), str):
        return data["language"].strip()
    m = re.search(r"^language:\s*(.+?)\s*$", text, re.M)
    return _scalar_value(m.group(1)) if m else None


def check_pr_shape(out, root, rel):
    """H42 -- a PR must not mix repo-skill changes with recipe changes.

    Needs the changed-file list; with no --changed-files/--pr there is no PR to
    have a shape, so this is silently not applicable rather than skipped.

    A property of the PR, not of a recipe, so it is emitted ONCE however many
    recipes the run covers. Called per recipe, it produced N identical
    comments on one line -- and at exactly three recipes the grouping pass
    collapsed them into "the same thing in 2 other places in this PR", which
    is false: it is the same place, three times.
    """
    if CHANGED is None:
        return
    if any(f["rule"] == "H42" for f in out):
        return
    skill_files = sorted(c for c in CHANGED if c.startswith(".agents/skills/"))
    recipe_files = sorted(
        c for c in CHANGED if c.startswith(("core/", "contrib/", "skills/"))
    )
    if not skill_files or not recipe_files:
        return
    # Anchor inside the recipe, not the skill: the recipe half is what the
    # author is here for, and it is the half a maintainer will be reading.
    # Prefer this recipe's own manifest: the finding is anchored somewhere a
    # maintainer will be reading, and `recipe_files[0]` could be a file that
    # merely sorts first, such as contrib/README.md.
    anchor = next(
        (
            c
            for c in recipe_files
            if c.startswith(rel + "/") and c.endswith("manifest.yaml")
        ),
        next(
            (c for c in recipe_files if c.endswith("manifest.yaml")),
            recipe_files[0],
        ),
    )
    find(
        out,
        "H42",
        CI_ADV,
        anchor,
        1,
        f"this PR changes {len(skill_files)} file(s) under .agents/skills/ "
        f"alongside {len(recipe_files)} recipe file(s). Repo skills and "
        "recipes are separate concerns and belong in separate PRs",
        "AGENTS.md:14",
        f"compare {skill_files[0]} against {anchor}",
    )


def _rule_sources(root):
    """Which rule definitions actually exist in the tree under review.

    The checker must not apply a rule whose source is absent. PR #1994's head
    predates AGENTS.md and .github/policy.yml entirely, so reporting "deprecated
    model" or "frozen path" against it applies rules that did not exist when the
    branch was written. Absent source -> skip and say so.
    """
    return {
        "agents_md": os.path.exists(os.path.join(root, "AGENTS.md")),
        "policy": os.path.exists(os.path.join(root, ".github/policy.yml")),
    }


# Mirrors .github/policy.yml `required_files`. Read from policy.yml when it is
# present in the tree under review, so the two cannot drift; these are the
# fallback for a checkout that predates a key.
_REQUIRED_ALWAYS = ["README.md"]
_REQUIRED_BY_ROOT = {
    "core": ["AGENTS.md"],
    "contrib": [],
    "skills": ["SKILL.md", "EVAL.yaml"],
}
_REQUIRED_BY_LANGUAGE = {
    "python": [
        "pyproject.toml",
        "uv.lock",
        ".env.example",
        "tests/test_runnability.py",
    ],
    "go": ["go.mod"],
}


def _case_insensitive_files(root):
    """Names policy.yml says CI accepts in any case (today: EVAL.yaml)."""
    for base in (_OWN_REPO, root):
        path = os.path.join(base, ".github/policy.yml")
        if not os.path.exists(path):
            continue
        try:
            import yaml

            with open(path, "rb") as handle:
                names = (yaml.safe_load(handle) or {}).get(
                    "case_insensitive_files"
                )
        except Exception:
            continue
        if isinstance(names, list):
            return {str(n).lower() for n in names}
    return {"eval.yaml"}


def _missing(recipe_abs, rel_name, lenient):
    """Is this required file absent?

    `eval.yaml` satisfies `EVAL.yaml`: validate_structure.py reads
    policy.case_insensitive_files and accepts either spelling, and the lane
    runs on a case-sensitive filesystem where a bare os.path.exists does not.
    A false "required file missing" on a file that is right there is the most
    confusing comment this rule can produce.
    """
    if os.path.exists(os.path.join(recipe_abs, rel_name)):
        return False
    if rel_name.lower() not in lenient:
        return True
    directory, base = os.path.split(os.path.join(recipe_abs, rel_name))
    try:
        return base.lower() not in {e.lower() for e in os.listdir(directory)}
    except OSError:
        return True


def _required_files(root, rel, recipe_abs):
    """The files THIS recipe must have: always + by root + by its language.

    A recipe's language comes from its manifest, not its path: under skills/
    the middle folder is a vertical, so the path cannot say.
    """
    policy = _load_policy_required_files(root)
    always = policy.get("always", _REQUIRED_ALWAYS)
    by_root = policy.get("by_root", _REQUIRED_BY_ROOT)
    by_language = policy.get("by_language", _REQUIRED_BY_LANGUAGE)

    area = rel.strip("/").split("/")[0]
    required = list(always) + list(by_root.get(area) or [])

    language = (_manifest_language(root, rel) or "").strip().lower()
    if not language:
        parts = rel.strip("/").split("/")
        if area in ("core", "contrib") and len(parts) >= 2:
            language = parts[1].lower()
    required += list(by_language.get(language) or [])
    return sorted(set(required))


# This file lives at <repo>/.agents/skills/github-pr-review/scripts/, so the
# repository holding it is four levels up. That repository is the BASE
# checkout when the CI lane runs, which is the whole point: policy.yml decides
# what a recipe must contain, so reading it out of the tree under review would
# let a PR edit the policy that judges it. One line added to its own
# policy.yml would turn H21 from five CI-FAILs into none.
_OWN_REPO = os.path.dirname(os.path.abspath(__file__))
for _ in range(4):
    _OWN_REPO = os.path.dirname(_OWN_REPO)


def _load_policy_required_files(root):
    """`required_files` from the checker's OWN repository, or {}.

    `root` is consulted only when this script runs standalone against a tree
    that is not its own — a developer pointing it somewhere else — and never
    in preference to the base copy.
    """
    for base in (_OWN_REPO, root):
        path = os.path.join(base, ".github/policy.yml")
        if not os.path.exists(path):
            continue
        try:
            import yaml

            with open(path, "rb") as handle:
                section = (yaml.safe_load(handle) or {}).get("required_files")
        except Exception:
            continue
        if not isinstance(section, dict):
            continue
        # Each key by its OWN expected type. `isinstance(v, (list, dict))`
        # let `by_root:` written as a list through, and _required_files then
        # called .get() on it -- crashing the rule that exists to catch
        # exactly that kind of mistake, and taking the recipe's whole review
        # with it.
        clean = {}
        if isinstance(section.get("always"), list):
            clean["always"] = section["always"]
        for key in ("by_root", "by_language"):
            value = section.get(key)
            if isinstance(value, dict):
                clean[key] = {
                    k: v for k, v in value.items() if isinstance(v, list)
                }
        if base is not _OWN_REPO:
            # Reached only when the checker lives outside a repository -- the
            # skill documents installing it in ~/.agents/skills. The tree
            # under review is then the only policy available, and a PR can
            # edit it, so the reader deserves to know.
            SKIPPED.append(
                (
                    "H21",
                    "required_files came from the tree under review, not from "
                    "a base checkout; a PR can edit that file",
                )
            )
        return clean
    return {}


def check_layout(out, root, rel, recipe_name):
    recipe_abs = os.path.join(root, rel)
    src = _rule_sources(root)

    # H2 -- standalone ruff config anywhere in the subtree
    for dirpath, dirnames, filenames in os.walk(recipe_abs):
        dirnames[:] = [
            d
            for d in dirnames
            if d not in ("__pycache__", ".venv", "node_modules")
        ]
        for fn in filenames:
            if fn in ("ruff.toml", ".ruff.toml"):
                rp = os.path.relpath(os.path.join(dirpath, fn), root)
                find(
                    out,
                    "H2",
                    CI_FAIL,
                    rp,
                    1,
                    "standalone ruff config in a recipe; config lives in the repo root",
                    "python-validate-recipe.yml:268-278",
                    "check the file exists",
                )

    # H21 -- required files, SCOPED BY LANGUAGE AND ROOT.
    #
    # This used to hardcode the Python list for every recipe in every
    # language, so a new TypeScript or Kotlin recipe collected four confident
    # CI-FAIL comments demanding a pyproject.toml, a uv.lock, an .env.example
    # and a pytest file it should never have -- the failure mode this whole
    # lane exists to avoid, produced deterministically on every non-Python
    # recipe. policy.yml has scoped these under `by_language` all along.
    lenient = _case_insensitive_files(root)
    for f in _required_files(root, rel, recipe_abs):
        if _missing(recipe_abs, f, lenient):
            find(
                out,
                "H21",
                CI_FAIL,
                os.path.join(rel, f),
                1,
                f"required file missing: {f}",
                ".github/policy.yml required_files",
                "check the file exists",
            )

    # H22 -- folder name
    if not re.fullmatch(r"[a-z][a-z-]*", recipe_name):
        find(
            out,
            "H22",
            CI_FAIL,
            rel,
            1,
            f'folder name "{recipe_name}" must match ^[a-z][a-z-]*$',
            "validate_structure.py:84",
            "read the directory name",
        )
    elif recipe_name.endswith("-"):
        find(
            out,
            "H22",
            CI_ADV,
            rel,
            1,
            f'folder name "{recipe_name}" ends with a hyphen '
            "(CI permits it, the scaffolder rejects it)",
            "scaffold.py:25",
            "read the directory name",
        )
    if len(recipe_name) > 30:
        find(
            out,
            "H22",
            CI_FAIL,
            rel,
            1,
            f"folder name is {len(recipe_name)} chars, max is 30",
            ".github/policy.yml max_folder_name_length",
            "count the characters",
        )

    # H23 / H41 / H47 -- skills/<vertical>/<solution>. One condition, three
    # different consequences; emit exactly one, or a misplaced skill collects
    # three comments saying the same thing in different words.
    parts = rel.strip("/").split("/")
    if parts[0] == "skills":
        if len(parts) == 2:
            # H41: the depth CI checks and the depth the Python validation
            # matrix reads are different things. This passes and is unvalidated.
            find(
                out,
                "H41",
                CI_ADV,
                rel,
                1,
                f"a solution directly under skills/ ({rel}) gets no per-recipe "
                "Python validation at all -- it must be "
                "skills/<vertical>/<solution>/",
                ".github/scripts/recipe_manifests.py",
                "count the path segments",
            )
        elif len(parts) != 3:
            find(
                out,
                "H23",
                CI_FAIL,
                rel,
                1,
                f"skills recipes must be at skills/<vertical>/<solution>/, got {rel}",
                "validate_placement.py:51-92",
                "count the path segments",
            )
        elif parts[1] in LANGUAGE_DIRS | {"js", "javascript", "ts"}:
            # H47: right depth, so CI passes it. Wrong meaning.
            find(
                out,
                "H47",
                CI_ADV,
                rel,
                1,
                f'"{parts[1]}" is a language, but that folder is the VERTICAL '
                "(retail, finance, healthcare). The language comes from "
                "manifest.language",
                "tools/validate_placement.py:67",
                "read the middle path segment",
            )

    # H24 -- frozen legacy roots. Only meaningful if policy.yml declares them.
    if not src["policy"]:
        SKIPPED.append(
            (
                "H24",
                "no .github/policy.yml in the reviewed tree; "
                "frozen paths undefined here",
            )
        )
    elif re.match(r"^(python|java|go|kotlin|typescript)/agents/", rel):
        find(
            out,
            "H24",
            CI_FAIL,
            rel,
            1,
            f"{rel} is under a frozen legacy path; use contrib/ or core/",
            ".github/policy.yml frozen_paths",
            "read the path",
        )

    # H44 -- a directory whose basename validation silently prunes. Everything
    # inside it is invisible to every check in the repo, which is a far worse
    # outcome than a badly named folder.
    # From git, not the filesystem — the same mistake H43 was rewritten to
    # fix, twenty lines away. dist/, build/, out/ and coverage/ are exactly
    # what a local build leaves behind and .gitignore hides, so on a
    # developer's own tree (SKILL.md invokes this checker there) every one of
    # them was a finding.
    tracked_dirs = set()
    for rp in _tracked_files(root, rel) or ():
        parts = os.path.dirname(rp).split("/")
        for i, part in enumerate(parts):
            if part in PRUNED_DIR_NAMES:
                tracked_dirs.add("/".join(parts[: i + 1]))
    for rp in sorted(tracked_dirs):
        d = rp.rsplit("/", 1)[-1]
        find(
            out,
            "H44",
            CI_ADV,
            rp,
            1,
            f'"{d}/" is silently pruned from validation, so nothing inside '
            "it is checked by anything. Rename it",
            ".github/policy.yml:103-121",
            "read the directory name",
        )

    # H43 -- a file that should never be committed. Size-exempt, so no other
    # check reports them. TRACKED files only; see _tracked_files.
    junk: list[tuple[str, str]] = []
    tracked = _tracked_files(root, rel)
    if tracked is None:
        SKIPPED.append(
            (
                "H43",
                "git could not list tracked files here; refusing "
                "to call an untracked .env 'committed'",
            )
        )
    else:
        for rp in sorted(tracked):
            head, fn = os.path.split(rp)
            why = _junk_file(fn)
            if why:
                junk.append((rp, why))
            elif ".idea" in head.split("/"):
                # .vscode is deliberately excluded: `launch.json` and
                # `extensions.json` are routinely committed as recommended
                # project configuration, and calling that "editor state
                # committed by accident" is wrong more often than it is right.
                junk.append((rp, "JetBrains project state, not project config"))

    # ONE finding with a count, like H10 and H26. A recipe with twelve stray
    # files should not collect twelve comments.
    if junk:
        rp, why = junk[0]
        extra = (
            f"; {len(junk)} such file(s) are tracked here"
            if len(junk) > 1
            else ""
        )
        find(
            out,
            "H43",
            CI_ADV,
            rp,
            1,
            f"{why}{extra}",
            ".gitignore",
            "check the file is tracked: git ls-files -- " + rp,
        )

    # H40 -- manifest.language must agree with the path. The two consumers
    # resolve it differently and Python validation is skipped entirely when
    # they disagree, so the recipe looks green while nothing has run.
    # Only when the path segment IS a language directory: a recipe at
    # core/<name> with no language segment is a placement problem, and calling
    # it a language mismatch describes the wrong defect.
    if (
        parts[0] in ("core", "contrib")
        and len(parts) >= 2
        and parts[1] in LANGUAGE_DIRS
    ):
        declared = _manifest_language(root, rel)
        if declared and declared.lower() != parts[1].lower():
            find(
                out,
                "H40",
                CI_ADV,
                os.path.join(rel, "manifest.yaml"),
                1,
                f'manifest.language is "{declared}" but the recipe sits under '
                f"{parts[0]}/{parts[1]}/. The path and the manifest disagree, "
                "and per-language validation follows the path",
                "tools/validate_structure.py",
                "compare the two",
            )


# H39. A value that reads as real and is not. Exact matches only, lowercased:
# a substring rule flags `PROJECT_ID_HELP_URL=https://example.com/docs`, and a
# shape rule flags every legitimately short value.
_STUB_VALUES = {
    "your-project-id",
    "my-project-id",
    "your-project",
    "my-project",
    "project-id",
    "your_project_id",
    "my_project_id",
    "sample-project",
    "changeme",
    "change-me",
    "change_me",
    "replace-me",
    "replaceme",
    "example.com",
    "www.example.com",
    "https://example.com",
    "http://example.com",
    "user@example.com",
    "foo@example.com",
    "foo",
    "bar",
    "baz",
    "foobar",
    "qux",
    "asdf",
    "test123",
    "adk samples team",
    "your-team",
    "my-team",
    "your-name",
    "my-name",
    "your-api-key",
    "my-api-key",
    "api-key-here",
    "insert-key-here",
    "your-bucket",
    "my-bucket",
    "bucket-name",
    "your-region",
    "us-central1-placeholder",
    "0000000000",
    "1234567890",
}


def _looks_like_a_stub(val):
    v = val.strip().strip("\"'").lower()
    return bool(v) and v in _STUB_VALUES


# A line that names a deprecated model in order to BAN it is documentation,
# not a use. Two recipes in this repo carry exactly that in their own
# AGENTS.md -- "don't use deprecated ones (`gemini-2.0-flash`,
# `gemini-2.5-flash`)" -- and reporting it is a confidently wrong comment
# from the one lane whose reason for existing is that it cannot produce one.
_PROHIBITION = re.compile(
    r"\b(deprecated|do not|don't|dont|never|avoid|instead of|no longer|"
    r"rather than|forbidden|banned|not use)\b",
    re.IGNORECASE,
)


def _forbids_rather_than_uses(line):
    return bool(_PROHIBITION.search(line))


def check_text_wide(out, root, rel):
    """H10, H13, H14 and H39 -- literal scans across the recipe."""
    recipe_abs = os.path.join(root, rel)
    if not os.path.exists(os.path.join(root, "AGENTS.md")):
        SKIPPED.append(
            (
                "H10",
                "no AGENTS.md in the reviewed tree; the deprecated "
                "model list is undefined here",
            )
        )
        banned = None
    else:
        # The negative lookahead is the whole point: `gemini-2.5-flash-image`
        # is a CURRENT model and `gemini-2.5-flash-lite` another, and a
        # prefix match told the author of skills/retail/virtual-tryon to
        # replace a correct image model with a text one, 21 times. A trailing
        # `-` followed by a letter starts a different model name; a digit
        # (`-001`) is a version pin of the same deprecated one, so that still
        # matches.
        banned = re.compile(r"gemini-2\.[05]-flash(?!-[a-zA-Z])")
    hits = []
    for dirpath, dirnames, filenames in os.walk(recipe_abs) if banned else []:
        dirnames[:] = [
            d
            for d in dirnames
            if d not in ("__pycache__", ".venv", "node_modules", ".git")
        ]
        for fn in filenames:
            if fn in ("uv.lock", "poetry.lock"):
                continue
            fp = os.path.join(dirpath, fn)
            try:
                if os.path.getsize(fp) > MAX_READ_BYTES:
                    continue
            except OSError:
                # A dangling symlink is trivially committable -- git stores
                # mode 120000 and never checks the target. Unguarded, the
                # FileNotFoundError escapes to the lane, which drops EVERY
                # finding for this recipe and reports it as clean. One file
                # would evade the entire deterministic review.
                continue
            t = read(fp)
            if not t:
                continue
            for i, line in enumerate(t.split("\n"), 1):
                if banned.search(line) and not _forbids_rather_than_uses(line):
                    hits.append((os.path.relpath(fp, root), i))
    # Sorted, so the same pull request always produces the same comment.
    # os.walk yields filesystem order, not alphabetical, so which file the
    # finding anchored on varied with the order the files happened to be
    # created in.
    hits.sort()
    if hits:
        # ONE finding, not one per hit -- anchored on a file the PR touched,
        # like every other whole-recipe rule. Anchored on the first hit in
        # walk order, _is_ours dropped it whenever an earlier file happened
        # to mention a banned id, which a recipe's own AGENTS.md often does.
        anchored = _anchor_in_diff([h[0] for h in hits])
        p, first_line = next((h for h in hits if h[0] == anchored), hits[0])
        find(
            out,
            "H10",
            CI_ADV,
            p,
            first_line,
            f"deprecated model id (use gemini-3.5-flash); {len(hits)} occurrence(s) "
            f"across {len({h[0] for h in hits})} file(s)",
            "AGENTS.md:40",
            "grep gemini-2.0-flash / gemini-2.5-flash",
        )

    envx = os.path.join(recipe_abs, ".env.example")
    t = read(envx)
    if t:
        for i, line in enumerate(t.split("\n"), 1):
            m = re.match(
                r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$", line
            )
            if not m:
                continue
            # _scalar_value, not split("#"): `NAME="foo # bar"` is a value
            # containing a hash, not a value plus a comment. Splitting it
            # produced '"foo" is a stub committed as if it were a real value',
            # stray quote included.
            var, val = m.group(1), _scalar_value(m.group(2))
            if var != var.upper():
                find(
                    out,
                    "H13",
                    CI_FAIL,
                    os.path.join(rel, ".env.example"),
                    i,
                    f'"{var}" is not UPPER_SNAKE_CASE',
                    "extract_env_vars.py:444",
                    f"read line {i}",
                )
            if val.lower() in (
                "<changeme>",
                "changeme",
                "todo",
                "<todo>",
                "xxx",
            ):
                find(
                    out,
                    "H14",
                    CI_ADV,
                    os.path.join(rel, ".env.example"),
                    i,
                    'placeholder should be the exact string "<TODO: update-this-value>"',
                    "extract_env_vars.py:91",
                    f"read line {i}",
                )
            elif _looks_like_a_stub(val):
                # H39. Distinct from H14: this one does NOT look like a
                # placeholder, so a reader copying .env.example gets a value
                # that is syntactically fine and simply wrong.
                find(
                    out,
                    "H39",
                    CI_ADV,
                    os.path.join(rel, ".env.example"),
                    i,
                    f'"{val}" is a stub committed as if it were a real value. '
                    "Someone copying this file has no way to tell it needs "
                    "replacing; use <TODO: update-this-value>",
                    ".agents/skills/extract-python-environment-variables/",
                    f"read line {i}",
                )


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo-root", required=True)
    ap.add_argument(
        "--recipe", required=True, help="path relative to repo root"
    )
    ap.add_argument(
        "--schema",
        default=None,
        help="path to manifest-schema.json (defaults to <root>/.github/schemas/)",
    )
    ap.add_argument(
        "--repo",
        help="owner/name; with --pr, fetches the changed "
        "files itself (paged) so nothing is truncated",
    )
    ap.add_argument("--pr", help="PR number; use with --repo")
    ap.add_argument(
        "--changed-files",
        help="override: file with one PR-changed path per line. Without "
        "this or --repo/--pr the whole recipe is audited, which is "
        "right for a NEW recipe and wrong for a small edit.",
    )
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()

    root = os.path.abspath(os.path.expanduser(args.repo_root))
    rel = args.recipe.strip("/")
    if not os.path.isdir(os.path.join(root, rel)):
        sys.exit(f"not a directory: {os.path.join(root, rel)}")
    recipe_name = os.path.basename(rel)
    schema = args.schema or os.path.join(
        root, ".github/schemas/manifest-schema.json"
    )

    global CHANGED, NEW_RECIPE
    if args.changed_files:
        CHANGED = {ln.strip() for ln in open(args.changed_files) if ln.strip()}
    elif args.repo and args.pr:
        CHANGED = fetch_changed_files(args.repo, args.pr)
    if CHANGED is not None:
        # A PR that adds the recipe's manifest or pyproject is creating it.
        NEW_RECIPE = any(
            c.startswith(rel + "/")
            and c.endswith(("manifest.yaml", "pyproject.toml"))
            for c in CHANGED
        )

    pj = load_toml(os.path.join(root, rel, "pyproject.toml")) or {}
    pj_name = _table(pj, "project").get("name", "")

    out = []
    check_pyproject(out, root, rel, recipe_name)
    check_uv_lock(out, root, rel, recipe_name, pj_name)
    check_dotenv_bootstrap(out, root, rel)
    check_manifest(out, root, rel, schema)
    check_readme(out, root, rel)
    check_layout(out, root, rel, recipe_name)
    check_text_wide(out, root, rel)
    check_env_defaults(out, root, rel)
    check_license_headers(out, root, rel)
    check_pr_shape(out, root, rel)

    out.sort(key=lambda f: (f["ci"] != "fail", f["rule"], f["path"]))

    if args.json:
        print(json.dumps({"findings": out, "skipped": SKIPPED}, indent=1))
    else:
        nf = sum(1 for f in out if f["ci"] == "fail")
        print(
            f"{rel}: {len(out)} finding(s), {nf} CI-failing"
            if out
            else f"{rel}: no house-rule violations"
        )
        if out:
            print()
        for f in out:
            tag = "FAIL" if f["ci"] == "fail" else "adv "
            print(f"  [{tag}] {f['rule']:<4} {f['path']}:{f['line']}")
            print(f"         {f['what']}")
        if FILTERED:
            from collections import Counter

            c = Counter(r for r, _ in FILTERED)
            print(
                f"\n  {len(FILTERED)} pre-existing violation(s) not attributed to "
                f"this PR: {', '.join(f'{k}x{v}' for k, v in sorted(c.items()))}"
            )
        if SKIPPED:
            print("\n  NOT CHECKED this run:")
            for rule, why in SKIPPED:
                print(f"    {rule}: {why}")
        print("\n  Never checked by this script (need an AST or judgement):")
        for rule, why in sorted(MODEL_JUDGED.items()):
            print(f"    {rule}: {why.splitlines()[0]}")


if __name__ == "__main__":
    main()
