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

"""Argv-structural shell-command classifier (gemini-cli's commandSafety, stdlib).

Lexes a command into argv via ``shlex`` (quote- and operator-aware), then
inspects tokens structurally instead of substring/regex-matching the raw string.
Returns a coarse verdict: ``"deny"`` (catastrophic — hard-block everywhere),
``"ask"`` (risky — interactive ask on the root chain, block on child/headless),
or ``None`` (no opinion; the normal permission flow decides).
"""

from __future__ import annotations

import os.path
import re
import shlex

from app.guardrails.command_classify import split_segments, strip_wrapper

_CONTROL_OPS = frozenset({"|", "||", "&&", ";", "&"})
_PIPE_OPS = frozenset({"|", "||"})
# Transparent launchers that run the rest of the line as a command — unwrap them
# so the classifier inspects the real binary, not the wrapper. Unlike sudo/doas
# these grant no privilege, so they do NOT set `escalated`.
_LAUNCHERS = frozenset(
    {
        "command",
        "nice",
        "nohup",
        "stdbuf",
        "setsid",
        "ionice",
        "xargs",
        "timeout",
    }
)
# A `-c` shell flag, possibly clustered with other short flags (`-lc`, `-ic`).
_SHELL_C_FLAG = re.compile(r"^-[a-z]*c[a-z]*$")
# Per-launcher flags that consume the NEXT token as a separate-arg value, so the
# value isn't mistaken for the wrapped command (`timeout -s KILL 5 rm -rf /`).
_LAUNCHER_VALUE_FLAGS = {
    "timeout": {"-s", "--signal", "-k", "--kill-after"},
    "nice": {"-n", "--adjustment"},
    "ionice": {"-c", "-n", "-p", "-P", "-u", "--class", "--classdata", "--pid"},
    "stdbuf": {"-i", "-o", "-e", "--input", "--output", "--error"},
    "xargs": {
        "-I",
        "-i",
        "-d",
        "--delimiter",
        "-E",
        "-e",
        "--eof",
        "-n",
        "--max-args",
        "-L",
        "-l",
        "--max-lines",
        "-P",
        "--max-procs",
        "-s",
        "--max-chars",
        "--replace",
    },
}
_DANGEROUS_RM_TARGETS = frozenset(
    {
        "/",
        "~",
        "$HOME",
        "/etc",
        "/usr",
        "/bin",
        "/sbin",
        "/lib",
        "/lib64",
        "/boot",
        "/sys",
        "/proc",
        "/dev",
        "/var",
        "/root",
        "/home",
        "/Users",
    }
)
# Containers whose one-level children ARE account roots: /home/bob wipes a whole
# user, /home/bob/build does not (so these can't join the prefix tuple below).
_HOME_CONTAINERS = ("/home", "/Users")
_DANGEROUS_RM_PREFIXES = (
    "/etc",
    "/usr",
    "/bin",
    "/sbin",
    "/lib",
    "/lib64",
    "/boot",
    "/sys",
    "/proc",
    "/dev",
    "/var",
    "/root",
)
_FIND_DESTRUCTIVE = frozenset(
    {
        "-delete",
        "-exec",
        "-execdir",
        "-ok",
        "-okdir",
        "-fls",
        "-fprint",
        "-fprint0",
    }
)
_PIPE_INTERPRETERS = frozenset(
    {"sh", "bash", "zsh", "python", "python3", "perl", "ruby", "node", "php"}
)
# Cloud/infra CLIs whose delete/destroy verbs are irreversible data/resource
# loss. Token-matched (not substring) so a quoted SQL DELETE or a resource named
# "delete" does not trip it.
_CLOUD_DELETE: dict[str, frozenset[str]] = {
    "bq": frozenset({"rm"}),
    "gcloud": frozenset({"delete", "rm"}),
    "gsutil": frozenset({"rm"}),
    "kubectl": frozenset({"delete"}),
    "terraform": frozenset({"destroy"}),
    "docker": frozenset({"rm", "rmi", "prune"}),
    "gws": frozenset({"delete"}),
}


# git's global options that consume the NEXT token as a separate value, so the
# value can't be mistaken for the subcommand (`git -C /repo push --force`). The
# `--opt=value` form needs no entry: it lexes as a single flag token.
_GIT_VALUE_FLAGS = frozenset(
    {"-C", "-c", "--git-dir", "--work-tree", "--namespace", "--super-prefix"}
)
_GIT_GATED_SUBCOMMANDS = frozenset(
    {"push", "reset", "clean", "filter-branch", "filter-repo"}
)


def _git_positionals(argv: list[str]) -> list[str]:
    out: list[str] = []
    i = 1
    while i < len(argv):
        tok = argv[i]
        if tok in _GIT_VALUE_FLAGS:
            i += 2
        elif tok.startswith("-"):
            i += 1
        else:
            out.append(tok)
            i += 1
    return out


def _git_verdict(argv: list[str]) -> tuple[str, str] | None:
    positionals = _git_positionals(argv)
    # Scan for a gated verb rather than trusting the first positional: an unknown
    # global flag's value would otherwise shadow it, and a path named `push`
    # would otherwise fake it.
    sub = next((t for t in positionals if t in _GIT_GATED_SUBCOMMANDS), None)
    if sub is None:
        return None
    flags = [t for t in argv[1:] if t.startswith("-")]
    if sub == "push":
        if any(
            f in {"-f", "--force"} or f.startswith("--force-with-lease")
            for f in flags
        ):
            return ("ask", "git force-push rewrites remote history")
        if any(f in {"--delete", "-d", "--mirror"} for f in flags):
            return ("ask", "git push deletes or mirrors a remote ref")
        if any(p.startswith("+") for p in positionals):
            return (
                "ask",
                "git force-push via +refspec rewrites remote history",
            )
        return None
    if sub == "reset" and "--hard" in flags:
        return ("ask", "git reset --hard discards changes")
    if sub == "clean":
        short = [f[1:] for f in flags if not f.startswith("--")]
        if "--force" in flags or any("f" in s for s in short):
            return ("ask", "git clean deletes untracked files")
        return None
    if sub in {"filter-branch", "filter-repo"}:
        return ("ask", "git history rewrite")
    return None


def lex(command: str) -> list[str] | None:
    """shlex tokens (quote- and operator-aware); None on a parse error."""
    try:
        lx = shlex.shlex(command, posix=True, punctuation_chars=True)
        lx.whitespace_split = True
        return list(lx)
    except ValueError:
        return None


def _split_tokens(tokens: list[str]) -> list[tuple[list[str], bool]]:
    # Each segment carries whether the operator BEFORE it was a pipe (`|`/`||`);
    # only then is it a pipe target. `&&`/`;`/`&` are sequence, not pipe.
    out: list[tuple[list[str], bool]] = []
    cur: list[str] = []
    cur_is_pipe_target = False
    for tok in tokens:
        if tok in _CONTROL_OPS:
            if cur:
                out.append((cur, cur_is_pipe_target))
                cur = []
            cur_is_pipe_target = tok in _PIPE_OPS
            continue
        cur.append(tok)
    if cur:
        out.append((cur, cur_is_pipe_target))
    return out


def _segments_with_pipe(command: str) -> list[tuple[list[str], bool]]:
    inner = strip_wrapper(command)
    toks = lex(inner)
    if toks is None:
        # Conservative fallback: regex split, shlex each piece, drop unparseable.
        segs: list[tuple[list[str], bool]] = []
        for seg in split_segments(inner) or [inner]:
            try:
                segs.append((shlex.split(seg), False))
            except ValueError:
                continue
        return segs
    # `bash -c <inner>` / `bash -lc <inner>` reaching here (strip_wrapper only
    # matches the bare `-c` form) -> recurse on the inner command string.
    if len(toks) >= 3 and os.path.basename(toks[0]) in {"bash", "sh", "zsh"}:
        ci = next(
            (j for j, t in enumerate(toks) if _SHELL_C_FLAG.match(t)),
            None,
        )
        if ci is not None and ci + 1 < len(toks):
            return _segments_with_pipe(toks[ci + 1])
    return _split_tokens(toks)


def segments(command: str) -> list[list[str]]:
    """Per-segment argv. Unwraps a single ``bash -c '<inner>'`` and recurses."""
    return [argv for argv, _ in _segments_with_pipe(command)]


def _binary(argv: list[str]) -> str:
    return os.path.basename(argv[0]) if argv else ""


def _effective(argv: list[str]) -> tuple[list[str], bool]:
    """Strip env-var prefixes and unwrap sudo/doas/env; return (effective argv, escalated)."""
    i, escalated = 0, False
    while i < len(argv):
        tok = argv[i]
        if (
            "=" in tok
            and not tok.startswith("-")
            and "/" not in tok.split("=", 1)[0]
        ):
            i += 1
            continue
        base = os.path.basename(tok)
        if base in {"sudo", "doas"}:
            escalated = True
            i += 1
            while i < len(argv) and argv[i].startswith("-"):
                i += 1
            continue
        if base == "env":
            i += 1
            continue
        if base in _LAUNCHERS:
            i += 1
            value_flags = _LAUNCHER_VALUE_FLAGS.get(base, set())
            # Skip the launcher's own option/positional noise so the next real
            # token is the binary: boolean flags, numeric positionals (`timeout
            # 5`, `nice -5`), and the VALUE of a separate-arg flag (`-s KILL`).
            while i < len(argv):
                cur = argv[i]
                if cur.startswith("-"):
                    if "=" in cur:  # `--signal=KILL` is self-contained
                        i += 1
                    elif cur in value_flags:  # consumes the next token as value
                        i += 2
                    else:  # boolean flag
                        i += 1
                elif cur.lstrip("+-").isdigit():  # numeric positional
                    i += 1
                else:  # the wrapped command
                    break
            continue
        break
    return argv[i:], escalated


def _is_user_home_root(t: str) -> bool:
    parts = t.split("/")
    return (
        len(parts) == 3
        and f"/{parts[1]}" in _HOME_CONTAINERS
        and bool(parts[2])
    )


def _dangerous_target(t: str) -> bool:
    trimmed = t.rstrip("/*")
    if (
        t == "/*"
        or t in _DANGEROUS_RM_TARGETS
        or trimmed in _DANGEROUS_RM_TARGETS
        or _is_user_home_root(trimmed)
    ):
        return True
    return any(t == p or t.startswith(p + "/") for p in _DANGEROUS_RM_PREFIXES)


def _rm_verdict(argv: list[str]) -> tuple[str, str] | None:
    short_flags = [
        t[1:] for t in argv[1:] if t.startswith("-") and not t.startswith("--")
    ]
    has_r = "--recursive" in argv or any("r" in f.lower() for f in short_flags)
    has_f = "--force" in argv or any("f" in f.lower() for f in short_flags)
    recursive_force = has_r and has_f
    if not recursive_force:
        return None
    targets = [t for t in argv[1:] if not t.startswith("-")]
    if any(_dangerous_target(t) for t in targets):
        return ("deny", "recursive force-delete of a system/home root")
    if any(t in {".", "./", "..", "../", "*"} for t in targets):
        return (
            "ask",
            "recursive force-delete of the working directory or a glob",
        )
    return None


def _segment_verdict(
    argv: list[str], is_pipe_target: bool
) -> tuple[str, str] | None:
    if not argv:
        return None
    eff, escalated = _effective(argv)
    if not eff:
        return None
    binary = os.path.basename(eff[0])
    if is_pipe_target and binary in _PIPE_INTERPRETERS:
        return ("ask", "piping into an interpreter")
    if binary == "rm":
        v = _rm_verdict(eff)
        if v is not None:
            return v
    if binary == "find" and any(t in _FIND_DESTRUCTIVE for t in eff[1:]):
        return ("ask", "find with a side-effecting action")
    if binary == "git":
        v = _git_verdict(eff)
        if v is not None:
            return v
    if binary in {"chmod", "chown"}:
        short_flags = [
            t[1:]
            for t in eff[1:]
            if t.startswith("-") and not t.startswith("--")
        ]
        recursive = "--recursive" in eff[1:] or any(
            "r" in f.lower() for f in short_flags
        )
        targets = [t for t in eff[1:] if not t.startswith("-")]
        if recursive and any(_dangerous_target(t) for t in targets):
            return ("ask", f"recursive {binary} on a system/home root")
    if binary == "mv":
        targets = [t for t in eff[1:] if not t.startswith("-")]
        if any(t == "/dev/null" or t.startswith("/dev/null/") for t in targets):
            return ("ask", "mv into /dev/null destroys data")
    delete_verbs = _CLOUD_DELETE.get(binary)
    if delete_verbs is not None and any(
        t in delete_verbs for t in eff[1:] if not t.startswith("-")
    ):
        return ("ask", f"{binary} destructive delete")
    if binary in {"sudo", "su"}:
        return ("ask", f"privilege escalation via {binary}")
    if escalated:
        return ("ask", "privilege escalation")
    # nc -l / socat listeners stay a hard-deny in the seed (reverse-shell primitive),
    # so they are deliberately NOT classified here.
    return None


def classify(command: str) -> tuple[str, str] | None:
    """Strongest verdict across segments. Unparseable command -> conservative ask."""
    if lex(strip_wrapper(command)) is None and not segments(command):
        return ("ask", "command could not be parsed")
    segs = _segments_with_pipe(command)
    if not segs:
        return ("ask", "command could not be parsed")
    verdict: tuple[str, str] | None = None
    for argv, is_pipe_target in segs:
        v = _segment_verdict(argv, is_pipe_target=is_pipe_target)
        if v is None:
            continue
        if v[0] == "deny":
            return v
        verdict = v
    return verdict


__all__ = ["classify", "lex", "segments"]
