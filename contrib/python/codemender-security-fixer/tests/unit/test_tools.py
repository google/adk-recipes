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
"""Unit tests for the scanner, validator, workspace and tools. No model calls.

The *_FIX constants are reference fixes for the bundled sample target. They
double as proof that every vulnerability in the sample is fixable within the
validator's rules.
"""

import hashlib
import os
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

from app import config, scanner, validator, workspace
from app.agent import get_patch_report, list_findings, propose_patch, read_file

TIMEOUT = 60
SKIPS = os.environ["BANDIT_SKIPS"]  # loaded from .env.example by conftest

DB_FIX = '''
import sqlite3

SCHEMA = """
CREATE TABLE IF NOT EXISTS orders (
    id TEXT PRIMARY KEY,
    customer TEXT NOT NULL,
    status TEXT NOT NULL,
    total_cents INTEGER NOT NULL
);
"""

SEED_ORDERS = [
    ("ORD-1001", "acme", "shipped", 129900),
    ("ORD-1002", "acme", "pending", 4500),
    ("ORD-1003", "globex", "shipped", 78000),
    ("ORD-1004", "initech", "cancelled", 1999),
]


def connect(path=":memory:"):
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    conn.executemany("INSERT OR IGNORE INTO orders VALUES (?, ?, ?, ?)", SEED_ORDERS)
    conn.commit()
    return conn


def find_orders(conn, customer, status=None):
    query = "SELECT * FROM orders WHERE customer = ?"
    params = [customer]
    if status:
        query += " AND status = ?"
        params.append(status)
    return [dict(row) for row in conn.execute(query, params)]
'''

# Parameterized, but drops the status filter: fixes the injection and
# breaks a functional test.
DB_BROKEN = DB_FIX.replace(
    """    if status:
        query += " AND status = ?"
        params.append(status)
""",
    "",
)

INVOICES_FIX = """
import os
import re
import subprocess

_MONTH = re.compile(r"\\d{4}-\\d{2}")


def read_invoice(invoice_dir, name):
    base = os.path.realpath(invoice_dir)
    path = os.path.realpath(os.path.join(base, name))
    if os.path.commonpath([base, path]) != base:
        raise ValueError("invoice path escapes the invoice directory")
    with open(path, "rb") as f:
        return f.read()


def archive_invoices(invoice_dir, month, out_dir):
    if not _MONTH.fullmatch(month):
        raise ValueError("month must be YYYY-MM")
    archive = os.path.join(out_dir, f"invoices-{month}.tar.gz")
    subprocess.run(["tar", "-czf", archive, "-C", invoice_dir, month], check=True)
    return archive
"""

# The classic incomplete fix: strip "../" once. "....//" survives it, absolute
# paths are untouched, and the shell injection is still there.
INVOICES_INCOMPLETE = """
import os
import subprocess


def read_invoice(invoice_dir, name):
    path = os.path.join(invoice_dir, name.replace("../", ""))
    with open(path, "rb") as f:
        return f.read()


def archive_invoices(invoice_dir, month, out_dir):
    archive = os.path.join(out_dir, f"invoices-{month}.tar.gz")
    subprocess.run(
        f"tar -czf {archive} -C {invoice_dir} {month}", shell=True, check=True
    )
    return archive
"""

CART_FIX = """
import base64
import json


def dump_cart(cart):
    return base64.b64encode(json.dumps(cart).encode()).decode("ascii")


def load_cart(cookie):
    cart = json.loads(base64.b64decode(cookie))
    if not isinstance(cart, dict) or not all(
        isinstance(k, str) and isinstance(v, int) for k, v in cart.items()
    ):
        raise ValueError("cart must map SKUs to quantities")
    return cart
"""

AUTH_FIX = """
import hmac
import os


def is_admin(authorization_header):
    token = os.environ.get("ORDERS_API_TOKEN")
    if not token or not authorization_header:
        return False
    return hmac.compare_digest(
        authorization_header.encode(), f"Bearer {token}".encode()
    )
"""

ALL_FIXES = {
    "orders/db.py": DB_FIX,
    "orders/invoices.py": INVOICES_FIX,
    "orders/cart.py": CART_FIX,
    "orders/auth.py": AUTH_FIX,
}


def _tree_hash(root: Path) -> str:
    digest = hashlib.sha256()
    for rel in workspace.list_files(root, suffix=""):
        if "__pycache__" in rel:
            continue
        digest.update(rel.encode())
        digest.update((root / rel).read_bytes())
    return digest.hexdigest()


@pytest.fixture(scope="module")
def baseline() -> dict:
    return scanner.scan(str(workspace.BUNDLED_TARGET), TIMEOUT, None, SKIPS)


@pytest.fixture
def work(tmp_path: Path) -> Path:
    return workspace.copy_tree(workspace.BUNDLED_TARGET, str(tmp_path))


@pytest.fixture
def ctx(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    monkeypatch.delenv("TARGET_DIR", raising=False)
    monkeypatch.delenv("TEST_COMMAND", raising=False)
    monkeypatch.setenv("VALIDATION_TIMEOUT_SECONDS", str(TIMEOUT))
    return SimpleNamespace(state={})


# --- scanner -----------------------------------------------------------------


def test_scanner_finds_the_sample_vulnerabilities(baseline: dict) -> None:
    keys = set(scanner.finding_counts(baseline))
    assert {
        "orders/db.py:B608",
        "orders/cart.py:B301",
        "orders/invoices.py:B602",
        "orders/auth.py:B105",
    } <= keys
    assert not any(k.startswith("tests/") for k in keys)
    assert baseline["scanner_error"] is None
    assert baseline["tests_error"] is None


def test_test_harness_reports_per_test_outcomes(baseline: dict) -> None:
    failed = scanner.failed_tests(baseline)
    passed = scanner.passed_tests(baseline)
    assert len(passed) == 9  # every functional test passes
    assert len(failed) == 8  # every exploit test fails
    assert all(".test_security." in t for t in failed)
    assert "tests.test_orders.CartTest.test_cart_round_trip" in passed


def test_generic_test_command_parsing() -> None:
    run = {
        "returncode": 1,
        "stdout": "PASSED tests/test_a.py::test_ok\n"
        "FAILED tests/test_a.py::test_bad - AssertionError: boom\n",
        "stderr": "",
        "timed_out": False,
    }
    tests = scanner._parse_generic(run)["tests"]
    assert tests == [
        {"id": "tests/test_a.py::test_ok", "status": "passed", "detail": ""},
        {
            "id": "tests/test_a.py::test_bad",
            "status": "failed",
            "detail": "AssertionError: boom",
        },
    ]
    fallback = scanner._parse_generic({**run, "stdout": "1 failed\n"})
    assert fallback["tests"][0]["id"] == "test-suite"
    assert fallback["tests"][0]["status"] == "failed"
    argv = scanner.build_test_argv("python -m pytest -rA -q")
    assert argv[0] != "python" and argv[1:] == ["-m", "pytest", "-rA", "-q"]


# --- validator ---------------------------------------------------------------


def test_validator_accepts_a_correct_fix(work: Path, baseline: dict) -> None:
    verdict = validator.validate_patch(
        work, "orders/db.py", DB_FIX, baseline, TIMEOUT, None, SKIPS
    )
    assert verdict["accepted"], verdict["reasons"]
    assert verdict["resolved_findings"] == ["orders/db.py:B608"]
    assert len(verdict["fixed_tests"]) == 3
    assert verdict["regressions"] == []
    # Validation happens in a scratch copy; the working copy is untouched.
    assert 'f"SELECT' in (work / "orders/db.py").read_text()


def test_validator_rejects_a_fix_that_breaks_behaviour(
    work: Path, baseline: dict
) -> None:
    verdict = validator.validate_patch(
        work, "orders/db.py", DB_BROKEN, baseline, TIMEOUT, None, SKIPS
    )
    assert not verdict["accepted"]
    assert verdict["regressions"] == [
        "tests.test_orders.OrdersTest.test_find_orders_by_status"
    ]


def test_validator_rejects_an_incomplete_fix(
    work: Path, baseline: dict
) -> None:
    verdict = validator.validate_patch(
        work,
        "orders/invoices.py",
        INVOICES_INCOMPLETE,
        baseline,
        TIMEOUT,
        None,
        SKIPS,
    )
    assert not verdict["accepted"]
    assert verdict["fixed_tests"] == []
    assert any("no finding resolved" in r for r in verdict["reasons"])


@pytest.mark.parametrize(
    ("rel_path", "content", "reason"),
    [
        ("tests/test_security.py", "pass\n", "test files"),
        ("orders/db.py", "def broken(:\n", "syntax error"),
        (
            "orders/db.py",
            DB_FIX.replace("import sqlite3", "import sqlite3  # nosec"),
            "nosec",
        ),
        ("../outside.py", "x = 1\n", "relative"),
        ("orders/missing.py", "x = 1\n", "not found"),
    ],
)
def test_validator_prechecks(
    work: Path, baseline: dict, rel_path: str, content: str, reason: str
) -> None:
    verdict = validator.validate_patch(
        work, rel_path, content, baseline, TIMEOUT, None, SKIPS
    )
    assert not verdict["accepted"]
    assert verdict["after"] is None  # rejected before running anything
    assert reason in verdict["reasons"][0]


# --- workspace ---------------------------------------------------------------


def test_safe_path_and_test_detection(tmp_path: Path) -> None:
    (tmp_path / "pkg").mkdir()
    (tmp_path / "pkg" / "mod.py").write_text("x = 1\n")
    (tmp_path / "link.py").symlink_to(tmp_path / "pkg" / "mod.py")
    assert workspace.safe_path(tmp_path, "pkg/mod.py").is_file()
    for bad in ("../etc/passwd", "/etc/passwd", "link.py", ""):
        with pytest.raises(workspace.WorkspaceError):
            workspace.safe_path(tmp_path, bad)
    assert workspace.is_test_path("tests/test_x.py")
    assert workspace.is_test_path("pkg/conftest.py")
    assert workspace.is_test_path("pkg/thing_test.py")
    assert not workspace.is_test_path("orders/db.py")


def test_working_copy_replays_accepted_patches() -> None:
    copy = workspace.create_working_copy(
        workspace.BUNDLED_TARGET, {"orders/db.py": DB_FIX}
    )
    try:
        assert (copy / "orders/db.py").read_text() == DB_FIX
        assert copy.resolve() != workspace.BUNDLED_TARGET
    finally:
        shutil.rmtree(copy.parent)


# --- tools -------------------------------------------------------------------


def test_budget_is_enforced(
    ctx: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("MAX_PATCH_ATTEMPTS", "1")
    first = propose_patch("orders/db.py", "def broken(:\n", "typo", ctx)
    assert first["accepted"] is False
    assert first["patch_attempts_remaining"] == 0
    assert first["done"] is True
    second = propose_patch("orders/db.py", DB_FIX, "parameterize", ctx)
    assert second["budget_exhausted"] is True
    assert len(ctx.state["cm_attempts"]) == 1


def test_full_loop_and_report(ctx: SimpleNamespace) -> None:
    before_hash = _tree_hash(workspace.BUNDLED_TARGET)

    findings = list_findings(ctx)
    assert findings["tests_passed"] == 9
    assert "orders/db.py" in findings["patchable_files"]
    assert "tests/test_security.py" in findings["test_files"]
    assert 'f"SELECT' in read_file("orders/db.py", ctx)["content"]

    rejected = propose_patch(
        "orders/invoices.py", INVOICES_INCOMPLETE, "strip ../", ctx
    )
    assert not rejected["accepted"]
    for rel, content in ALL_FIXES.items():
        result = propose_patch(rel, content, f"fix {rel}", ctx)
        assert result["accepted"], (rel, result["reasons"])
    assert result["done"] is True

    report = get_patch_report(ctx)
    assert report["static_findings_after"] == 0
    assert report["static_findings_before"] >= 4
    assert report["tests_before"] == {"passed": 9, "total": 17}
    assert report["tests_after"] == {"passed": 17, "total": 17}
    assert len(report["exploit_tests_fixed"]) == 8
    assert report["files_changed"] == sorted(ALL_FIXES)
    assert report["attempts_used"] == 5
    assert [a["accepted"] for a in report["attempts"]] == [
        False,
        True,
        True,
        True,
        True,
    ]
    assert (
        '+    query = "SELECT * FROM orders WHERE customer = ?"'
        in (report["diff"])
    )
    assert Path(report["patch_file"]).read_text() == report["diff"]
    # The bundled target is never modified in place.
    assert _tree_hash(workspace.BUNDLED_TARGET) == before_hash


def test_target_dir_points_at_your_own_repo(
    ctx: SimpleNamespace, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    repo = tmp_path / "payments-service"
    shutil.copytree(workspace.BUNDLED_TARGET, repo)
    monkeypatch.setenv("TARGET_DIR", str(repo))
    findings = list_findings(ctx)
    assert findings["target"] == "payments-service"
    assert Path(ctx.state["cm_work_dir"]) != repo
    assert not str(ctx.state["cm_work_dir"]).startswith(str(repo))
    assert os.path.isdir(ctx.state["cm_work_dir"])


def test_report_requires_a_baseline(ctx: SimpleNamespace) -> None:
    assert "error" in get_patch_report(ctx)


def test_missing_config_fails_with_a_clear_error(
    ctx: SimpleNamespace, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("MAX_PATCH_ATTEMPTS")
    with pytest.raises(config.ConfigError, match="MAX_PATCH_ATTEMPTS"):
        propose_patch("orders/db.py", DB_FIX, "parameterize", ctx)
