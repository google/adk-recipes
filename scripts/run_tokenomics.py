"""Vendored entrypoint invoked by Google `agents-cli` (`agents-cli-extension/v1alpha1`).

When an extension is installed via `agents-cli extension add`, `agents-cli`
vendors the repository under `<scope>/extensions/adk-tokenomics/` and executes
`run:` from the target project's working directory with
`AGENTS_CLI_EXTENSION_DIR` pointing at the vendored extension root.

Prepending that root to `sys.path` lets `agents-cli tokenomics <web|profile|assert>`
and `agents-cli eval budget` execute directly from the SHA-pinned extension copy
with zero extra package installation required for local profiling, web UI, and
CI/CD assertions, while still inheriting optional dependencies (such as
`google-cloud-bigquery` or `google-adk`) from the active `uv` environment.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import os
from pathlib import Path
import signal
import subprocess
import sys
import threading

EXTENSION_DIR_ENV = "AGENTS_CLI_EXTENSION_DIR"
# Read by `adk_tokenomics_plugin.cli_profiler.cli_prog_name()` for usage lines and remediation hints.
PROG_ENV = "ADK_TOKENOMICS_PROG"
AGENTS_CLI_PROG = "agents-cli tokenomics"
_PACKAGE_INIT = ("adk_tokenomics_plugin", "__init__.py")
# Flags whose values are filesystem paths the user typed relative to their shell directory.
PATH_FLAGS = frozenset({"-t", "--traces", "--trace", "--traces-dir", "--prompts-file"})
_WEB_COMMANDS = frozenset({"web", "serve", "ui"})


def resolve_user_relative_paths(
    argv: Sequence[str],
    *,
    cwd: str | os.PathLike[str] | None = None,
    env: Mapping[str, str] | None = None,
) -> list[str]:
    """Re-anchor relative path flags to the user's shell directory.

    `agents-cli` runs extension commands with cwd set to the project root, so
    `cd app && agents-cli tokenomics profile -t ../artifacts/traces/x.json`
    would otherwise resolve `..` against the root. Shells export the user's
    directory as `$PWD`; when it lies inside the project root (and differs from
    cwd), a relative path value that exists under `$PWD` is rewritten to its
    absolute form. Anything else is left untouched, so project-root-relative
    paths keep working exactly as before.
    """
    args = list(argv)
    active_env = os.environ if env is None else env
    raw_pwd = (active_env.get("PWD") or "").strip()
    if not raw_pwd:
        return args
    try:
        base = Path(cwd if cwd is not None else os.getcwd()).resolve()
        user_dir = Path(raw_pwd).resolve()
    except OSError:
        return args
    if user_dir == base or not user_dir.is_dir() or not user_dir.is_relative_to(base):
        return args

    def _fix(value: str) -> str:
        if not value or os.path.isabs(value) or value.startswith("~"):
            return value
        candidate = user_dir / value
        return str(candidate.resolve()) if candidate.exists() else value

    out: list[str] = []
    i = 0
    while i < len(args):
        tok = args[i]
        if tok in PATH_FLAGS and i + 1 < len(args):
            out.extend([tok, _fix(args[i + 1])])
            i += 2
            continue
        if tok.startswith("--") and "=" in tok:
            flag, value = tok.split("=", 1)
            if flag in PATH_FLAGS:
                out.append(f"{flag}={_fix(value)}")
                i += 1
                continue
        out.append(tok)
        i += 1
    return out


def parent_pid_of(pid: int) -> int | None:
    """Return the parent PID of `pid`, or None if the process no longer exists."""
    try:
        with open(f"/proc/{pid}/stat", "rb") as fh:
            stat = fh.read().decode(errors="replace")
        # Field 4 (ppid) follows the parenthesised command name, which may contain spaces.
        return int(stat.rsplit(")", 1)[1].split()[1])
    except FileNotFoundError:
        if os.path.isdir("/proc/self"):
            return None  # procfs exists, so the process is gone
    except (OSError, ValueError, IndexError):
        pass
    try:  # macOS / BSD
        res = subprocess.run(["ps", "-o", "ppid=", "-p", str(pid)], capture_output=True, text=True, timeout=5)
        text = res.stdout.strip()
        return int(text) if res.returncode == 0 and text else None
    except (OSError, ValueError, subprocess.SubprocessError):
        return None


def snapshot_ancestors(depth: int = 2, pid: int | None = None) -> dict[int, int]:
    """Map each of `pid`'s first `depth` ancestors to its own parent (skipping init)."""
    links: dict[int, int] = {}
    current = os.getpid() if pid is None else pid
    parent = parent_pid_of(current)
    for _ in range(depth):
        if parent is None or parent <= 1:
            break
        grandparent = parent_pid_of(parent)
        if grandparent is None:
            break
        links[parent] = grandparent
        parent = grandparent
    return links


def ancestors_changed(snapshot: Mapping[int, int]) -> bool:
    """True when any recorded ancestor has exited or been re-parented."""
    return any(parent_pid_of(child) != expected for child, expected in snapshot.items())


def start_parent_watchdog(interval: float = 1.0, depth: int = 2, grace: float = 5.0) -> threading.Thread | None:
    """Stop a long-running command when the `agents-cli` that launched it dies.

    The process tree is `agents-cli` -> `uv run` -> python. When a CI timeout or
    `kill` ends `agents-cli`, `uv` keeps running, so watching only `getppid()`
    is not enough: the watchdog records the first `depth` ancestors and sends
    SIGINT to this process (triggering the normal clean shutdown) as soon as
    any of them exits or is re-parented. If the clean shutdown has not finished
    within `grace` seconds, the process exits hard.
    """
    if os.name == "nt":
        return None
    snapshot = snapshot_ancestors(depth=depth)
    if not snapshot:
        return None

    def _watch() -> None:
        stop = threading.Event()
        while not stop.wait(interval):
            if ancestors_changed(snapshot):
                try:
                    print("\n[SYS] Launching agents-cli process exited; stopping.", file=sys.stderr, flush=True)
                except (OSError, ValueError):
                    pass  # the launcher's pipes are usually gone too (e.g. a CI step that timed out)
                os.kill(os.getpid(), signal.SIGINT)
                if not stop.wait(grace):
                    os._exit(1)  # clean shutdown did not complete in time
                return

    thread = threading.Thread(target=_watch, name="adk-tokenomics-parent-watchdog", daemon=True)
    thread.start()
    return thread


def _is_web_command(argv: Sequence[str]) -> bool:
    first = next((tok for tok in argv if not tok.startswith("-")), None)
    return first in _WEB_COMMANDS


def resolve_extension_root(env: Mapping[str, str] | None = None) -> Path:
    """Resolve the extension root from `AGENTS_CLI_EXTENSION_DIR` or script location."""
    active_env = os.environ if env is None else env
    raw_env_dir = active_env.get(EXTENSION_DIR_ENV, "").strip()
    if raw_env_dir:
        candidate = Path(raw_env_dir).expanduser().resolve()
        if candidate.joinpath(*_PACKAGE_INIT).is_file():
            return candidate
    return Path(__file__).resolve().parents[1]


def bootstrap_sys_path(ext_root: Path | None = None) -> Path:
    """Ensure the pinned `adk_tokenomics_plugin` package root is first on `sys.path`."""
    resolved_root = (ext_root or resolve_extension_root()).resolve()
    if not resolved_root.joinpath(*_PACKAGE_INIT).is_file():
        raise RuntimeError(
            f"Could not locate 'adk_tokenomics_plugin' under extension root: {resolved_root}"
        )

    target_str = str(resolved_root)
    sys.path[:] = [
        entry
        for entry in sys.path
        if not entry or Path(entry).resolve() != resolved_root
    ]
    sys.path.insert(0, target_str)
    return resolved_root


def run(argv: Sequence[str] | None = None) -> int:
    """Bootstrap `sys.path` and dispatch to `adk_tokenomics_plugin.cli.main`."""
    bootstrap_sys_path()
    from adk_tokenomics_plugin.cli import main

    prev_argv = sys.argv[:]
    effective = resolve_user_relative_paths(prev_argv[1:] if argv is None else argv)
    under_agents_cli = bool(os.environ.get(EXTENSION_DIR_ENV))
    if _is_web_command(effective) and under_agents_cli:
        start_parent_watchdog()
    prev_prog = os.environ.get(PROG_ENV)
    if under_agents_cli and not (prev_prog or "").strip():
        # `adk-tokenomics` isn't on PATH for extension users; name the command they actually ran.
        os.environ[PROG_ENV] = AGENTS_CLI_PROG
    try:
        sys.argv = [prev_argv[0] if prev_argv else "adk-tokenomics", *effective]
        try:
            rc = main()
        except SystemExit as exc:
            if exc.code is None:
                return 0
            if isinstance(exc.code, int):
                return exc.code
            print(exc.code, file=sys.stderr)
            return 1
        return 0 if rc is None else int(rc)
    finally:
        sys.argv = prev_argv
        if prev_prog is None:
            os.environ.pop(PROG_ENV, None)
        else:
            os.environ[PROG_ENV] = prev_prog


if __name__ == "__main__":
    raise SystemExit(run())
