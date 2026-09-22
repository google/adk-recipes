#!/usr/bin/env python3
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
"""Unit tests for tools/check_github_dir_changes.py."""

from __future__ import annotations

import io
import json
import urllib.error
from unittest.mock import MagicMock, patch

import check_github_dir_changes as m
import pytest

# ---------------------------------------------------------------------------
# find_github_files
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("changed", "expected"),
    [
        (
            [
                ".github/workflows/foo.yml",
                ".github/policy.yml",
                "core/python/rag/agent.py",
                "README.md",
            ],
            [".github/workflows/foo.yml", ".github/policy.yml"],
        ),
        (
            [
                "./.github/workflows/bar.yml",
                "/.github/scripts/test.py",
            ],
            [".github/workflows/bar.yml", ".github/scripts/test.py"],
        ),
        (
            [
                ".github-extra/file.txt",
                "github/workflow.yml",
                "core/python/app.py",
            ],
            [],
        ),
        (
            [
                "",
                "   ",
                ".github/workflows/a.yml",
                ".github/workflows/a.yml",
            ],
            [".github/workflows/a.yml"],
        ),
        # git C-quotes any path containing non-ASCII bytes. Left quoted, the
        # leading '"' becomes part of the first path component and the file
        # walks straight past the .github/ prefix match.
        (
            [r'".github/workflows/w\303\266rk.yml"'],
            [".github/workflows/wörk.yml"],
        ),
        (
            [r'"docs/w\303\266rk.md"'],
            [],
        ),
    ],
)
def test_find_github_files(changed, expected):
    assert m.find_github_files(changed) == expected


# ---------------------------------------------------------------------------
# check_is_admin
# ---------------------------------------------------------------------------


def test_is_admin_override():
    assert m.check_is_admin("user", is_admin_override=True) is True
    assert m.check_is_admin("user", is_admin_override=False) is False


def test_is_admin_owner_association():
    assert (
        m.check_is_admin("user", repo="owner/repo", author_association="OWNER")
        is True
    )


def test_is_admin_missing_author_or_repo():
    assert m.check_is_admin("", repo="owner/repo") is False
    assert m.check_is_admin("user", repo="") is False


def test_is_admin_api_permission_admin():
    response_data = json.dumps(
        {"permission": "admin", "permissions": {"admin": True}}
    ).encode("utf-8")
    mock_resp = MagicMock()
    mock_resp.read.return_value = response_data
    mock_resp.__enter__.return_value = mock_resp

    with patch("urllib.request.urlopen", return_value=mock_resp):
        assert (
            m.check_is_admin(
                "admin_user",
                repo="google/adk-samples",
                token="test_token",
            )
            is True
        )


def test_is_admin_api_role_name_admin():
    response_data = json.dumps(
        {"permission": "read", "role_name": "admin"}
    ).encode("utf-8")
    mock_resp = MagicMock()
    mock_resp.read.return_value = response_data
    mock_resp.__enter__.return_value = mock_resp

    with patch("urllib.request.urlopen", return_value=mock_resp):
        assert (
            m.check_is_admin(
                "admin_user",
                repo="google/adk-samples",
            )
            is True
        )


def test_is_admin_api_non_admin():
    response_data = json.dumps(
        {
            "permission": "write",
            "role_name": "write",
            "permissions": {"admin": False, "push": True},
        }
    ).encode("utf-8")
    mock_resp = MagicMock()
    mock_resp.read.return_value = response_data
    mock_resp.__enter__.return_value = mock_resp

    with (
        patch("urllib.request.urlopen", return_value=mock_resp),
        patch("shutil.which", return_value=None),
    ):
        assert (
            m.check_is_admin(
                "collaborator",
                repo="google/adk-samples",
            )
            is False
        )


@pytest.mark.parametrize(
    "association",
    [
        "CONTRIBUTOR",
        "FIRST_TIME_CONTRIBUTOR",
        "FIRST_TIMER",
        "MANNEQUIN",
        "NONE",
    ],
)
def test_non_admin_association_resolves_after_lookups_fail(association):
    """With nothing lookupable, the trusted payload still gives an answer."""
    with (
        patch(
            "urllib.request.urlopen",
            side_effect=urllib.error.URLError("no network"),
        ),
        patch("shutil.which", return_value=None),
    ):
        assert (
            m.check_is_admin(
                "outsider",
                repo="google/adk-samples",
                author_association=association,
            )
            is False
        )


def test_api_answer_overrules_the_association_inference():
    """A real 200 beats what CONTRIBUTOR would otherwise imply."""
    response_data = json.dumps({"permission": "admin"}).encode("utf-8")
    mock_resp = MagicMock()
    mock_resp.read.return_value = response_data
    mock_resp.__enter__.return_value = mock_resp

    with patch("urllib.request.urlopen", return_value=mock_resp):
        assert (
            m.check_is_admin(
                "concealed_admin",
                repo="google/adk-samples",
                author_association="CONTRIBUTOR",
            )
            is True
        )


@pytest.mark.parametrize("association", ["MEMBER", "COLLABORATOR"])
def test_member_and_collaborator_still_query_the_api(association):
    """Either may hold admin via a team or a direct grant."""
    response_data = json.dumps({"permission": "admin"}).encode("utf-8")
    mock_resp = MagicMock()
    mock_resp.read.return_value = response_data
    mock_resp.__enter__.return_value = mock_resp

    with patch("urllib.request.urlopen", return_value=mock_resp) as urlopen:
        assert (
            m.check_is_admin(
                "someone",
                repo="google/adk-samples",
                author_association=association,
            )
            is True
        )
    urlopen.assert_called_once()


@pytest.mark.parametrize("code", [401, 403, 429, 500, 502])
def test_is_admin_undetermined_on_non_404_http_error(code):
    """A token that cannot ask is not the same answer as 'not an admin'."""
    http_error = urllib.error.HTTPError(
        url="http://api.github.com",
        code=code,
        msg="nope",
        hdrs={},
        fp=io.BytesIO(b""),
    )
    with (
        patch("urllib.request.urlopen", side_effect=http_error),
        patch("shutil.which", return_value=None),
    ):
        assert m.check_is_admin("admin_user", repo="google/adk-samples") is None


def test_is_admin_undetermined_when_network_and_cli_both_fail():
    with (
        patch(
            "urllib.request.urlopen",
            side_effect=urllib.error.URLError("connection refused"),
        ),
        patch("shutil.which", return_value="/usr/bin/gh"),
        patch(
            "subprocess.run", return_value=MagicMock(returncode=1, stdout="")
        ),
    ):
        assert m.check_is_admin("admin_user", repo="google/adk-samples") is None


def test_is_admin_api_404_returns_false():
    http_error = urllib.error.HTTPError(
        url="http://api.github.com",
        code=404,
        msg="Not Found",
        hdrs={},
        fp=io.BytesIO(b""),
    )
    with (
        patch("urllib.request.urlopen", side_effect=http_error),
        patch("shutil.which", return_value=None),
    ):
        assert (
            m.check_is_admin(
                "outsider",
                repo="google/adk-samples",
            )
            is False
        )


def test_is_admin_gh_cli_fallback():
    http_error = urllib.error.URLError("connection refused")
    cli_output = json.dumps(
        {"permission": "admin", "permissions": {"admin": True}}
    )
    mock_process = MagicMock(returncode=0, stdout=cli_output)

    with (
        patch("urllib.request.urlopen", side_effect=http_error),
        patch("shutil.which", return_value="/usr/bin/gh"),
        patch("subprocess.run", return_value=mock_process),
    ):
        assert (
            m.check_is_admin(
                "admin_user",
                repo="google/adk-samples",
            )
            is True
        )


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


def test_main_passes_when_no_github_files_changed(monkeypatch, capsys):
    monkeypatch.setattr(
        "sys.stdin", io.StringIO("core/python/foo/agent.py\nREADME.md\n")
    )
    code = m.main([])
    assert code == 0
    assert "[PASS] No files under .github/ modified in this PR." in (
        capsys.readouterr().out
    )


def test_main_passes_when_admin_modifies_github_files(monkeypatch, capsys):
    monkeypatch.setattr(
        "sys.stdin", io.StringIO(".github/workflows/global-checks.yml\n")
    )
    code = m.main(["--author", "happyhuman", "--is-admin", "true"])
    assert code == 0
    out = capsys.readouterr().out
    assert "[PASS]" in out
    assert "happyhuman" in out
    assert "repository administrator" in out


def test_main_passes_for_owner_association(monkeypatch, capsys):
    monkeypatch.setattr(
        "sys.stdin", io.StringIO(".github/workflows/global-checks.yml\n")
    )
    code = m.main(
        [
            "--author",
            "repo-owner",
            "--author-association",
            "OWNER",
            "--repo",
            "google/adk-samples",
        ]
    )
    assert code == 0
    assert "[PASS]" in capsys.readouterr().out


def test_main_fails_when_non_admin_modifies_github_files(monkeypatch, capsys):
    monkeypatch.setattr(
        "sys.stdin",
        io.StringIO(
            ".github/workflows/python-tests.yml\ncore/python/foo/agent.py\n"
        ),
    )
    code = m.main(
        [
            "--author",
            "contributor123",
            "--author-association",
            "CONTRIBUTOR",
            "--repo",
            "google/adk-samples",
            "--is-admin",
            "false",
        ]
    )
    assert code == 1
    out = capsys.readouterr().out
    assert "::error file=.github/workflows/python-tests.yml::" in out
    assert "::error file=core/python/foo/agent.py::" not in out
    assert "contributor123" in out
    assert "ACTION REQUIRED" in out
    assert (
        "troubleshooting.md#only-repository-admins-may-modify-files-under-github"
        in (out)
    )


def test_main_fails_with_multiple_github_files(monkeypatch, capsys):
    monkeypatch.setattr(
        "sys.stdin",
        io.StringIO(
            ".github/workflows/python-tests.yml\n.github/policy.yml\n.github/CODEOWNERS\n"
        ),
    )
    code = m.main(
        [
            "--author",
            "contributor123",
            "--is-admin",
            "false",
        ]
    )
    assert code == 1
    out = capsys.readouterr().out
    assert out.count("::error file=.github/") == 3
    assert "3 unauthorized files modified under .github/" in out


def test_main_reports_ci_fault_when_admin_status_undetermined(
    monkeypatch, capsys
):
    """Fail closed, but do not tell an administrator they are not one."""
    monkeypatch.setattr(
        "sys.stdin", io.StringIO(".github/workflows/global-checks.yml\n")
    )
    with patch.object(m, "check_is_admin", return_value=None):
        code = m.main(
            [
                "--author",
                "happyhuman",
                "--author-association",
                "MEMBER",
                "--repo",
                "google/adk-samples",
            ]
        )

    assert code == 2
    out = capsys.readouterr().out
    assert "[CI FAULT]" in out
    assert "Could not determine" in out
    # Never an accusation, and never an annotation on a contributor's file.
    assert "does not have administrator permissions" not in out
    assert "::error file=" not in out


def test_main_reads_from_changed_files_flag(tmp_path, capsys):
    changed_file = tmp_path / "changed.txt"
    changed_file.write_text(".github/workflows/test.yml\n", encoding="utf-8")

    code = m.main(
        [
            "--changed-files",
            str(changed_file),
            "--author",
            "contributor123",
            "--is-admin",
            "false",
        ]
    )
    assert code == 1
    assert (
        "::error file=.github/workflows/test.yml::" in capsys.readouterr().out
    )
