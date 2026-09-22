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

"""Configuration for the data-exfiltration guard.

Two extension axes, no code change required for either:

* **App-wide defaults** — edit the module-level constants below
  (``DEFAULT_ALLOWED_HOSTS``, ``_DEFAULT_SECRET_PATTERNS``, the regexes).
  These ship with the agent and apply to every tenant.
* **Per-tenant overlay** — an optional ``<working_dir>/.lha/exfil.jsonl``,
  hot-reloaded by mtime (same mechanism as ``policies.jsonl``). Each line
  is a JSON object that *extends* the defaults:

      {"allow_hosts": ["api.mycorp.com", "*.internal.example"]}
      {"secret_patterns": ["MYCORP-[A-Z0-9]{20}"]}
      {"sensitive_paths": ["vault/", "\\.token$"]}

A third axis — per-session ``/grant`` exemptions — lives in the guard
itself via the shared ``policy_grants`` machinery.
"""

from __future__ import annotations

import ipaddress
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from horizon.guardrails._jsonl import iter_jsonl_objects
from horizon.guardrails._overlay import (
    is_local_env,
    overlay_cache_key,
    read_overlay_text,
)
from horizon.guardrails._regex_safety import safe_regex

_logger = logging.getLogger(__name__)

EXFIL_OVERLAY_FILENAME = ".lha/exfil.jsonl"

# Curated host allowlist for Layer A (argument guard). Plain domains also match
# their subdomains (so `googleapis.com` covers `drive.googleapis.com`, etc.),
# and "*." globs are honored explicitly. Widen per-tenant via .lha/exfil.jsonl.
DEFAULT_ALLOWED_HOSTS: tuple[str, ...] = (
    # loopback (harmless; used by the Layer A host check)
    "localhost",
    "127.0.0.1",
    "::1",
    # source control
    "github.com",
    "githubusercontent.com",
    # Google: Cloud APIs (gcloud), Drive/Docs, auth, static + user content
    "google.com",
    "googleapis.com",
    "gstatic.com",
    "googleusercontent.com",
    "gcr.io",
    "pkg.dev",
    # package registries — uv/pip/npm break without these
    "pypi.org",
    "pythonhosted.org",
    "npmjs.org",
)

# (label, regex) for well-known secret shapes — chosen for low false-positive.
_DEFAULT_SECRET_PATTERNS: tuple[tuple[str, str], ...] = (
    ("private key block", r"-----BEGIN[ A-Z]*PRIVATE KEY-----"),
    ("AWS access key id", r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    ("Google API key", r"\bAIza[0-9A-Za-z_\-]{35}\b"),
    ("GitHub token", r"\bgh[pousr]_[A-Za-z0-9]{36,}\b"),
    ("Slack token", r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"),
    ("OpenAI-style key", r"\bsk-[A-Za-z0-9]{20,}\b"),
    ("service-account private_key", r'"private_key"\s*:\s*"-----BEGIN'),
)

# Shell-command shape detectors (app-wide; not tenant-extensible by design).
# Best-effort argument guard, not exhaustive (misses a raw python socket,
# git push, etc.), raising the bar against the common shapes. HTTP tools
# gate only on an *upload* shape so plain GET/downloads pass; transfer/tunnel
# tools move data bidirectionally and are hard to direction-parse, so any
# remote host they name is gated.
HTTP_TOOLS_RE = re.compile(r"\b(curl|wget)\b")
TRANSFER_TOOLS_RE = re.compile(
    r"\b(scp|sftp|ftp|rsync|ssh|nc|ncat|netcat|telnet|socat)\b"
)
# Upload-shaped curl/wget flags (no bare "@" — it false-matched user@host).
UPLOAD_RE = re.compile(
    r"(--data\b|--data-[a-z]+\b|\s-d\b|\s-F\b|--form\b|\s-T\b|--upload-file\b)"
)
# Env-var names whose underscore-delimited segments include a secret word.
# Matching segments (not substrings) avoids $API_BASE / $MONKEY false hits.
_ENV_VAR_RE = re.compile(r"\$\{?([A-Za-z_][A-Za-z0-9_]*)")
_SECRET_ENV_WORDS = frozenset(
    {
        "KEY",
        "KEYS",
        "TOKEN",
        "SECRET",
        "SECRETS",
        "PASSWORD",
        "PASSWD",
        "CREDENTIAL",
        "CREDENTIALS",
        "APIKEY",
    }
)
# Credential file/dir shapes — matched against shell commands that also use a
# network tool. Use the gcloud *credentials* dir, not the bare word "gcloud"
# (which false-matched `curl -o gcloud.tgz` / using the gcloud binary).
_DEFAULT_SENSITIVE_PATH = (
    r"(\.env(?![.\w])|\.env\.(?:local|production|prod|preprod|dev|development|staging|test|testing|secret|secrets)\b"
    r"|/\.aws(/|\b)|application_default_credentials|/\.config/gcloud/"
    r"|id_rsa|id_ed25519|id_ecdsa|/\.ssh/"
    r"|service[-_]?account[^\"'\s]*\.json|credentials\.json"
    r"|\.pem\b|\.netrc\b|\.npmrc\b|\.pypirc\b)"
)
HOST_RE = re.compile(r"https?://([^/\s'\"]+)")
SCP_HOST_RE = re.compile(r"\b[\w.+-]+@([\w.-]+):")
DEVTCP_RE = re.compile(r"/dev/tcp/([^/\s'\"]+)")
# Non-http URL schemes that HOST_RE misses — e.g. `git push ssh://host/x`,
# `git push git://host/x`, `curl ftp://host/x`.
SCHEME_HOST_RE = re.compile(r"\b(?:ssh|git|ftp|ftps)://(?:[\w.+-]+@)?([\w.-]+)")
# `git push` / `git remote add|set-url` move data to a remote; treat them as an
# egress shape so a push to a non-allowlisted host is gated. A push to a
# pre-configured remote (no URL in the command) names no host and is not caught
# here.
GIT_EGRESS_RE = re.compile(
    r"\bgit\b[^\n]*\b(?:push|remote\s+(?:add|set-url))\b"
)


@dataclass(frozen=True)
class ExfilConfig:
    allowed_hosts: tuple[str, ...]
    secret_patterns: tuple[tuple[str, re.Pattern[str]], ...]
    sensitive_path_res: tuple[re.Pattern[str], ...]


def _compile_secret_patterns(
    extra: list[str] | None = None,
) -> tuple[tuple[str, re.Pattern[str]], ...]:
    out: list[tuple[str, re.Pattern[str]]] = []
    for label, pat in _DEFAULT_SECRET_PATTERNS:
        out.append((label, re.compile(pat)))
    for pat in extra or []:
        if not safe_regex(pat):
            _logger.warning("exfil: dropping unsafe secret_pattern %r", pat)
            continue
        compiled = _safe_compile(pat)
        if compiled is not None:
            out.append((f"tenant pattern {pat!r}", compiled))
    return tuple(out)


def _safe_compile(pattern: str) -> re.Pattern[str] | None:
    try:
        return re.compile(pattern, re.IGNORECASE)
    except re.error:
        _logger.warning("exfil: skipping invalid regex %r", pattern)
        return None


def default_config() -> ExfilConfig:
    return ExfilConfig(
        allowed_hosts=DEFAULT_ALLOWED_HOSTS,
        secret_patterns=_compile_secret_patterns(),
        sensitive_path_res=(
            re.compile(_DEFAULT_SENSITIVE_PATH, re.IGNORECASE),
        ),
    )


_cache: dict[str, Any] = {"path": None, "mtime": None, "config": None}


def _parse_overlay(text: str) -> tuple[list[str], list[str], list[str]]:
    """Parse overlay JSONL → (allow_hosts, secret_patterns, sensitive_paths)."""
    hosts: list[str] = []
    secrets: list[str] = []
    paths: list[str] = []
    for obj in iter_jsonl_objects(text, context="exfil"):
        hosts += [h for h in obj.get("allow_hosts", []) if isinstance(h, str)]
        secrets += [
            s for s in obj.get("secret_patterns", []) if isinstance(s, str)
        ]
        paths += [
            p for p in obj.get("sensitive_paths", []) if isinstance(p, str)
        ]
    return hosts, secrets, paths


def _read_overlay(path: Path) -> tuple[list[str], list[str], list[str]]:
    """Return (allow_hosts, secret_patterns, sensitive_paths) from the overlay."""
    try:
        text = path.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError):
        return [], [], []
    return _parse_overlay(text)


def _build_overlay_config(
    hosts: list[str], secrets: list[str], paths: list[str]
) -> ExfilConfig:
    """Defaults extended with a parsed overlay's hosts/secret-patterns/paths."""
    safe_paths = [p for p in paths if safe_regex(p)]
    if len(safe_paths) < len(paths):
        dropped = len(paths) - len(safe_paths)
        _logger.warning("exfil: dropped %d unsafe sensitive_paths", dropped)
    extra_path_res = tuple(
        r for r in (_safe_compile(p) for p in safe_paths) if r is not None
    )
    return ExfilConfig(
        allowed_hosts=DEFAULT_ALLOWED_HOSTS + tuple(hosts),
        secret_patterns=_compile_secret_patterns(secrets),
        sensitive_path_res=(
            re.compile(_DEFAULT_SENSITIVE_PATH, re.IGNORECASE),
            *extra_path_res,
        ),
    )


def load_exfil_config(working_dir: Path | None = None) -> ExfilConfig:
    """Defaults merged with the per-tenant overlay read from the local fs
    (hot-reloaded by mtime). For the runtime guard under the sandbox backend use
    ``load_exfil_config_for_env``, which reaches the overlay via the env interface."""
    if working_dir is None:
        return default_config()

    overlay_path = working_dir / EXFIL_OVERLAY_FILENAME
    try:
        mtime: float | None = overlay_path.stat().st_mtime
    except (FileNotFoundError, OSError):
        mtime = None

    cache_key = str(overlay_path)
    if (
        _cache["path"] == cache_key
        and _cache["mtime"] == mtime
        and _cache["config"] is not None
        and mtime is not None
    ):
        return _cache["config"]

    config = _build_overlay_config(*_read_overlay(overlay_path))
    _cache["path"] = cache_key
    _cache["mtime"] = mtime
    _cache["config"] = config
    return config


_overlay_cache: dict[str, ExfilConfig] = {}


async def load_exfil_config_for_env(env: Any) -> ExfilConfig:
    """Defaults merged with the ``.lha/exfil.jsonl`` overlay, read through the
    right backend: local fs (mtime hot-reload) or the sandbox env interface (the file
    lives in the sandbox, not on the host). Sandbox reads are cached per sandbox
    identity."""
    if env is None:
        return default_config()
    working_dir = getattr(env, "working_dir", None)
    if working_dir is None:
        return default_config()
    if is_local_env(env):
        return load_exfil_config(working_dir)
    key = overlay_cache_key(env)
    cached = _overlay_cache.get(key)
    if cached is not None:
        return cached
    text = await read_overlay_text(env, working_dir / EXFIL_OVERLAY_FILENAME)
    config = _build_overlay_config(*_parse_overlay(text))
    _overlay_cache[key] = config
    return config


def scan_secrets(text: str, config: ExfilConfig) -> str | None:
    """Return the label of the first secret pattern found in ``text``."""
    for label, pat in config.secret_patterns:
        if pat.search(text):
            return label
    return None


def _has_secret_env(command: str) -> bool:
    for name in _ENV_VAR_RE.findall(command):
        if any(seg in _SECRET_ENV_WORDS for seg in name.upper().split("_")):
            return True
    return False


def references_secret(command: str, config: ExfilConfig) -> bool:
    """True if the command reads a credential path or a secret env var."""
    if _has_secret_env(command):
        return True
    return any(pat.search(command) for pat in config.sensitive_path_res)


def extract_hosts(command: str) -> list[str]:
    return (
        HOST_RE.findall(command)
        + SCP_HOST_RE.findall(command)
        + DEVTCP_RE.findall(command)
        + SCHEME_HOST_RE.findall(command)
    )


def host_allowed(host: str, config: ExfilConfig) -> bool:
    host = (
        host.rsplit("@", maxsplit=1)[-1]
        .split(":", maxsplit=1)[0]
        .strip()
        .lower()
        .rstrip(".")
    )
    if not host:
        return False
    for pattern in config.allowed_hosts:
        p = pattern.lower()
        if p.startswith("*."):
            base = p[2:]
            if host == base or host.endswith("." + base):
                return True
        elif host == p or host.endswith("." + p):
            return True
    return False


# The GCP metadata server hands out the sandbox's ambient (shared, privileged)
# service-account token. Layer A blocks any network command that targets it on
# every turn — released only by an explicit exact /grant from the user. This is
# a heuristic shape match: decimal/hex/IPv6 forms of the link-local address still
# evade it.
METADATA_HOSTS: frozenset[str] = frozenset(
    {"metadata.google.internal", "metadata", "169.254.169.254"}
)


def _as_ipv4(host: str) -> str | None:
    """Normalize the numeric IPv4 spellings curl accepts (bare decimal/hex/octal,
    per-octet hex/octal, IPv6-mapped) to a dotted quad, so an obfuscated literal
    can't walk past a string match."""

    def _octet(tok: str) -> int:
        if re.fullmatch(r"0[xX][0-9a-fA-F]+", tok):
            return int(tok, 16)
        if re.fullmatch(r"0[0-7]+", tok):
            return int(tok, 8)
        return int(tok, 10)

    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        return str(getattr(addr, "ipv4_mapped", None) or addr)

    # inet_aton accepts 1-4 parts; the last one packs all the remaining bytes
    # (169.254.43518 == 169.254.169.254), so each arity needs its own width.
    parts = host.split(".")
    if not 1 <= len(parts) <= 4:
        return None
    try:
        values = [_octet(tok) for tok in parts]
    except ValueError:
        return None

    tail_bits = 8 * (4 - len(parts) + 1)
    if any(not 0 <= v <= 255 for v in values[:-1]):
        return None
    if not 0 <= values[-1] < (1 << tail_bits):
        return None

    packed = values[-1]
    for shift, value in enumerate(reversed(values[:-1]), start=1):
        packed |= value << (tail_bits + 8 * (shift - 1))
    try:
        return str(ipaddress.ip_address(packed))
    except (ValueError, ipaddress.AddressValueError):
        return None


def is_metadata_host(host: str) -> bool:
    host = (
        host.rsplit("@", maxsplit=1)[-1].strip().lower().strip("[]").rstrip(".")
    )
    if "]" in host or host.count(":") <= 1:
        host = host.split(":", maxsplit=1)[0].strip("[]")
    if host in METADATA_HOSTS:
        return True
    return _as_ipv4(host) in METADATA_HOSTS


def targets_metadata(command: str) -> bool:
    """True if a command names the GCP metadata server (scheme'd or bare)."""
    lowered = command.lower()
    if "169.254.169.254" in lowered or "metadata.google.internal" in lowered:
        return True
    return any(is_metadata_host(h) for h in extract_hosts(command))


__all__ = [
    "DEFAULT_ALLOWED_HOSTS",
    "EXFIL_OVERLAY_FILENAME",
    "GIT_EGRESS_RE",
    "HTTP_TOOLS_RE",
    "METADATA_HOSTS",
    "TRANSFER_TOOLS_RE",
    "UPLOAD_RE",
    "ExfilConfig",
    "default_config",
    "extract_hosts",
    "host_allowed",
    "is_metadata_host",
    "load_exfil_config",
    "load_exfil_config_for_env",
    "references_secret",
    "scan_secrets",
    "targets_metadata",
]
