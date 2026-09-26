"""Database layer for Customer Support Agent System."""

from customer_support_mas.database.client import (
    db_client,
    get_db_client,
)

__all__ = [
    "db_client",
    "get_db_client",
]
