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
"""HTTP routes for the orders API (stdlib only).

GET  /orders?customer=acme&status=shipped   -> JSON list of orders
GET  /invoices/<name>                       -> invoice download
POST /admin/archive?month=2026-01           -> tar.gz of a month's invoices
GET  /cart (Cookie: cart=...)               -> decoded cart

Run locally with: python -m orders.server  (binds to 127.0.0.1:8081)
"""

import json
import os
import tempfile
from http import cookies
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import parse_qs, unquote, urlparse

from . import auth, cart, db, invoices

INVOICE_DIR = os.path.join(os.path.dirname(__file__), "..", "invoices")


class OrdersHandler(BaseHTTPRequestHandler):
    """Routes requests to the data-access helpers."""

    conn = db.connect()

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: int, payload: object) -> None:
        self._send(status, json.dumps(payload).encode(), "application/json")

    def do_GET(self) -> None:
        url = urlparse(self.path)
        query = {k: v[0] for k, v in parse_qs(url.query).items()}
        try:
            if url.path == "/orders":
                rows = db.find_orders(
                    self.conn, query.get("customer", ""), query.get("status")
                )
                self._json(200, rows)
            elif url.path.startswith("/invoices/"):
                name = unquote(url.path.removeprefix("/invoices/"))
                body = invoices.read_invoice(INVOICE_DIR, name)
                self._send(200, body, "text/plain")
            elif url.path == "/cart":
                jar = cookies.SimpleCookie(self.headers.get("Cookie", ""))
                value = jar["cart"].value if "cart" in jar else ""
                self._json(200, cart.load_cart(value) if value else {})
            else:
                self._json(404, {"error": "not found"})
        except (OSError, ValueError) as exc:
            self._json(400, {"error": str(exc)})

    def do_POST(self) -> None:
        url = urlparse(self.path)
        if url.path != "/admin/archive":
            self._json(404, {"error": "not found"})
            return
        if not auth.is_admin(self.headers.get("Authorization")):
            self._json(401, {"error": "unauthorized"})
            return
        month = parse_qs(url.query).get("month", [""])[0]
        out_dir = tempfile.mkdtemp(prefix="archives-")
        try:
            path = invoices.archive_invoices(INVOICE_DIR, month, out_dir)
        except (OSError, ValueError) as exc:
            self._json(400, {"error": str(exc)})
            return
        self._json(200, {"archive": path})


def main() -> None:
    """Serves the API on localhost."""
    HTTPServer(("127.0.0.1", 8081), OrdersHandler).serve_forever()


if __name__ == "__main__":
    main()
