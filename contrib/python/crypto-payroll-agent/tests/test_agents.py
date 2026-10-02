# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Unit tests for the Crypto Payroll Agent.

The local helpers are tested directly. The agent assembly smoke test
imports the agent and verifies its tool roster. The Spraay batch tools
themselves are integration-tested against `google-adk-community` upstream
and are not re-tested here.
"""

from __future__ import annotations

from decimal import Decimal


# --------------------------------------------------------------------------
# lookup_token_info
# --------------------------------------------------------------------------
def test_lookup_token_info_resolves_known_symbol():
    from crypto_payroll_agent.tools.helpers import lookup_token_info

    result = lookup_token_info("USDC")
    assert result["found"] is True
    assert result["symbol"] == "USDC"
    assert result["decimals"] == 6
    assert result["address"].startswith("0x833589")


def test_lookup_token_info_is_case_insensitive():
    from crypto_payroll_agent.tools.helpers import lookup_token_info

    for query in ("usdc", "Usdc", "USDC"):
        assert lookup_token_info(query)["decimals"] == 6


def test_lookup_token_info_resolves_known_address():
    from crypto_payroll_agent.tools.helpers import lookup_token_info

    weth = "0x4200000000000000000000000000000000000006"
    result = lookup_token_info(weth)
    assert result["found"] is True
    assert result["symbol"] == "WETH"
    assert result["decimals"] == 18


def test_lookup_token_info_returns_not_found_for_unknown_symbol():
    from crypto_payroll_agent.tools.helpers import lookup_token_info

    result = lookup_token_info("MYSTERY")
    assert result["found"] is False
    assert result["decimals"] is None
    assert "not a recognized symbol" in result["note"]


def test_lookup_token_info_returns_not_found_for_unknown_address():
    from crypto_payroll_agent.tools.helpers import lookup_token_info

    unknown = "0x1111111111111111111111111111111111111111"
    result = lookup_token_info(unknown)
    assert result["found"] is False
    assert result["address"] == unknown
    assert "decimals" in result["note"]


# --------------------------------------------------------------------------
# split_pool_proportionally
# --------------------------------------------------------------------------
def test_split_pool_simple_equal_weights():
    from crypto_payroll_agent.tools.helpers import split_pool_proportionally

    result = split_pool_proportionally("300", [1, 1, 1], decimals=6)
    assert result["ok"] is True
    # All three shares are equal and reconcile exactly to the total.
    assert len({Decimal(a) for a in result["amounts"]}) == 1
    assert Decimal(result["amounts"][0]) == Decimal("100")
    assert Decimal(result["total_distributed"]) == Decimal("300")


def test_split_pool_proportional_weights():
    from crypto_payroll_agent.tools.helpers import split_pool_proportionally

    result = split_pool_proportionally("10000", [120, 80, 50], decimals=6)
    assert result["ok"] is True
    # Reconciles exactly to the pool total.
    assert Decimal(result["total_distributed"]) == Decimal("10000")
    # Largest-weight recipient gets the largest share.
    amounts = [Decimal(a) for a in result["amounts"]]
    assert amounts[0] > amounts[1] > amounts[2]


def test_split_pool_rounding_dust_goes_to_largest_weight():
    from crypto_payroll_agent.tools.helpers import split_pool_proportionally

    # 100 split 5:3:1 at 6 decimals floors to 55.555555 / 33.333333 /
    # 11.111111, leaving 0.000001 of dust. The largest fractional
    # remainder belongs to the largest weight, so the dust quantum
    # lands on recipient 0.
    result = split_pool_proportionally("100", [5, 3, 1], decimals=6)
    assert result["ok"] is True

    amounts = [Decimal(a) for a in result["amounts"]]
    assert amounts == [
        Decimal("55.555556"),
        Decimal("33.333333"),
        Decimal("11.111111"),
    ]
    assert Decimal(result["total_distributed"]) == Decimal("100")


def test_split_pool_rejects_out_of_range_decimals():
    from crypto_payroll_agent.tools.helpers import split_pool_proportionally

    # A negative value inverts the quantum; >18 exceeds the precision of
    # any token on Base.
    for bad in (-1, 19):
        result = split_pool_proportionally("100", [1, 1], decimals=bad)
        assert result["ok"] is False, bad
        assert result["error"] == "decimals must be between 0 and 18."


def test_split_pool_rejects_empty_weights():
    from crypto_payroll_agent.tools.helpers import split_pool_proportionally

    result = split_pool_proportionally("100", [], decimals=6)
    assert result["ok"] is False
    assert "non-empty" in result["error"]


def test_split_pool_rejects_negative_weights():
    from crypto_payroll_agent.tools.helpers import split_pool_proportionally

    result = split_pool_proportionally("100", [1, -1, 2], decimals=6)
    assert result["ok"] is False
    assert "non-negative" in result["error"]


def test_split_pool_rejects_non_finite_weights():
    from crypto_payroll_agent.tools.helpers import split_pool_proportionally

    # NaN compares False against every bound, so it slips past the
    # non-negative guard; infinity poisons the proportional share. Both
    # must be rejected before any Decimal math runs.
    for bad in (float("nan"), float("inf"), float("-inf")):
        result = split_pool_proportionally("100", [1, bad, 2], decimals=6)
        assert result["ok"] is False, bad
        assert result["error"] == "weights must be finite numbers."


def test_split_pool_rejects_zero_total():
    from crypto_payroll_agent.tools.helpers import split_pool_proportionally

    result = split_pool_proportionally("0", [1, 1], decimals=6)
    assert result["ok"] is False
    assert "positive" in result["error"]


def test_split_pool_rejects_non_finite_total():
    from crypto_payroll_agent.tools.helpers import split_pool_proportionally

    # Decimal parses these happily, and comparing a Decimal NaN against a
    # bound raises rather than returning False — so they have to be caught
    # before the positivity check.
    for bad in ("NaN", "Infinity", "-Infinity"):
        result = split_pool_proportionally(bad, [1, 1], decimals=6)
        assert result["ok"] is False, bad
        assert result["error"] == "total_amount must be a finite number."


def test_split_pool_rejects_total_finer_than_token_decimals():
    from crypto_payroll_agent.tools.helpers import split_pool_proportionally

    # 7 fractional digits cannot be represented at 6 decimals, so the
    # shares could never reconcile to the total.
    result = split_pool_proportionally("100.1234567", [1, 1], decimals=6)
    assert result["ok"] is False
    assert result["error"] == (
        "total_amount has more decimal places than the token supports."
    )


def test_split_pool_accepts_total_at_exactly_token_decimals():
    from crypto_payroll_agent.tools.helpers import split_pool_proportionally

    # 6 fractional digits is exactly representable at 6 decimals, and
    # still distributes to the cent.
    result = split_pool_proportionally("100.123456", [1, 1], decimals=6)
    assert result["ok"] is True
    assert Decimal(result["total_distributed"]) == Decimal("100.123456")


def test_split_pool_accepts_trailing_zeros_beyond_decimals():
    from crypto_payroll_agent.tools.helpers import split_pool_proportionally

    # "100.0000000" has 7 fractional digits but only 2 significant ones
    # past the point, so it is representable at 6 decimals.
    result = split_pool_proportionally("100.0000000", [1, 1], decimals=6)
    assert result["ok"] is True
    assert Decimal(result["total_distributed"]) == Decimal("100")
    # Amounts stay in plain decimal form — no scientific notation leaks
    # out of normalize() into the strings handed to the batch tools.
    assert result["amounts"] == ["50.000000", "50.000000"]


def test_split_pool_rejects_zero_weight_sum():
    from crypto_payroll_agent.tools.helpers import split_pool_proportionally

    result = split_pool_proportionally("100", [0, 0, 0], decimals=6)
    assert result["ok"] is False
    assert "zero" in result["error"]


def test_split_pool_18_decimals_for_eth_amounts():
    from crypto_payroll_agent.tools.helpers import split_pool_proportionally

    # Splitting 1 ETH among 3 with 18 decimals.
    result = split_pool_proportionally("1", [1, 1, 1], decimals=18)
    assert result["ok"] is True
    assert Decimal(result["total_distributed"]) == Decimal("1")


# --------------------------------------------------------------------------
# Agent assembly smoke test
# --------------------------------------------------------------------------
def test_root_agent_has_expected_tools():
    """Agent imports cleanly and exposes all six tools."""
    from crypto_payroll_agent import root_agent

    tool_names = {
        getattr(t, "__name__", None) or getattr(t, "name", "")
        for t in root_agent.tools
    }

    # Local helpers
    assert "lookup_token_info" in tool_names
    assert "split_pool_proportionally" in tool_names

    # Spraay batch tools
    assert "spraay_batch_eth" in tool_names
    assert "spraay_batch_token" in tool_names
    assert "spraay_batch_eth_variable" in tool_names
    assert "spraay_batch_token_variable" in tool_names

    assert root_agent.name == "crypto_payroll_agent"
