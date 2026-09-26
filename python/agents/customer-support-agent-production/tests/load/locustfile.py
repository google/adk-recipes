"""
Load tests — run against staging after deploy to validate SLOs before prod promotion.

Tests infrastructure availability under concurrent connections only.
LLM response quality and latency are not tested here — latency is dominated
by LLM inference (not infra) and correctness is gated by smoke tests and
post-deploy eval (eval_vertex.py).

5 concurrent users, 2 minute run — enough to surface concurrency issues
without burning Agent Engine quota.

Usage:
    locust -f tests/load/locustfile.py \\
      --headless --users 5 --spawn-rate 1 --run-time 2m \\
      --host https://your-service-url \\
      --csv /tmp/load-results --exit-code-on-error 1
"""

from locust import HttpUser, between, task


class CustomerSupportUser(HttpUser):
    """Validates the service stays up and accepts connections under concurrent load."""

    wait_time = between(1, 3)

    @task
    def health_check(self):
        self.client.get("/health", name="/health")
