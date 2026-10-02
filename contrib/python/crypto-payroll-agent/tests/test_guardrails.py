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

"""Tests for the before_tool_callback spend ceilings and batch limits.

All offline: the callback is a pure function of the tool name and the
argument dict, so nothing here touches a chain, an RPC endpoint, or a
signing key.
"""

from __future__ import annotations

from types import SimpleNamespace

# Ceilings come from .env.example via conftest: 10000 USD, 5 ETH.
USDC = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"
WETH = "0x4200000000000000000000000000000000000006"

ADDR_A = "0x9f2c000000000000000000000000000000000000"
ADDR_B = "0x4ab7000000000000000000000000000000000000"
ADDR_C = "0x8eee000000000000000000000000000000000000"


def _tool(name: str) -> SimpleNamespace:
    """Stand in for the ADK BaseTool the callback receives."""
    return SimpleNamespace(name=name)


def _call(tool_name: str, **args):
    from crypto_payroll_agent.guardrails import enforce_batch_limits

    return enforce_batch_limits(
        tool=_tool(tool_name), args=args, tool_context=None
    )


def test_allows_token_batch_under_usd_ceiling():
    # 2 x 250 USDC = 500, well under the 10000 USD ceiling.
    assert (
        _call(
            "spraay_batch_token",
            token_address=USDC,
            recipients=[ADDR_A, ADDR_B],
            amount_per_recipient="250",
            token_decimals=6,
        )
        is None
    )


def test_allows_eth_batch_under_eth_ceiling():
    # 2 x 0.01 ETH = 0.02, well under the 5 ETH ceiling.
    assert (
        _call(
            "spraay_batch_eth",
            recipients=[ADDR_A, ADDR_B],
            amount_per_recipient_eth="0.01",
        )
        is None
    )


def test_blocks_token_batch_over_usd_ceiling():
    # 2 x 8000 USDC = 16000, over the 10000 USD ceiling.
    result = _call(
        "spraay_batch_token",
        token_address=USDC,
        recipients=[ADDR_A, ADDR_B],
        amount_per_recipient="8000",
        token_decimals=6,
    )
    assert result is not None
    assert result["status"] == "error"
    assert "PAYROLL_MAX_BATCH_USD" in result["error"]


def test_blocks_eth_batch_over_eth_ceiling():
    # 4 + 3 = 7 ETH, over the 5 ETH ceiling.
    result = _call(
        "spraay_batch_eth_variable",
        recipients=[ADDR_A, ADDR_B],
        amounts_eth=["4", "3"],
    )
    assert result is not None
    assert result["status"] == "error"
    assert "PAYROLL_MAX_BATCH_ETH" in result["error"]


def test_blocks_non_pegged_token_even_under_ceiling():
    # 2 WETH is far under 10000, but WETH has no offline USD value.
    result = _call(
        "spraay_batch_token",
        token_address=WETH,
        recipients=[ADDR_A, ADDR_B],
        amount_per_recipient="1",
        token_decimals=18,
    )
    assert result is not None
    assert result["status"] == "error"
    assert "not a $1-pegged token" in result["error"]
    # The message must name the customization point.
    assert "enforce_batch_limits" in result["error"]


def test_blocks_more_than_200_recipients():
    recipients = [f"0x{i:040x}" for i in range(201)]
    result = _call(
        "spraay_batch_eth",
        recipients=recipients,
        amount_per_recipient_eth="0.0001",
    )
    assert result is not None
    assert result["status"] == "error"
    assert "201 recipients" in result["error"]


def test_blocks_malformed_recipient_address():
    result = _call(
        "spraay_batch_token",
        token_address=USDC,
        recipients=[ADDR_A, "alice.eth"],
        amount_per_recipient="10",
        token_decimals=6,
    )
    assert result is not None
    assert result["status"] == "error"
    assert "alice.eth" in result["error"]


def test_blocks_variable_batch_with_too_few_amounts():
    # 3 recipients, 2 amounts — the contract would revert on-chain.
    result = _call(
        "spraay_batch_token_variable",
        token_address=USDC,
        recipients=[ADDR_A, ADDR_B, ADDR_C],
        amounts=["10", "20"],
        token_decimals=6,
    )
    assert result is not None
    assert result["status"] == "error"
    assert "3 recipients but 2 amounts" in result["error"]


def test_blocks_variable_batch_with_too_many_amounts():
    # The mismatch is caught in the other direction too.
    result = _call(
        "spraay_batch_eth_variable",
        recipients=[ADDR_A, ADDR_B],
        amounts_eth=["0.1", "0.2", "0.3"],
    )
    assert result is not None
    assert result["status"] == "error"
    assert "2 recipients but 3 amounts" in result["error"]


def test_blocks_batch_with_a_missing_amounts_argument():
    # Every tool carries its amounts in exactly one argument. Omitting it
    # leaves nothing to measure against the ceiling, so the call is
    # refused by name rather than reaching a signing path.
    for tool_name, amount_key, extra in (
        ("spraay_batch_eth", "amount_per_recipient_eth", {}),
        ("spraay_batch_eth_variable", "amounts_eth", {}),
        ("spraay_batch_token", "amount_per_recipient", {"token_address": USDC}),
        (
            "spraay_batch_token_variable",
            "amounts",
            {"token_address": USDC},
        ),
    ):
        result = _call(tool_name, recipients=[ADDR_A, ADDR_B], **extra)
        assert result is not None, tool_name
        assert result["status"] == "error"
        assert f"missing its {amount_key} argument" in result["error"]


def test_blocks_batch_with_non_numeric_amounts():
    # Present but unparseable is a different refusal from absent.
    result = _call(
        "spraay_batch_token",
        token_address=USDC,
        recipients=[ADDR_A, ADDR_B],
        amount_per_recipient="two hundred fifty",
        token_decimals=6,
    )
    assert result is not None
    assert result["status"] == "error"
    assert "not numeric" in result["error"]


def test_accepts_uppercase_0x_prefix_but_stays_strict():
    from crypto_payroll_agent.guardrails import _is_eth_address

    # An uppercase prefix is still a valid address, as in helpers.py.
    assert _is_eth_address("0X" + ADDR_A[2:])
    # Everything else stays strict: wrong length, non-hex, no prefix.
    assert not _is_eth_address(ADDR_A[:-1])
    assert not _is_eth_address("0x" + "z" * 40)
    assert not _is_eth_address(ADDR_A[2:])
    assert not _is_eth_address(None)


def test_ignores_tools_that_are_not_spraay_batches():
    assert _call("lookup_token_info", symbol_or_address="USDC") is None


def test_agent_wires_the_callback():
    from crypto_payroll_agent import root_agent
    from crypto_payroll_agent.guardrails import enforce_batch_limits

    assert root_agent.before_tool_callback is enforce_batch_limits
