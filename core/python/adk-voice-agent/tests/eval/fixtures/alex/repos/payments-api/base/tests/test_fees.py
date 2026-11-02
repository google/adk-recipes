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

from payments.fees import FIXED_FEE_CENTS, processing_fee_cents


class ProcessingFeeTest(unittest.TestCase):
    def test_small_charge(self):
        # 2.9% of $10.00 is 29 cents.
        self.assertEqual(processing_fee_cents(1000), 29 + FIXED_FEE_CENTS)

    def test_fee_rounds_half_up(self):
        # 2.9% of $15.00 is 43.5 cents, which rounds up to 44.
        self.assertEqual(processing_fee_cents(1500), 74)

    def test_zero_amount_pays_the_fixed_fee(self):
        self.assertEqual(processing_fee_cents(0), FIXED_FEE_CENTS)

    def test_negative_amount_is_rejected(self):
        with self.assertRaises(ValueError):
            processing_fee_cents(-1)


if __name__ == "__main__":
    unittest.main()
