# Data Model Documentation

This document describes the database seeding and user data model for the customer support multi-agent system.

## Overview

The system distinguishes between **global data** (accessible by everyone) and **user-specific data** (linked to individual user accounts).

## Data Categories

### Global Data (Public Catalog)

| Collection | Description | Access |
|------------|-------------|--------|
| `products` | Product catalog with details, specs, pricing | Everyone |
| `inventory` | Stock levels per warehouse | Everyone |
| `reviews` | Product ratings and customer reviews | Everyone |

### User-Specific Data (Requires Authentication)

| Collection | Description | Linked By |
|------------|-------------|-----------|
| `orders` | Purchase history, tracking info | `customer_id` = `user_id` |
| `invoices` | Billing records | `customer_id` = `user_id` |
| `payments` | Payment status and transactions | `customer_id` = `user_id` |
| `refund_eligibility` | Refund rules per order | `customer_id` = `user_id` |
| `users` | User accounts and profiles | `user_id` |

## User Types

### 1. Demo Users (Pre-seeded Accounts)

Demo users have pre-configured order history for testing all system features.

| Email | Password | User ID | Tier | Description |
|-------|----------|---------|------|-------------|
| `demo@example.com` | `demo123` | `demo-user-001` | Gold | Active shopper with 3 orders |
| `jane@example.com` | `jane123` | `demo-user-002` | Silver | Occasional shopper with 1 order |

#### Demo User 001 (demo@example.com)

**Orders:**
| Order ID | Status | Total | Refundable |
|----------|--------|-------|------------|
| ORD-12345 | In Transit | $1,295.98 | Yes - within 30 days |
| ORD-67890 | Delivered | $215.99 | Yes - within 30 days |
| ORD-11111 | Delivered | $485.99 | No - past 30 days |

**Invoices:** INV-2025-001, INV-2025-002, INV-2024-003

#### Demo User 002 (jane@example.com)

**Orders:**
| Order ID | Status | Total | Refundable |
|----------|--------|-------|------------|
| ORD-22222 | Processing | $647.99 | Yes - not yet shipped |

**Invoices:** INV-2025-004

### 2. New Users (Fresh Accounts)

Users who register through the portal start with:
- **No order history**
- **No invoices**
- **No refund eligibility records**
- **Full access to product catalog** (browse, search, get details)

### 3. Anonymous Users

Users who don't register can:
- Browse products
- Search catalog
- View product details, inventory, reviews

Cannot:
- Track orders
- View invoices
- Request refunds
- Access any user-specific features

## Data Access Matrix

```
+------------------+---------------+---------------+---------------+
|    Data Type     |  Demo User    |   New User    |  Anonymous    |
+------------------+---------------+---------------+---------------+
| Products         |      Yes      |      Yes      |      Yes      |
| Inventory        |      Yes      |      Yes      |      Yes      |
| Reviews          |      Yes      |      Yes      |      Yes      |
+------------------+---------------+---------------+---------------+
| Orders           | 3 pre-seeded  |     None      |      No       |
| Invoices         | 3 pre-seeded  |     None      |      No       |
| Payments         | 3 pre-seeded  |     None      |      No       |
| Refund Eligibility| 3 pre-seeded |     None      |      No       |
+------------------+---------------+---------------+---------------+
```

## How User-Data Linking Works

### The Key: `customer_id` = `user_id`

When a user logs in, the system uses their `user_id` to query user-specific data:

```python
# Example: get_my_order_history()
user_id = tool_context.user_id  # e.g., "demo-user-001"
query = db.collection("orders").where("customer_id", "==", user_id)
```

### Demo Users

Demo users have **known user_ids** that are pre-seeded in the database:
- `demo@example.com` -> `user_id: "demo-user-001"`
- `jane@example.com` -> `user_id: "demo-user-002"`

All their orders, invoices, etc. have `customer_id: "demo-user-001"` or `"demo-user-002"`.

### New Users

New users get **random UUIDs** as their `user_id`:
- `newuser@gmail.com` -> `user_id: "a1b2c3d4-e5f6-..."`

Since no orders exist with this `customer_id`, they see "No orders found."

## Seeding the Database

### Run the Seed Script

```bash
# Seed with demo data
python -m customer_support_mas.database.fixtures \
    --project YOUR_PROJECT_ID \
    --database customer-support-db

# Clear and re-seed (fresh start)
python -m customer_support_mas.database.fixtures \
    --project YOUR_PROJECT_ID \
    --database customer-support-db \
    --clear
```

### What Gets Seeded

1. **Products** (6 items): Laptops, headphones, keyboard, chair, desk
2. **Inventory**: Stock levels for all products
3. **Reviews**: Ratings and comments for select products
4. **Users**: Demo user accounts (demo-user-001, demo-user-002)
5. **Orders**: 4 orders linked to demo users
6. **Invoices**: 4 invoices linked to demo users
7. **Payments**: Payment records for all orders
8. **Refund Eligibility**: Refund rules for all orders

## Authentication Flow

### Login with Demo Account

1. User enters `demo@example.com` / `demo123`
2. System finds user in `users` collection with `user_id: "demo-user-001"`
3. Password verified, token generated
4. User can now query their orders, invoices, etc.

### Register New Account

1. User enters `newuser@gmail.com` / `password123`
2. System checks email is not a demo email
3. Creates new user with random UUID: `user_id: "abc123-..."`
4. User starts with empty order history

### Attempt to Register Demo Email

1. User enters `demo@example.com` / `anypassword`
2. System detects this is a demo email
3. Returns error: "This email is reserved for demo purposes. Please log in with password 'demo123' instead."

## Security: Ownership Verification (Production Pattern)

All user-specific tools verify ownership using **decorators** before returning data.
This is a production-ready pattern with:
- Single database fetch (no redundant calls)
- Audit logging for security compliance
- Clean separation of concerns

### Decorator Architecture

```python
# customer_support_mas/auth.py - Reusable authorization decorators

@requires_order_ownership
def track_order(order_id: str, tool_context: ToolContext, _order_data: dict = None):
    # _order_data is pre-fetched by decorator - no redundant DB call
    # Ownership already verified, audit logged
    return {"status": "success", "order": _order_data}

@requires_authenticated_user
def get_my_orders(tool_context: ToolContext, _user_id: str = None):
    # _user_id injected by decorator
    # Query uses authenticated user's ID
    ...
```

### Available Decorators

| Decorator | Purpose | Injected Data |
|-----------|---------|---------------|
| `@requires_order_ownership` | Verify user owns the order | `_order_data`, `_order_id` |
| `@requires_invoice_ownership` | Verify user owns the invoice | `_invoice_data`, `_invoice_id` |
| `@requires_authenticated_user` | Ensure user is logged in | `_user_id` |

### Tools with Ownership Verification

| Tool | Decorator | Verification |
|------|-----------|--------------|
| `track_order(order_id)` | `@requires_order_ownership` | Single fetch, ownership verified |
| `get_invoice(invoice_id)` | `@requires_invoice_ownership` | Single fetch, ownership verified |
| `get_invoice_by_order_id(order_id)` | `@requires_order_ownership` | Order ownership verified |
| `check_payment_status(order_id)` | `@requires_order_ownership` | Order ownership verified |

### Tools Using Authenticated User

| Tool | Decorator | Description |
|------|-----------|-------------|
| `get_order_history()` | `@requires_authenticated_user` | Full order details |
| `get_my_order_history()` | `@requires_authenticated_user` | Order summary |
| `get_my_invoices()` | `@requires_authenticated_user` | All invoices |
| `get_my_payments()` | `@requires_authenticated_user` | All payments |

### Workflow Tools (Helper Function)

Workflow tools use `verify_order_ownership()` helper instead of decorators
because they need to control `tool_context.actions.escalate`:

```python
# Workflow tools need escalation control
is_authorized, order_data, error_msg = verify_order_ownership(order_id, user_id)
if not is_authorized:
    tool_context.actions.escalate = True  # Stop the SequentialAgent
    return {"status": "error", "message": error_msg}
```

## Enhanced Refund Workflow

The refund system includes comprehensive validation and tracking:

### Workflow Steps

| Step | Tool | Validates |
|------|------|-----------|
| 1 | `validate_refund_request()` | Ownership, delivery status, items exist in order |
| 2 | `check_refund_eligibility()` | 30-day return window, items not already refunded |
| 3 | `process_refund()` | Creates refund record with item tracking |

### Features

- **Item Verification**: Requested items must exist in the user's order
- **Dynamic Eligibility**: Calculated from delivery date (not static records)
- **Duplicate Prevention**: Items already refunded are automatically excluded
- **Partial Refunds**: Users can refund specific items with `item_ids` parameter
- **Audit Trail**: All actions logged for security compliance

### Helper Tool

| Tool | Purpose |
|------|---------|
| `get_refundable_items(order_id)` | Shows what items can still be refunded |

### Refund Eligibility Rules

```
1. Order must be delivered (status = "Delivered")
2. Within 30-day return window from delivery date
3. Items not previously refunded
4. Reason must be product-related (not "changed my mind")
```

### Acceptable vs Unacceptable Refund Reasons

| Acceptable (Product Issues) | Unacceptable (Not Product Issues) |
|-----------------------------|-----------------------------------|
| defective | changed_mind |
| damaged | found_cheaper |
| wrong_item | no_longer_need |
| not_as_described | gift_unwanted |
| missing_parts | ordering_mistake |
| quality_issue | |

**Policy**: Refunds are granted for product-related issues only. Reasons like "I changed my mind" or "found it cheaper elsewhere" are not valid grounds for a refund.

### Example Refund Record

```json
{
  "refund_id": "REF-12345-01",
  "order_id": "ORD-12345",
  "customer_id": "demo-user-001",
  "status": "pending",
  "items": [
    {"product_id": "PROD-001", "name": "Pro Laptop", "qty": 1, "price": 1199.99}
  ],
  "total_refund_amount": 1199.99,
  "reason": "Defective screen",
  "created_at": "2025-01-15T10:30:00"
}
```

### Audit Logging

All access attempts are logged for security compliance:

```
[AUDIT] AUTHORIZED: demo-user-001 -> track_order on order/ORD-12345
[AUDIT] DENIED: demo-user-001 -> track_order on order/ORD-22222 - Belongs to demo-user-002
```

### Authorization Error Example

If user `demo-user-001` tries to access order `ORD-22222` (belongs to `demo-user-002`):

```json
{
  "status": "error",
  "message": "You don't have permission to access order ORD-22222"
}
```

## Quick Reference

### Demo Credentials

```
Email: demo@example.com
Password: demo123
Orders: ORD-12345, ORD-67890, ORD-11111
Refundable: ORD-12345, ORD-67890

Email: jane@example.com
Password: jane123
Orders: ORD-22222
Refundable: ORD-22222
```

### API Endpoint

The `/api` endpoint now displays demo account information:

```bash
curl http://localhost:8000/api
```

Returns demo account credentials and available orders for testing.
