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
"""Functional tests: the behaviour every security fix must preserve."""

import os
import tarfile
import tempfile
import unittest

from orders import auth, cart, db, invoices

INVOICE_DIR = os.path.join(os.path.dirname(__file__), "..", "invoices")


class OrdersTest(unittest.TestCase):
    def setUp(self):
        self.conn = db.connect()

    def test_find_orders_by_customer(self):
        ids = [o["id"] for o in db.find_orders(self.conn, "acme")]
        self.assertEqual(sorted(ids), ["ORD-1001", "ORD-1002"])

    def test_find_orders_by_status(self):
        rows = db.find_orders(self.conn, "acme", "shipped")
        self.assertEqual([o["id"] for o in rows], ["ORD-1001"])

    def test_unknown_customer_has_no_orders(self):
        self.assertEqual(db.find_orders(self.conn, "umbrella"), [])


class InvoicesTest(unittest.TestCase):
    def test_download_invoice_in_month_folder(self):
        body = invoices.read_invoice(INVOICE_DIR, "2026-01/INV-1001.txt")
        self.assertIn(b"Customer: acme", body)

    def test_missing_invoice_raises(self):
        with self.assertRaises(OSError):
            invoices.read_invoice(INVOICE_DIR, "2026-01/INV-9999.txt")

    def test_archive_month(self):
        with tempfile.TemporaryDirectory() as out_dir:
            path = invoices.archive_invoices(INVOICE_DIR, "2026-01", out_dir)
            with tarfile.open(path) as tar:
                names = tar.getnames()
        self.assertTrue(any(n.endswith("INV-1001.txt") for n in names))
        self.assertFalse(any("INV-1003" in n for n in names))


class CartTest(unittest.TestCase):
    def test_cart_round_trip(self):
        original = {"SKU-RED-MUG": 2, "SKU-TEE-L": 1}
        self.assertEqual(cart.load_cart(cart.dump_cart(original)), original)

    def test_cookie_is_ascii(self):
        cookie = cart.dump_cart({"SKU-RED-MUG": 2})
        self.assertTrue(cookie.isascii())
        self.assertNotIn(";", cookie)


class AuthTest(unittest.TestCase):
    def test_missing_header_is_rejected(self):
        self.assertFalse(auth.is_admin(None))
        self.assertFalse(auth.is_admin(""))


if __name__ == "__main__":
    unittest.main()
