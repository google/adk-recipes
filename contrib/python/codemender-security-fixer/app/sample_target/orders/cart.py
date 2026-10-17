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
"""Shopping-cart cookie encoding. INTENTIONALLY VULNERABLE sample code."""

import base64
import pickle


def dump_cart(cart: dict[str, int]) -> str:
    """Encodes a cart ({sku: quantity}) into a cookie value."""
    return base64.b64encode(pickle.dumps(cart)).decode("ascii")


def load_cart(cookie: str) -> dict[str, int]:
    """Decodes a cart cookie sent back by the browser.

    Raises:
        ValueError: if the cookie is not a valid cart.
    """
    # VULNERABLE: unpickling attacker-controlled bytes runs arbitrary code.
    cart = pickle.loads(base64.b64decode(cookie))  # noqa: S301 -- intentional sample vulnerability
    if not isinstance(cart, dict):
        raise ValueError("cart must be a mapping")
    return cart
