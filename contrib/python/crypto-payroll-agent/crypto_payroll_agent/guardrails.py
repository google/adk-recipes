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

"""Programmatic spend ceilings and batch limits for the Spraay tools.

The agent instruction also states these limits, but prose is not
enforcement: a prompt-injected recipient list or an ordinary model
mistake would sail straight past it. `enforce_batch_limits` is wired in
as the agent's `before_tool_callback`, so every `spraay_batch_*` call is
checked in Python before any transaction is signed.

ADK invokes the callback as
`callback(tool=..., args=..., tool_context=...)` and treats the return
value as: `None` -> run the tool, a dict -> skip the tool and hand that
dict back as its result. Blocking therefore means returning an error
dict in the same shape the Spraay tools use on failure, which the
instruction already tells the model to surface verbatim.

Valuation is deliberately offline. Stablecoins in the bundled registry
are valued at $1; ETH is bounded by its own ceiling in ETH rather than
converted; everything else is blocked, because guessing at the USD value
of an arbitrary ERC-20 without a price feed is worse than refusing.
"""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any

from google.adk.tools.base_tool import BaseTool

from .config import CONFIG, PEGGED_USD_SYMBOLS
from .tools.helpers import ETH_ADDRESS_LENGTH

# Spraay's per-transaction recipient cap.
MAX_RECIPIENTS = 200

ETH_TOOLS = frozenset({"spraay_batch_eth", "spraay_batch_eth_variable"})
TOKEN_TOOLS = frozenset({"spraay_batch_token", "spraay_batch_token_variable"})

# Where each tool keeps its amounts: (equal-amount key, per-recipient key).
_AMOUNT_KEYS = {
    "spraay_batch_eth": ("amount_per_recipient_eth", None),
    "spraay_batch_eth_variable": (None, "amounts_eth"),
    "spraay_batch_token": ("amount_per_recipient", None),
    "spraay_batch_token_variable": (None, "amounts"),
}


def _pegged_usd_addresses() -> dict[str, str]:
    """Map lower-cased address -> display symbol for the $1-pegged set."""
    return {
        CONFIG.token_registry[symbol]["address"].lower(): CONFIG.token_registry[
            symbol
        ]["symbol"]
        for symbol in PEGGED_USD_SYMBOLS
        if symbol in CONFIG.token_registry
    }


def _blocked(reason: str) -> dict[str, Any]:
    """Build the refusal in the shape the Spraay tools use for errors."""
    return {
        "status": "error",
        "error": reason,
        "blocked_by": "crypto_payroll_agent guardrail",
    }


def _is_eth_address(value: Any) -> bool:
    """True when `value` is a 0x-prefixed, 40-hex-character address."""
    if not isinstance(value, str) or len(value) != ETH_ADDRESS_LENGTH:
        return False
    if not value.lower().startswith("0x"):
        return False
    return all(c in "0123456789abcdefABCDEF" for c in value[2:])


def _amount_key(tool_name: str) -> str:
    """The single argument a tool carries its amounts in."""
    equal_key, per_recipient_key = _AMOUNT_KEYS[tool_name]
    return equal_key if equal_key is not None else per_recipient_key


def _batch_total(tool_name: str, args: dict[str, Any], count: int) -> Decimal:
    """Total amount a call would move, in the token's own human units.

    The caller has already confirmed the amounts argument is present, so
    the only failure left is an unparseable value: that raises
    InvalidOperation, which the caller turns into a refusal — an
    unreadable amount must never reach a signing path.
    """
    equal_key, per_recipient_key = _AMOUNT_KEYS[tool_name]

    if equal_key is not None:
        return Decimal(str(args[equal_key])) * count

    amounts = args[per_recipient_key]
    if not isinstance(amounts, (list, tuple)):
        raise InvalidOperation(f"{per_recipient_key} is not a list")
    return sum((Decimal(str(a)) for a in amounts), Decimal(0))


def enforce_batch_limits(
    tool: BaseTool,
    args: dict[str, Any],
    tool_context: Any = None,
) -> dict[str, Any] | None:
    """Block a `spraay_batch_*` call that breaches a configured limit.

    Returns None for anything allowed (including non-Spraay tools), so
    ADK proceeds normally. Returns an error dict to refuse, which ADK
    substitutes for the tool result.
    """
    name = tool.name
    if name not in ETH_TOOLS and name not in TOKEN_TOOLS:
        return None

    recipients = args.get("recipients")
    if not isinstance(recipients, (list, tuple)) or not recipients:
        return _blocked(
            "Refused: the batch has no recipients. Supply a non-empty "
            "list of 0x addresses."
        )

    count = len(recipients)
    if count > MAX_RECIPIENTS:
        return _blocked(
            f"Refused: {count} recipients exceeds the Spraay limit of "
            f"{MAX_RECIPIENTS} per transaction. Split the batch."
        )

    malformed = [r for r in recipients if not _is_eth_address(r)]
    if malformed:
        return _blocked(
            f"Refused: {len(malformed)} recipient address(es) are not "
            f"0x-prefixed 40-hex-character addresses, starting with "
            f"{malformed[0]!r}. Resolve names to addresses first."
        )

    per_recipient_key = _AMOUNT_KEYS[name][1]
    if per_recipient_key is not None:
        amounts = args.get(per_recipient_key)
        if isinstance(amounts, (list, tuple)) and len(amounts) != count:
            return _blocked(
                f"Refused: {count} recipients but {len(amounts)} amounts. "
                f"A variable batch needs one amount per recipient — the "
                f"contract would revert."
            )

    # Absence is checked here rather than left to _batch_total: a bare
    # KeyError says nothing about which argument the model forgot, and
    # the refusal text is what the model gets to act on.
    amount_key = _amount_key(name)
    if amount_key not in args:
        return _blocked(
            f"Refused: the call is missing its {amount_key} argument, so "
            "the spend ceiling cannot be checked."
        )

    try:
        total = _batch_total(name, args, count)
    except (TypeError, ValueError, InvalidOperation):
        return _blocked(
            "Refused: the batch amounts are not numeric, so the spend "
            "ceiling cannot be checked."
        )

    if total <= 0:
        return _blocked("Refused: the batch total must be greater than 0.")

    if name in ETH_TOOLS:
        if total > CONFIG.max_batch_eth:
            return _blocked(
                f"Refused: batch total {total} ETH exceeds the "
                f"PAYROLL_MAX_BATCH_ETH ceiling of "
                f"{CONFIG.max_batch_eth} ETH."
            )
        return None

    token_address = str(args.get("token_address") or "")
    symbol = _pegged_usd_addresses().get(token_address.lower())
    if symbol is None:
        return _blocked(
            f"Refused: {token_address or 'the token'} is not a $1-pegged "
            "token in the bundled registry, so this recipe cannot value "
            "the batch in USD offline and the PAYROLL_MAX_BATCH_USD "
            "ceiling cannot be applied. To allow it, extend "
            "enforce_batch_limits in crypto_payroll_agent/guardrails.py "
            "with a price source, or send a pegged token instead."
        )

    if total > CONFIG.max_batch_usd:
        return _blocked(
            f"Refused: batch total {total} {symbol} (valued at 1 USD "
            f"each) exceeds the PAYROLL_MAX_BATCH_USD ceiling of "
            f"{CONFIG.max_batch_usd} USD."
        )

    return None
