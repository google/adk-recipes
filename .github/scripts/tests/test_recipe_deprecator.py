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
"""Unit tests for .github/scripts/recipe_deprecator.py."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import MagicMock, patch

import recipe_deprecator as rd
import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
WORKFLOW_PATH = REPO_ROOT / ".github" / "workflows" / "recipe-deprecator.yml"
POLICY_PATH = REPO_ROOT / ".github" / "policy.yml"


# ---------------------------------------------------------------------------
# Policy & Repo Admin Tests
# ---------------------------------------------------------------------------


def test_load_policy_reads_real_policy_file():
    """Loads the real repository policy.yml."""
    policy = rd.load_policy(POLICY_PATH)
    assert isinstance(policy, dict)
    repo_admin = policy.get("repo_admin")
    assert isinstance(repo_admin, str)
    assert len(repo_admin) > 0


def test_get_repo_admin_from_policy_dict():
    assert rd.get_repo_admin({"repo_admin": "alice"}) == "alice"


def test_get_repo_admin_fallback_on_missing_or_empty(tmp_path):
    missing_file = tmp_path / "missing_policy.yml"
    assert rd.get_repo_admin(policy_path=missing_file) == rd.DEFAULT_REPO_ADMIN

    empty_file = tmp_path / "empty_policy.yml"
    empty_file.write_text("", encoding="utf-8")
    assert rd.get_repo_admin(policy_path=empty_file) == rd.DEFAULT_REPO_ADMIN

    no_admin_file = tmp_path / "no_admin.yml"
    no_admin_file.write_text("foo: bar\n", encoding="utf-8")
    assert rd.get_repo_admin(policy_path=no_admin_file) == rd.DEFAULT_REPO_ADMIN


# ---------------------------------------------------------------------------
# Manifest Mtime Tests
# ---------------------------------------------------------------------------


def test_get_manifest_mtime_git_success(tmp_path):
    manifest = tmp_path / "manifest.yaml"
    manifest.write_text("status: active\n", encoding="utf-8")

    fixed_ts = 1700000000
    mock_res = MagicMock()
    mock_res.returncode = 0
    mock_res.stdout = f"{fixed_ts}\n"

    with patch("subprocess.run", return_value=mock_res):
        mtime = rd.get_manifest_mtime(manifest, repo_root=tmp_path)
        assert mtime == datetime.fromtimestamp(fixed_ts, tz=UTC)


def test_get_manifest_mtime_fallback_to_file_stat(tmp_path):
    manifest = tmp_path / "manifest.yaml"
    manifest.write_text("status: active\n", encoding="utf-8")

    mock_res = MagicMock()
    mock_res.returncode = 1
    mock_res.stdout = ""

    with patch("subprocess.run", return_value=mock_res):
        mtime = rd.get_manifest_mtime(manifest, repo_root=tmp_path)
        expected = datetime.fromtimestamp(manifest.stat().st_mtime, tz=UTC)
        assert abs((mtime - expected).total_seconds()) < 1.0


# ---------------------------------------------------------------------------
# Scan Recipes Tests
# ---------------------------------------------------------------------------


def test_scan_recipes(tmp_path):
    core_recipe = tmp_path / "core" / "python" / "recipe-a"
    core_recipe.mkdir(parents=True)
    (core_recipe / "manifest.yaml").write_text(
        "type: standalone\nstatus: active\n", encoding="utf-8"
    )

    contrib_recipe = tmp_path / "contrib" / "python" / "recipe-b"
    contrib_recipe.mkdir(parents=True)
    (contrib_recipe / "manifest.yaml").write_text(
        "type: standalone\nstatus: inactive\n", encoding="utf-8"
    )

    skill_recipe = tmp_path / "skills" / "retail" / "recipe-c"
    skill_recipe.mkdir(parents=True)
    (skill_recipe / "manifest.yaml").write_text(
        "type: standalone\nstatus: inactive\n", encoding="utf-8"
    )

    # Skipped directory
    skipped = tmp_path / "core" / ".venv" / "bad-recipe"
    skipped.mkdir(parents=True)
    (skipped / "manifest.yaml").write_text("status: active\n", encoding="utf-8")

    recipes = rd.scan_recipes(repo_root=tmp_path)
    rel_paths = [r.rel_path for r in recipes]

    assert rel_paths == [
        "contrib/python/recipe-b",
        "core/python/recipe-a",
        "skills/retail/recipe-c",
    ]
    assert recipes[0].status == "inactive"
    assert recipes[1].status == "active"
    assert recipes[2].status == "inactive"


# ---------------------------------------------------------------------------
# Classification Tests
# ---------------------------------------------------------------------------


def test_classify_recipe():
    now = datetime(2026, 9, 25, 12, 0, 0, tzinfo=UTC)

    # Active status -> always active
    mtime_old = now - timedelta(days=100)
    assert rd.classify_recipe("active", mtime_old, now=now) == "active"
    assert rd.classify_recipe("ACTIVE", mtime_old, now=now) == "active"

    # Inactive under 60 days -> inactive_recent
    mtime_recent = now - timedelta(days=30)
    assert (
        rd.classify_recipe("inactive", mtime_recent, now=now, threshold_days=60)
        == "inactive_recent"
    )
    mtime_59d = now - timedelta(days=59, hours=23)
    assert (
        rd.classify_recipe("inactive", mtime_59d, now=now, threshold_days=60)
        == "inactive_recent"
    )

    # Inactive 60+ days -> inactive_expired
    mtime_60d = now - timedelta(days=60)
    assert (
        rd.classify_recipe("inactive", mtime_60d, now=now, threshold_days=60)
        == "inactive_expired"
    )
    mtime_90d = now - timedelta(days=90)
    assert (
        rd.classify_recipe("inactive", mtime_90d, now=now, threshold_days=60)
        == "inactive_expired"
    )

    # Unknown status
    assert rd.classify_recipe("other", mtime_old, now=now) == "unknown"


# ---------------------------------------------------------------------------
# PR Matching Tests
# ---------------------------------------------------------------------------


def test_find_existing_deletion_pr():
    recipe_path = "contrib/python/small-business-loan-agent"
    branch_name = f"deprecate/{recipe_path}"

    prs = [
        {
            "number": 101,
            "title": f"Deprecate recipe: {recipe_path}",
            "headRefName": branch_name,
            "state": "OPEN",
            "mergedAt": None,
        },
        {
            "number": 100,
            "title": f"Deprecate recipe: {recipe_path}",
            "headRefName": branch_name,
            "state": "CLOSED",
            "mergedAt": None,
        },
    ]

    # Finds open PR first
    match = rd.find_existing_deletion_pr(recipe_path, prs)
    assert match is not None
    assert match["number"] == 101

    # When no open PR, finds closed unmerged PR
    closed_only = [prs[1]]
    match = rd.find_existing_deletion_pr(recipe_path, closed_only)
    assert match is not None
    assert match["number"] == 100

    # Merged PRs are ignored (not returned for reopen)
    merged_pr = [
        {
            "number": 99,
            "title": f"Deprecate recipe: {recipe_path}",
            "headRefName": branch_name,
            "state": "CLOSED",
            "mergedAt": "2026-08-01T00:00:00Z",
        }
    ]
    assert rd.find_existing_deletion_pr(recipe_path, merged_pr) is None

    # Overlapping prefix names do not falsely match
    overlap_prs = [
        {
            "number": 103,
            "title": "Deprecate recipe: contrib/python/recipe-a-advanced",
            "headRefName": "deprecate/contrib/python/recipe-a-advanced",
            "state": "OPEN",
            "mergedAt": None,
        }
    ]
    assert (
        rd.find_existing_deletion_pr("contrib/python/recipe-a", overlap_prs)
        is None
    )

    # Unrelated PRs don't match
    unrelated = [
        {
            "number": 102,
            "title": "Fix bug in store-ops",
            "headRefName": "fix/store-ops",
            "state": "OPEN",
            "mergedAt": None,
        }
    ]
    assert rd.find_existing_deletion_pr(recipe_path, unrelated) is None


# ---------------------------------------------------------------------------
# Reopen & Assign & PR Creation Tests
# ---------------------------------------------------------------------------


def test_reopen_pr_dry_run():
    assert rd.reopen_pr(101, "happyhuman", dry_run=True) is True


def test_reopen_pr_success():
    mock_res = MagicMock()
    mock_res.returncode = 0
    with patch("subprocess.run", return_value=mock_res):
        assert rd.reopen_pr(101, "happyhuman", dry_run=False) is True


def test_create_deletion_pr_dry_run(tmp_path):
    recipe = rd.RecipeInfo(
        rel_path="contrib/python/foo",
        dir_path=tmp_path / "contrib" / "python" / "foo",
        manifest_path=tmp_path / "contrib" / "python" / "foo" / "manifest.yaml",
        status="inactive",
        mtime=datetime.now(UTC) - timedelta(days=90),
    )
    result = rd.create_deletion_pr(
        recipe, "happyhuman", repo_root=tmp_path, dry_run=True
    )
    assert result is None


def test_create_deletion_pr_success(tmp_path):
    recipe_dir = tmp_path / "contrib" / "python" / "foo"
    recipe_dir.mkdir(parents=True)
    manifest = recipe_dir / "manifest.yaml"
    manifest.write_text("status: inactive\n", encoding="utf-8")

    recipe = rd.RecipeInfo(
        rel_path="contrib/python/foo",
        dir_path=recipe_dir,
        manifest_path=manifest,
        status="inactive",
        mtime=datetime.now(UTC) - timedelta(days=90),
    )

    mock_res = MagicMock()
    mock_res.returncode = 0
    mock_res.stdout = "https://github.com/google/adk-recipes/pull/999\n"

    with patch("subprocess.run", return_value=mock_res):
        result = rd.create_deletion_pr(
            recipe, "happyhuman", repo_root=tmp_path, dry_run=False
        )
        assert result == 999


# ---------------------------------------------------------------------------
# End-to-End Orchestration Tests
# ---------------------------------------------------------------------------


def test_process_recipes_orchestration(tmp_path):
    policy_file = tmp_path / "policy.yml"
    policy_file.write_text("repo_admin: happyhuman\n", encoding="utf-8")

    now = datetime(2026, 9, 25, 12, 0, 0, tzinfo=UTC)

    # 1. Active recipe
    r_active = tmp_path / "core" / "python" / "active-rec"
    r_active.mkdir(parents=True)
    (r_active / "manifest.yaml").write_text(
        "status: active\n", encoding="utf-8"
    )

    # 2. Inactive recent (30 days ago)
    r_recent = tmp_path / "contrib" / "python" / "recent-rec"
    r_recent.mkdir(parents=True)
    (r_recent / "manifest.yaml").write_text(
        "status: inactive\n", encoding="utf-8"
    )

    # 3. Inactive expired (80 days ago) with existing OPEN PR
    r_open_pr = tmp_path / "contrib" / "python" / "open-pr-rec"
    r_open_pr.mkdir(parents=True)
    (r_open_pr / "manifest.yaml").write_text(
        "status: inactive\n", encoding="utf-8"
    )

    # 4. Inactive expired (90 days ago) with closed unmerged PR -> should reopen
    r_closed_pr = tmp_path / "contrib" / "python" / "closed-pr-rec"
    r_closed_pr.mkdir(parents=True)
    (r_closed_pr / "manifest.yaml").write_text(
        "status: inactive\n", encoding="utf-8"
    )

    # 5. Inactive expired (100 days ago) with no PR -> should open new PR
    r_no_pr = tmp_path / "skills" / "retail" / "new-pr-rec"
    r_no_pr.mkdir(parents=True)
    (r_no_pr / "manifest.yaml").write_text(
        "status: inactive\n", encoding="utf-8"
    )

    def mock_mtime(manifest_path, repo_root):
        rel = manifest_path.relative_to(repo_root).as_posix()
        if "recent-rec" in rel:
            return now - timedelta(days=30)
        if "open-pr-rec" in rel:
            return now - timedelta(days=80)
        if "closed-pr-rec" in rel:
            return now - timedelta(days=90)
        if "new-pr-rec" in rel:
            return now - timedelta(days=100)
        return now - timedelta(days=200)

    mock_prs = [
        {
            "number": 201,
            "title": "Deprecate recipe: contrib/python/open-pr-rec",
            "headRefName": "deprecate/contrib/python/open-pr-rec",
            "state": "OPEN",
            "mergedAt": None,
        },
        {
            "number": 202,
            "title": "Deprecate recipe: contrib/python/closed-pr-rec",
            "headRefName": "deprecate/contrib/python/closed-pr-rec",
            "state": "CLOSED",
            "mergedAt": None,
        },
    ]

    with (
        patch("recipe_deprecator.get_manifest_mtime", side_effect=mock_mtime),
        patch("recipe_deprecator.fetch_prs", return_value=mock_prs),
        patch("recipe_deprecator.reopen_pr", return_value=True) as mock_reopen,
        patch(
            "recipe_deprecator.create_deletion_pr", return_value=301
        ) as mock_create,
    ):
        summary = rd.process_recipes(
            repo_root=tmp_path,
            policy_path=policy_file,
            inactive_days=60,
            dry_run=False,
            now=now,
        )

        assert summary["total_recipes"] == 5
        assert summary["active"] == 1
        assert summary["inactive_recent"] == 1
        assert summary["inactive_expired"] == 3
        assert summary["pr_existing_open"] == 1
        assert summary["pr_reopened"] == 1
        assert summary["pr_opened"] == 1

        mock_reopen.assert_called_once_with(
            202, "happyhuman", repo=None, dry_run=False
        )
        assert mock_create.call_count == 1
        created_recipe = mock_create.call_args[0][0]
        assert created_recipe.rel_path == "skills/retail/new-pr-rec"


# ---------------------------------------------------------------------------
# Workflow Verification Tests
# ---------------------------------------------------------------------------


def test_recipe_deprecator_workflow_properties():
    """Verify GitHub Actions workflow syntax, schedule, and permissions."""
    assert WORKFLOW_PATH.is_file(), f"{WORKFLOW_PATH} not found"
    doc = yaml.safe_load(WORKFLOW_PATH.read_text(encoding="utf-8"))

    # Name
    assert doc.get("name") == "Recipe Deprecator"

    # Triggers: scheduled on 2nd and 16th of every month
    triggers = doc.get("on") or doc.get(True) or {}
    schedules = triggers.get("schedule", [])
    assert any(s.get("cron") == "0 6 2,16 * *" for s in schedules), (
        f"Schedule cron must be '0 6 2,16 * *', got {schedules}"
    )
    assert "workflow_dispatch" in triggers

    # Security: empty permissions at workflow level
    assert doc.get("permissions") == {}

    # Job permissions: contents: write, pull-requests: write, issues: write
    job = doc["jobs"]["deprecate"]
    job_perms = job["permissions"]
    assert job_perms.get("contents") == "write"
    assert job_perms.get("pull-requests") == "write"
    assert job_perms.get("issues") == "write"

    # Concurrency
    assert doc.get("concurrency", {}).get("cancel-in-progress") is False
