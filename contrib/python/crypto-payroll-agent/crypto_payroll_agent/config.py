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

"""Configuration for the Crypto Payroll Agent."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from decimal import Decimal
from typing import TypedDict


class TokenInfo(TypedDict):
    """Metadata for an ERC-20 token on Base."""

    symbol: str
    address: str
    decimals: int


# Base mainnet token registry.
# Addresses verified against https://basescan.org as of 2025.
# Verify each entry before relying on it in production.
BASE_TOKEN_REGISTRY: dict[str, TokenInfo] = {
    "USDC": {
        "symbol": "USDC",
        "address": "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
        "decimals": 6,
    },
    "USDBC": {
        "symbol": "USDbC",
        "address": "0xd9aAEc86B65D86f6A7B5B1b0c42FFA531710b6CA",
        "decimals": 6,
    },
    "WETH": {
        "symbol": "WETH",
        "address": "0x4200000000000000000000000000000000000006",
        "decimals": 18,
    },
    "CBETH": {
        "symbol": "cbETH",
        "address": "0x2Ae3F1Ec7F1F5012CFEab0185bfc7aa3cf0DEc22",
        "decimals": 18,
    },
    "CBBTC": {
        "symbol": "cbBTC",
        "address": "0xcbB7C0000aB88B473b1f5aFd9ef808440eed33Bf",
        "decimals": 8,
    },
    "DAI": {
        "symbol": "DAI",
        "address": "0x50c5725949A6F0c72E6C4a641F24049A917DB0Cb",
        "decimals": 18,
    },
    "AERO": {
        "symbol": "AERO",
        "address": "0x940181a94A35A4569E4529A3CDfB74e38FD98631",
        "decimals": 18,
    },
}

# Registry symbols that hold a $1 peg. These are the only tokens this
# recipe can value in USD without a price feed, which is what the batch
# ceiling in guardrails.py is applied against.
PEGGED_USD_SYMBOLS: tuple[str, ...] = ("USDC", "USDBC", "DAI")


def _require_env(name: str) -> str:
    """Read a required environment variable, failing loudly if unset."""
    value = os.getenv(name)
    if not value:
        raise RuntimeError(
            f"Environment variable {name} is not set. "
            "Copy .env.example to .env and fill in the values."
        )
    return value


@dataclass(frozen=True)
class Config:
    """Runtime config sourced from environment variables."""

    # Model
    model: str = field(
        default_factory=lambda: _require_env("PAYROLL_AGENT_MODEL")
    )

    # Safety ceiling for a single batch run (in USD)
    max_batch_usd: Decimal = field(
        default_factory=lambda: Decimal(_require_env("PAYROLL_MAX_BATCH_USD"))
    )

    # Safety ceiling for a single ETH batch, denominated in ETH. ETH has
    # no offline USD valuation, so it is bounded in its own units rather
    # than converted.
    max_batch_eth: Decimal = field(
        default_factory=lambda: Decimal(_require_env("PAYROLL_MAX_BATCH_ETH"))
    )

    # Agent metadata
    agent_name: str = "crypto_payroll_agent"
    app_name: str = "Crypto Payroll Agent"

    # Token registry (immutable copy)
    token_registry: dict[str, TokenInfo] = field(
        default_factory=lambda: dict(BASE_TOKEN_REGISTRY)
    )


CONFIG = Config()
