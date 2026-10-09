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
"""Unit tests for check_lockfile_npm.py.

Tests supply-chain policy enforcement on TypeScript / npm lockfiles:
- Public npm registry validation with per-URL anchoring
- Git / VCS dependency rejection
- Local file / link dependency rejection
- Subresource Integrity (SRI) hash presence and format validation
- Exemption of root, workspace, and internal symlink entries
- Structural sanity / shape error handling
- Detection of unsupported lockfile formats (pnpm, yarn, bun)
- Exit code contracts (0 = pass, 1 = violations, 2 = CI fault)
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import check_lockfile_npm as m

EXIT_OK = 0
EXIT_VIOLATIONS = 1
EXIT_CI_FAULT = 2

_SHA512_HASH = "sha512-9b0xQp00000000000000000000000000000000000000000000000000000000000000000000000000000000=="
_SHA256_HASH = "sha256-47DEQpj8HBSa+/TImW+5JCeuQeRkm5NMpJWZG3hSuFU="


def _lockfile(tmp_path: Path, filename: str, content: str) -> Path:
    path = tmp_path / filename
    path.write_text(content, encoding="utf-8")
    return path


def _run(target: Path | str, monkeypatch) -> int:
    monkeypatch.setattr(sys, "argv", ["check_lockfile_npm.py", str(target)])
    return m.main()


# ---------------------------------------------------------------------------
# Passing cases (v1, v2, v3 lockfiles and shrinkwrap)
# ---------------------------------------------------------------------------


def test_valid_v3_package_lock_passes(tmp_path, monkeypatch, capsys):
    content = json.dumps(
        {
            "name": "my-recipe",
            "version": "1.0.0",
            "lockfileVersion": 3,
            "packages": {
                "": {
                    "name": "my-recipe",
                    "version": "1.0.0",
                },
                "node_modules/@types/node": {
                    "version": "20.10.0",
                    "resolved": "https://registry.npmjs.org/@types/node/-/node-20.10.0.tgz",
                    "integrity": _SHA512_HASH,
                },
                "node_modules/typescript": {
                    "version": "5.3.3",
                    "resolved": "https://registry.npmjs.org/typescript/-/typescript-5.3.3.tgz",
                    "integrity": _SHA256_HASH,
                },
            },
        }
    )
    path = _lockfile(tmp_path, "package-lock.json", content)
    assert _run(path, monkeypatch) == EXIT_OK
    out = capsys.readouterr().out
    assert "[PASS]" in out
    assert "::error" not in out


def test_valid_v2_package_lock_passes(tmp_path, monkeypatch, capsys):
    content = json.dumps(
        {
            "name": "my-recipe",
            "version": "1.0.0",
            "lockfileVersion": 2,
            "packages": {
                "": {"name": "my-recipe", "version": "1.0.0"},
                "node_modules/express": {
                    "version": "4.18.2",
                    "resolved": "https://registry.npmjs.org/express/-/express-4.18.2.tgz",
                    "integrity": _SHA512_HASH,
                },
            },
            "dependencies": {
                "express": {
                    "version": "4.18.2",
                    "resolved": "https://registry.npmjs.org/express/-/express-4.18.2.tgz",
                    "integrity": _SHA512_HASH,
                }
            },
        }
    )
    path = _lockfile(tmp_path, "package-lock.json", content)
    assert _run(path, monkeypatch) == EXIT_OK
    assert "::error" not in capsys.readouterr().out


def test_valid_v1_package_lock_passes(tmp_path, monkeypatch, capsys):
    content = json.dumps(
        {
            "name": "my-recipe",
            "version": "1.0.0",
            "lockfileVersion": 1,
            "dependencies": {
                "express": {
                    "version": "4.18.2",
                    "resolved": "https://registry.npmjs.org/express/-/express-4.18.2.tgz",
                    "integrity": _SHA512_HASH,
                    "dependencies": {
                        "accepts": {
                            "version": "1.3.8",
                            "resolved": "https://registry.npmjs.org/accepts/-/accepts-1.3.8.tgz",
                            "integrity": _SHA512_HASH,
                        }
                    },
                }
            },
        }
    )
    path = _lockfile(tmp_path, "package-lock.json", content)
    assert _run(path, monkeypatch) == EXIT_OK
    assert "::error" not in capsys.readouterr().out


def test_npm_shrinkwrap_passes(tmp_path, monkeypatch):
    content = json.dumps(
        {
            "name": "my-recipe",
            "version": "1.0.0",
            "lockfileVersion": 3,
            "packages": {
                "": {"name": "my-recipe", "version": "1.0.0"},
                "node_modules/foo": {
                    "version": "1.0.0",
                    "resolved": "https://registry.npmjs.org/foo/-/foo-1.0.0.tgz",
                    "integrity": _SHA512_HASH,
                },
            },
        }
    )
    path = _lockfile(tmp_path, "npm-shrinkwrap.json", content)
    assert _run(path, monkeypatch) == EXIT_OK


def test_recipe_dir_with_package_lock_passes(tmp_path, monkeypatch, capsys):
    content = json.dumps(
        {
            "name": "my-recipe",
            "version": "1.0.0",
            "lockfileVersion": 3,
            "packages": {
                "": {"name": "my-recipe", "version": "1.0.0"},
                "node_modules/foo": {
                    "version": "1.0.0",
                    "resolved": "https://registry.npmjs.org/foo/-/foo-1.0.0.tgz",
                    "integrity": _SHA512_HASH,
                },
            },
        }
    )
    _lockfile(tmp_path, "package-lock.json", content)
    assert _run(tmp_path, monkeypatch) == EXIT_OK
    assert "[PASS]" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# Exemptions: root, workspace packages, and internal symlinks
# ---------------------------------------------------------------------------


def test_root_and_workspace_and_link_entries_exempt(
    tmp_path, monkeypatch, capsys
):
    content = json.dumps(
        {
            "name": "workspace-root",
            "version": "1.0.0",
            "lockfileVersion": 3,
            "packages": {
                # Root package has no resolved and no integrity
                "": {
                    "name": "workspace-root",
                    "version": "1.0.0",
                    "workspaces": ["packages/*"],
                },
                # Workspace package has no resolved and no integrity
                "packages/shared-lib": {
                    "name": "shared-lib",
                    "version": "1.0.0",
                },
                # Internal workspace symlink has resolved to local path, link: true, no integrity
                "node_modules/shared-lib": {
                    "resolved": "packages/shared-lib",
                    "link": True,
                },
                # Regular dependency
                "node_modules/lodash": {
                    "version": "4.17.21",
                    "resolved": "https://registry.npmjs.org/lodash/-/lodash-4.17.21.tgz",
                    "integrity": _SHA512_HASH,
                },
            },
        }
    )
    path = _lockfile(tmp_path, "package-lock.json", content)
    assert _run(path, monkeypatch) == EXIT_OK
    assert "::error" not in capsys.readouterr().out


# ---------------------------------------------------------------------------
# Non-public registry checks and URL anchoring
# ---------------------------------------------------------------------------


def test_private_registry_url_is_reported(tmp_path, monkeypatch, capsys):
    content = json.dumps(
        {
            "name": "my-recipe",
            "version": "1.0.0",
            "lockfileVersion": 3,
            "packages": {
                "": {"name": "my-recipe", "version": "1.0.0"},
                "node_modules/corp-pkg": {
                    "version": "1.0.0",
                    "resolved": "https://npm.corp.internal.example/corp-pkg/-/corp-pkg-1.0.0.tgz",
                    "integrity": _SHA512_HASH,
                },
            },
        }
    )
    path = _lockfile(tmp_path, "package-lock.json", content)
    assert _run(path, monkeypatch) == EXIT_VIOLATIONS
    out = capsys.readouterr().out
    assert "corp-pkg@1.0.0 resolves from a non-public registry" in out
    assert "npm.corp.internal.example" in out
    assert f"::error file={path}::" in out


def test_anchoring_attacker_domain_is_reported(tmp_path, monkeypatch, capsys):
    content = json.dumps(
        {
            "name": "my-recipe",
            "version": "1.0.0",
            "lockfileVersion": 3,
            "packages": {
                "": {"name": "my-recipe", "version": "1.0.0"},
                "node_modules/evil-pkg": {
                    "version": "1.0.0",
                    "resolved": "https://registry.npmjs.org.attacker.example/evil-pkg-1.0.0.tgz",
                    "integrity": _SHA512_HASH,
                },
            },
        }
    )
    path = _lockfile(tmp_path, "package-lock.json", content)
    assert _run(path, monkeypatch) == EXIT_VIOLATIONS
    out = capsys.readouterr().out
    assert "registry.npmjs.org.attacker.example" in out
    assert "resolves from a non-public registry" in out


def test_anchoring_path_or_query_containing_npmjs_is_reported(
    tmp_path, monkeypatch, capsys
):
    content = json.dumps(
        {
            "name": "my-recipe",
            "version": "1.0.0",
            "lockfileVersion": 3,
            "packages": {
                "": {"name": "my-recipe", "version": "1.0.0"},
                "node_modules/proxy-pkg": {
                    "version": "1.0.0",
                    "resolved": "https://evil.example/registry.npmjs.org/proxy-pkg-1.0.0.tgz",
                    "integrity": _SHA512_HASH,
                },
            },
        }
    )
    path = _lockfile(tmp_path, "package-lock.json", content)
    assert _run(path, monkeypatch) == EXIT_VIOLATIONS
    out = capsys.readouterr().out
    assert "evil.example" in out
    assert "resolves from a non-public registry" in out


# ---------------------------------------------------------------------------
# Git / VCS dependencies
# ---------------------------------------------------------------------------


def test_git_dependencies_are_reported(tmp_path, monkeypatch, capsys):
    content = json.dumps(
        {
            "name": "my-recipe",
            "version": "1.0.0",
            "lockfileVersion": 3,
            "packages": {
                "": {"name": "my-recipe", "version": "1.0.0"},
                "node_modules/git-pkg-1": {
                    "version": "1.0.0",
                    "resolved": "git+https://github.com/org/repo.git#main",
                },
                "node_modules/git-pkg-2": {
                    "version": "github:user/project#1234567",
                },
                "node_modules/git-pkg-3": {
                    "version": "1.0.0",
                    "resolved": "git://github.com/org/repo.git",
                },
            },
        }
    )
    path = _lockfile(tmp_path, "package-lock.json", content)
    assert _run(path, monkeypatch) == EXIT_VIOLATIONS
    out = capsys.readouterr().out
    assert (
        "is a Git/VCS dependency: git+https://github.com/org/repo.git#main"
        in out
    )
    assert "is a Git/VCS dependency: github:user/project#1234567" in out
    assert "is a Git/VCS dependency: git://github.com/org/repo.git" in out


# ---------------------------------------------------------------------------
# Local file: and link: dependencies
# ---------------------------------------------------------------------------


def test_file_and_link_dependencies_are_reported(tmp_path, monkeypatch, capsys):
    content = json.dumps(
        {
            "name": "my-recipe",
            "version": "1.0.0",
            "lockfileVersion": 3,
            "packages": {
                "": {"name": "my-recipe", "version": "1.0.0"},
                "node_modules/local-file": {
                    "version": "1.0.0",
                    "resolved": "file:../local-pkg",
                },
                "node_modules/local-link": {
                    "version": "link:../other-pkg",
                },
            },
        }
    )
    path = _lockfile(tmp_path, "package-lock.json", content)
    assert _run(path, monkeypatch) == EXIT_VIOLATIONS
    out = capsys.readouterr().out
    assert "is a local file/link dependency: file:../local-pkg" in out
    assert "is a local file/link dependency: link:../other-pkg" in out


# ---------------------------------------------------------------------------
# Integrity hash checks
# ---------------------------------------------------------------------------


def test_missing_integrity_on_resolved_entry_is_reported(
    tmp_path, monkeypatch, capsys
):
    content = json.dumps(
        {
            "name": "my-recipe",
            "version": "1.0.0",
            "lockfileVersion": 3,
            "packages": {
                "": {"name": "my-recipe", "version": "1.0.0"},
                "node_modules/foo": {
                    "version": "1.0.0",
                    "resolved": "https://registry.npmjs.org/foo/-/foo-1.0.0.tgz",
                },
            },
        }
    )
    path = _lockfile(tmp_path, "package-lock.json", content)
    assert _run(path, monkeypatch) == EXIT_VIOLATIONS
    out = capsys.readouterr().out
    assert "has a resolved URL but no `integrity` hash" in out


def test_bad_integrity_is_reported(tmp_path, monkeypatch, capsys):
    content = json.dumps(
        {
            "name": "my-recipe",
            "version": "1.0.0",
            "lockfileVersion": 3,
            "packages": {
                "": {"name": "my-recipe", "version": "1.0.0"},
                "node_modules/foo": {
                    "version": "1.0.0",
                    "resolved": "https://registry.npmjs.org/foo/-/foo-1.0.0.tgz",
                    "integrity": "md5:deadbeef",
                },
                "node_modules/bar": {
                    "version": "1.0.0",
                    "integrity": None,
                },
            },
        }
    )
    path = _lockfile(tmp_path, "package-lock.json", content)
    assert _run(path, monkeypatch) == EXIT_VIOLATIONS
    out = capsys.readouterr().out
    assert "has an unrecognised integrity hash: 'md5:deadbeef'" in out
    assert "has an unrecognised integrity hash: 'None'" in out


# ---------------------------------------------------------------------------
# Structural sanity and shape errors
# ---------------------------------------------------------------------------


def test_truncated_json_produces_one_shape_error(tmp_path, monkeypatch, capsys):
    path = _lockfile(
        tmp_path, "package-lock.json", '{"name": "broken", "packages": {'
    )
    assert _run(path, monkeypatch) == EXIT_VIOLATIONS
    out = capsys.readouterr().out
    assert "is not valid JSON" in out
    assert out.count("::error file=") == 1
    assert "Traceback" not in out


def test_empty_lockfile_produces_shape_error(tmp_path, monkeypatch, capsys):
    path = _lockfile(tmp_path, "package-lock.json", "")
    assert _run(path, monkeypatch) == EXIT_VIOLATIONS
    out = capsys.readouterr().out
    assert "is empty" in out


def test_non_dict_top_level_produces_shape_error(tmp_path, monkeypatch, capsys):
    path = _lockfile(tmp_path, "package-lock.json", '["not-an-object"]')
    assert _run(path, monkeypatch) == EXIT_VIOLATIONS
    out = capsys.readouterr().out
    assert "Top-level structure is a list" in out


def test_non_dict_packages_produces_shape_error(tmp_path, monkeypatch, capsys):
    content = json.dumps({"packages": ["not-a-dict"]})
    path = _lockfile(tmp_path, "package-lock.json", content)
    assert _run(path, monkeypatch) == EXIT_VIOLATIONS
    out = capsys.readouterr().out
    assert "`packages` is a list" in out
    assert "not an object" in out


def test_non_dict_package_entry_produces_shape_error(
    tmp_path, monkeypatch, capsys
):
    content = json.dumps({"packages": {"node_modules/foo": "not-a-dict-entry"}})
    path = _lockfile(tmp_path, "package-lock.json", content)
    assert _run(path, monkeypatch) == EXIT_VIOLATIONS
    out = capsys.readouterr().out
    assert "Package entry 'node_modules/foo' is a str" in out


def test_unrecognised_lockfile_version_produces_shape_error(
    tmp_path, monkeypatch, capsys
):
    content = json.dumps({"lockfileVersion": 99, "packages": {}})
    path = _lockfile(tmp_path, "package-lock.json", content)
    assert _run(path, monkeypatch) == EXIT_VIOLATIONS
    out = capsys.readouterr().out
    assert "Unrecognised lockfileVersion: 99" in out


# ---------------------------------------------------------------------------
# Unsupported formats
# ---------------------------------------------------------------------------


def test_unsupported_pnpm_lock_is_reported(tmp_path, monkeypatch, capsys):
    path = _lockfile(
        tmp_path, "pnpm-lock.yaml", "lockfileVersion: '6.0'\ndependencies:\n"
    )
    assert _run(path, monkeypatch) == EXIT_VIOLATIONS
    out = capsys.readouterr().out
    assert "uses an unsupported lockfile format: pnpm (pnpm-lock.yaml)" in out


def test_unsupported_yarn_lock_is_reported(tmp_path, monkeypatch, capsys):
    path = _lockfile(tmp_path, "yarn.lock", "# yarn lockfile v1\n")
    assert _run(path, monkeypatch) == EXIT_VIOLATIONS
    out = capsys.readouterr().out
    assert "uses an unsupported lockfile format: Yarn (yarn.lock)" in out


def test_unsupported_bun_lock_is_reported(tmp_path, monkeypatch, capsys):
    path = _lockfile(tmp_path, "bun.lock", '{"lockfileVersion": 1}\n')
    assert _run(path, monkeypatch) == EXIT_VIOLATIONS
    out = capsys.readouterr().out
    assert "uses an unsupported lockfile format: Bun (bun.lock)" in out


def test_recipe_dir_with_unsupported_format_is_reported(
    tmp_path, monkeypatch, capsys
):
    _lockfile(tmp_path, "pnpm-lock.yaml", "lockfileVersion: '6.0'\n")
    assert _run(tmp_path, monkeypatch) == EXIT_VIOLATIONS
    out = capsys.readouterr().out
    assert "unsupported lockfile format: pnpm" in out


# ---------------------------------------------------------------------------
# Missing lockfile in recipe directory
# ---------------------------------------------------------------------------


def test_missing_lockfile_in_recipe_dir_is_reported(
    tmp_path, monkeypatch, capsys
):
    _lockfile(tmp_path, "package.json", '{"name": "my-recipe"}\n')
    assert _run(tmp_path, monkeypatch) == EXIT_VIOLATIONS
    out = capsys.readouterr().out
    assert f"No lockfile found in {tmp_path}" in out


# ---------------------------------------------------------------------------
# CI faults
# ---------------------------------------------------------------------------


def test_missing_target_is_ci_fault(tmp_path, monkeypatch, capsys):
    assert _run(tmp_path / "nonexistent.json", monkeypatch) == EXIT_CI_FAULT
    out = capsys.readouterr().out
    assert "[ci-fault]" in out
    assert "::error file=" not in out


def test_wrong_argument_count_is_ci_fault(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["check_lockfile_npm.py"])
    assert m.main() == EXIT_CI_FAULT
    assert "[ci-fault]" in capsys.readouterr().out


def test_unexpected_crash_is_ci_fault(tmp_path, monkeypatch, capsys):
    def boom(*_args, **_kwargs):
        raise RuntimeError("simulated crash")

    monkeypatch.setattr(m, "_check_file", boom)
    path = _lockfile(
        tmp_path, "package-lock.json", '{"name": "foo", "packages": {}}'
    )
    assert _run(path, monkeypatch) == EXIT_CI_FAULT
    out = capsys.readouterr().out
    assert "RuntimeError: simulated crash" in out
    assert "::error file=" not in out
