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
"""Exploit / regression tests. They FAIL on the vulnerable code by design.

Each test reproduces one attack an AppSec reviewer would try. A fix is only
complete when its exploit test passes and every functional test still does.
"""

import base64
import builtins
import importlib
import os
import pickle
import subprocess
import tempfile
import unittest
from unittest import mock

from orders import auth, cart, db, invoices

LEAKED_DEFAULT = "Bearer orders-admin-change-me"


class SqlInjectionTest(unittest.TestCase):
    def setUp(self):
        self.conn = db.connect()

    def test_customer_filter_cannot_be_bypassed(self):
        self.assertEqual(db.find_orders(self.conn, "x' OR '1'='1"), [])

    def test_status_filter_cannot_be_bypassed(self):
        rows = db.find_orders(self.conn, "nobody", "x' OR '1'='1")
        self.assertEqual(rows, [])

    def test_quote_in_customer_name_is_data_not_sql(self):
        self.assertEqual(db.find_orders(self.conn, "o'brien"), [])


class PathTraversalTest(unittest.TestCase):
    def test_cannot_read_outside_invoice_dir(self):
        with tempfile.TemporaryDirectory() as root:
            invoice_dir = os.path.join(root, "invoices")
            os.makedirs(os.path.join(invoice_dir, "2026-01"))
            secret = os.path.join(root, "service-account.json")
            with open(secret, "w") as f:
                f.write("TOP-SECRET")
            payloads = [
                "../service-account.json",
                "2026-01/../../service-account.json",
                "....//service-account.json",
                secret,
            ]
            for payload in payloads:
                with self.subTest(payload=payload):
                    try:
                        body = invoices.read_invoice(invoice_dir, payload)
                    except (ValueError, OSError):
                        body = b""
                    self.assertNotIn(b"TOP-SECRET", body)


class CommandInjectionTest(unittest.TestCase):
    def test_month_cannot_run_shell_commands(self):
        with tempfile.TemporaryDirectory() as root:
            invoice_dir = os.path.join(root, "invoices")
            os.makedirs(os.path.join(invoice_dir, "2026-01"))
            marker = os.path.join(root, "pwned")
            month = f"2026-01$(touch {marker})"
            try:
                invoices.archive_invoices(invoice_dir, month, root)
            except (ValueError, OSError, subprocess.SubprocessError):
                pass
            self.assertFalse(os.path.exists(marker), "shell command ran")


class _Exploit:
    def __reduce__(self):
        return (exec, ("import builtins; builtins._codemender_pwned = True",))


class DeserializationTest(unittest.TestCase):
    def tearDown(self):
        if hasattr(builtins, "_codemender_pwned"):
            del builtins._codemender_pwned

    def test_cart_cookie_cannot_execute_code(self):
        cookie = base64.b64encode(pickle.dumps(_Exploit())).decode()
        try:
            cart.load_cart(cookie)
        except (ValueError, pickle.UnpicklingError):
            pass
        self.assertFalse(hasattr(builtins, "_codemender_pwned"))


class HardcodedSecretTest(unittest.TestCase):
    def tearDown(self):
        importlib.reload(auth)

    def test_admin_token_comes_from_environment(self):
        with mock.patch.dict(os.environ, {"ORDERS_API_TOKEN": "tok-from-env"}):
            importlib.reload(auth)
            self.assertTrue(auth.is_admin("Bearer tok-from-env"))
            self.assertFalse(auth.is_admin(LEAKED_DEFAULT))

    def test_fails_closed_when_token_unset(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            importlib.reload(auth)
            self.assertFalse(auth.is_admin(LEAKED_DEFAULT))
            self.assertFalse(auth.is_admin("Bearer "))
            self.assertFalse(auth.is_admin("Bearer None"))


if __name__ == "__main__":
    unittest.main()
