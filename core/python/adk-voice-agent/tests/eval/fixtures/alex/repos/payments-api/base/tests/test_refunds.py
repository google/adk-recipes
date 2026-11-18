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

import unittest

from payments.refunds import refundable_cents


class RefundableTest(unittest.TestCase):
    def test_partial_refund(self):
        self.assertEqual(refundable_cents(5000, 1200), 3800)

    def test_fully_refunded(self):
        self.assertEqual(refundable_cents(5000, 5000), 0)

    def test_over_refund_is_clamped(self):
        self.assertEqual(refundable_cents(5000, 6000), 0)


if __name__ == "__main__":
    unittest.main()
