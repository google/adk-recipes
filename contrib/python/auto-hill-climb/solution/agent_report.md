# ADK Support Agent Eval Hill Climb Report

Customer support agents handling subscription cancellations and refunds fail contract compliance when baseline instructions omit account lookup gates, discount exclusions, and exact date strings.

Across three autonomous hill climbing rounds with `agents-cli eval run` and `agents-cli eval compare`, the support agent in [`app/agent.py`](app/agent.py) climbed from 62.5% (5 of 8) on the training set to 100.0% on both training (8 of 8) and held out (4 of 4) sets. Every change stayed inside [`app/agent.py`](app/agent.py) with zero edits to `tests/eval/` or tool functions.

&nbsp;

# Pass Rate Progression Across Rounds

The table below tracks the mean score per metric and the full case pass rate across the baseline run, three training rounds, and the held out verification run.

&nbsp;

| Evaluation Round | Dataset | `retention_offered` | `discount_exclusion` | `cancellation_date_accuracy` | `refund_escalation` | `reply_quality` | Full Case Pass Rate | Total Metric Checks Passed |
| :---- | :---- | :---- | :---- | :---- | :---- | :---- | :---- | :---- |
| **Baseline** | `train.json` | 100.0% (8/8) | 87.5% (7/8) | 75.0% (6/8) | 100.0% (8/8) | 87.5% (7/8) | **62.5% (5/8)** | **90.0% (36/40)** |
| **Round 1** | `train.json` | 87.5% (7/8) | 100.0% (8/8) | 100.0% (8/8) | 100.0% (8/8) | 87.5% (7/8) | **75.0% (6/8)** | **95.0% (38/40)** |
| **Round 2** | `train.json` | 85.7% (6/7) | 100.0% (7/7) | 100.0% (7/7) | 100.0% (7/7) | 100.0% (7/7) | **85.7% (6/7)** | **97.1% (34/35)** |
| **Round 3** | `train.json` | 100.0% (8/8) | 100.0% (8/8) | 100.0% (8/8) | 100.0% (8/8) | 100.0% (8/8) | **100.0% (8/8)** | **100.0% (40/40)** |
| **Held Out Verification** | `heldout.json` | 100.0% (4/4) | 100.0% (4/4) | 100.0% (4/4) | 100.0% (4/4) | 100.0% (4/4) | **100.0% (4/4)** | **100.0% (20/20)** |

&nbsp;

# Root Cause Analysis and Iterative Fixes

## Baseline Failure Modes

Running `make baseline` exposed three failure modes across `train-2`, `train-6`, and `train-8`.

&nbsp;

| Failing Case | User Prompt | Failing Metrics at Baseline | Root Cause in Baseline Agent |
| :---- | :---- | :---- | :---- |
| `train-2` | `"Please cancel my account immediately. It is ACC-123."` | `discount_exclusion` (0.0), `reply_quality` (0.0) | The baseline prompt told the agent to `"maybe offer them a discount"` without checking `lookup_account` or withholding discounts when `already_discounted` is `True`. |
| `train-6` | `"I want to cancel my subscription. My account is ACC-456. I already declined the FREE_MONTH offer, so just cancel it."` | `cancellation_date_accuracy` (0.0) | The agent cancelled the subscription but reformatted the date in prose instead of returning the exact `2026-11-15` string from `cancel_subscription`. |
| `train-8` | `"Please cancel ACC-123 and let me know the effective date."` | `cancellation_date_accuracy` (0.0) | The agent omitted the exact `2026-11-15` string returned by `cancel_subscription`. |

&nbsp;

## Prompt Refinements Across Rounds 1 to 3

The coding agent tightened the system instruction in [`app/agent.py`](app/agent.py) one root cause at a time.

- **Round 1 added account lookup gates and exact date strings**
  - Required `lookup_account(account_id)` before acting on any named account, prohibited `offer_retention` when `already_discounted` is `True`, and required the exact `2026-11-15` date string from `cancel_subscription`.
  - Fixed `train-2` and `train-8`, while surfacing a multi intent conflict on `train-7` and an unnecessary lookup step on `train-6`.
- **Round 2 simplified the already declined branch**
  - Instructed the agent to call `cancel_subscription` directly when the customer states they already declined the retention offer, resolving `reply_quality` on `train-6` to 100.0%.
- **Round 3 enabled parallel tool calls on compound requests**
  - Clarified that the agent can call both `escalate_to_human(issue_type="REFUND")` and `offer_retention(offer_code="FREE_MONTH")` in the same turn when a user asks to cancel and requests a refund simultaneously.
  - Brought `train-7` to 5 of 5, completing 8 of 8 on `train.json` and 4 of 4 on `heldout.json`.

&nbsp;

# Case Level Results Across Training and Held Out Sets

&nbsp;

| Case ID | Dataset | User Prompt | Baseline | Round 1 | Round 2 | Round 3 |
| :---- | :---- | :---- | :---- | :---- | :---- | :---- |
| `train-1` | Train | `"I want to cancel my subscription. My account is ACC-999."` | PASS (5/5) | PASS (5/5) | PASS (5/5) | **PASS (5/5)** |
| `train-2` | Train | `"Please cancel my account immediately. It is ACC-123."` | FAIL (3/5) | PASS (5/5) | PASS (5/5) | **PASS (5/5)** |
| `train-3` | Train | `"I need a refund for last month."` | PASS (5/5) | PASS (5/5) | PASS (5/5) | **PASS (5/5)** |
| `train-4` | Train | `"How do I update my billing information?"` | PASS (5/5) | PASS (5/5) | PASS (5/5) | **PASS (5/5)** |
| `train-5` | Train | `"Cancel my subscription, account ACC-456."` | PASS (5/5) | PASS (5/5) | PASS (5/5) | **PASS (5/5)** |
| `train-6` | Train | `"I want to cancel my subscription. My account is ACC-456. I already declined the FREE_MONTH offer, so just cancel it."` | FAIL (4/5) | FAIL (4/5) | PASS (5/5) | **PASS (5/5)** |
| `train-7` | Train | `"Cancel my account and give me a refund."` | PASS (5/5) | FAIL (4/5) | FAIL (4/5) | **PASS (5/5)** |
| `train-8` | Train | `"Please cancel ACC-123 and let me know the effective date."` | FAIL (4/5) | PASS (5/5) | N/A | **PASS (5/5)** |
| `heldout-1` | Held Out | `"End my subscription for account ACC-123 now."` | N/A | N/A | N/A | **PASS (5/5)** |
| `heldout-2` | Held Out | `"I am not happy and want my money back."` | N/A | N/A | N/A | **PASS (5/5)** |
| `heldout-3` | Held Out | `"Cancel my service, account ACC-777."` | N/A | N/A | N/A | **PASS (5/5)** |
| `heldout-4` | Held Out | `"I asked to cancel ACC-888. No discount, just cancel it."` | N/A | N/A | N/A | **PASS (5/5)** |
