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

"""Gated live disconnect-recovery probe.

Real-Postgres resilience can't be unit-tested; this probe validates the retry
path end-to-end by forcibly terminating the backend mid-operation and asserting
the wrapped call still succeeds. Set RUN_DB_RESILIENCE_PROBE=1 and LHA_PROBE_DB_URL
to run against a throwaway Postgres.
"""

import os

import pytest

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_DB_RESILIENCE_PROBE") != "1",
    reason="set RUN_DB_RESILIENCE_PROBE=1 and LHA_PROBE_DB_URL to run",
)


@pytest.mark.asyncio
async def test_retry_survives_forced_backend_termination():
    import asyncpg

    from app.infrastructure.db_resilience import retry_on_disconnect

    dsn = os.environ["LHA_PROBE_DB_URL"]  # e.g. postgres://...@localhost/lha
    pool = await asyncpg.create_pool(dsn=dsn, min_size=1, max_size=2)

    killed = {"done": False}

    async def op():
        async with pool.acquire() as conn:
            if not killed["done"]:
                pid = await conn.fetchval("SELECT pg_backend_pid()")
                async with pool.acquire() as killer:
                    await killer.execute("SELECT pg_terminate_backend($1)", pid)
                killed["done"] = True
            return await conn.fetchval("SELECT 1")

    result = await retry_on_disconnect(op, base_delay=0.1)
    assert result == 1
    await pool.close()
