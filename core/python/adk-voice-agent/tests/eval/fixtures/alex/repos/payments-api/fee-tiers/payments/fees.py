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

"""Card processing fees, in integer cents."""

from decimal import Decimal

#: The standard card rate, and the fixed fee added to every charge.
PERCENT_FEE = Decimal("0.029")
FIXED_FEE_CENTS = 30

#: Volume tiers: charges at or above the threshold (in cents) get the lower rate.
TIERS = [
    (100_000, Decimal("0.025")),
    (0, PERCENT_FEE),
]


def rate_for(amount_cents: int) -> Decimal:
    """The percentage rate for a charge of `amount_cents`."""
    for threshold, rate in TIERS:
        if amount_cents >= threshold:
            return rate
    return PERCENT_FEE


def processing_fee_cents(amount_cents: int) -> int:
    """The fee for one charge: the tier's rate, rounded half up to the cent, plus the fixed fee."""
    if amount_cents < 0:
        raise ValueError("amount_cents must be non-negative")
    percent = int(Decimal(amount_cents) * rate_for(amount_cents))
    return percent + FIXED_FEE_CENTS
